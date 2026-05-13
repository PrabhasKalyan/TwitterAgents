"""FastAPI service: reads SQLite, serves REST + SSE for the dashboard."""
import asyncio
import json
import os
import sqlite3
from datetime import date
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

DB_PATH = os.environ.get("DATABASE_URL", "/data/outreach.db")
LOG_FILE = os.environ.get("LOG_FILE", "/logs/outreach.log")
SCREENSHOT_DIR = os.environ.get("SCREENSHOT_DIR", "/data/screenshots")
DAILY_LIMIT = 20

app = FastAPI(title="Outreach API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def _conn():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_db():
    Path(os.path.dirname(DB_PATH)).mkdir(parents=True, exist_ok=True)
    if not os.path.exists(DB_PATH):
        # Create empty DB with schema so the API works even before worker has run
        conn = _conn()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS companies (id INTEGER PRIMARY KEY, name TEXT, filtered_in BOOLEAN DEFAULT 0);
            CREATE TABLE IF NOT EXISTS founders (id INTEGER PRIMARY KEY, handle_status TEXT, twitter_handle TEXT);
            CREATE TABLE IF NOT EXISTS dms (id INTEGER PRIMARY KEY, review_status TEXT, send_status TEXT,
                                            sent_at DATETIME, dm_text TEXT, char_count INTEGER,
                                            company_name TEXT, founder_name TEXT, twitter_handle TEXT,
                                            error_msg TEXT, screenshot_path TEXT, created_at DATETIME);
            CREATE TABLE IF NOT EXISTS pipeline_runs (id INTEGER PRIMARY KEY, step TEXT, status TEXT,
                                                      started_at DATETIME, finished_at DATETIME,
                                                      items_processed INTEGER, items_total INTEGER, log_tail TEXT);
            CREATE TABLE IF NOT EXISTS daily_send_log (id INTEGER PRIMARY KEY, date TEXT UNIQUE, count INTEGER DEFAULT 0);
        """)
        conn.commit()
        conn.close()


_ensure_db()


def _stats() -> dict:
    conn = _conn()
    try:
        def scalar(q, *p):
            row = conn.execute(q, p).fetchone()
            return row[0] if row else 0

        today = date.today().isoformat()
        return {
            "total_companies": scalar("SELECT COUNT(*) FROM companies"),
            "filtered_companies": scalar("SELECT COUNT(*) FROM companies WHERE filtered_in = 1"),
            "founders_found": scalar("SELECT COUNT(*) FROM founders WHERE handle_status = 'found'"),
            "dms_generated": scalar("SELECT COUNT(*) FROM dms"),
            "dms_approved": scalar("SELECT COUNT(*) FROM dms WHERE review_status = 'approved'"),
            "dms_sent": scalar("SELECT COUNT(*) FROM dms WHERE send_status = 'sent'"),
            "dms_failed": scalar("SELECT COUNT(*) FROM dms WHERE send_status = 'failed'"),
            "todays_sends": scalar("SELECT count FROM daily_send_log WHERE date = ?", today),
            "daily_limit": DAILY_LIMIT,
            "dms_pending": scalar("SELECT COUNT(*) FROM dms WHERE review_status = 'pending'"),
            "dms_skipped": scalar("SELECT COUNT(*) FROM dms WHERE review_status = 'skipped'"),
        }
    finally:
        conn.close()


def _pipeline_status() -> list[dict]:
    conn = _conn()
    try:
        rows = conn.execute(
            """SELECT step, status, started_at, finished_at, items_processed, items_total, log_tail
               FROM pipeline_runs
               WHERE id IN (SELECT MAX(id) FROM pipeline_runs GROUP BY step)"""
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@app.get("/api/stats")
def stats():
    return _stats()


@app.get("/api/pipeline/status")
def pipeline_status():
    return _pipeline_status()


@app.get("/api/dms")
def list_dms(
    status: Optional[str] = Query(None, description="review_status filter"),
    send_status: Optional[str] = Query(None),
    limit: int = 50,
    offset: int = 0,
):
    conn = _conn()
    try:
        clauses, params = [], []
        if status:
            clauses.append("review_status = ?")
            params.append(status)
        if send_status:
            clauses.append("send_status = ?")
            params.append(send_status)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        params += [limit, offset]
        rows = conn.execute(
            f"""SELECT id, founder_id, company_name, founder_name, twitter_handle, dm_text,
                       char_count, review_status, send_status, sent_at, error_msg, screenshot_path, created_at
                FROM dms {where} ORDER BY id DESC LIMIT ? OFFSET ?""",
            params,
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


class ReviewBody(BaseModel):
    status: str  # approved | skipped
    dm_text: Optional[str] = None


@app.patch("/api/dms/{dm_id}/review")
def review_dm(dm_id: int, body: ReviewBody):
    if body.status not in {"approved", "skipped", "pending"}:
        raise HTTPException(400, "status must be approved|skipped|pending")
    conn = _conn()
    try:
        row = conn.execute("SELECT id FROM dms WHERE id = ?", (dm_id,)).fetchone()
        if not row:
            raise HTTPException(404, "DM not found")
        if body.dm_text is not None:
            conn.execute(
                "UPDATE dms SET dm_text = ?, char_count = ?, review_status = ? WHERE id = ?",
                (body.dm_text, len(body.dm_text), body.status, dm_id),
            )
        else:
            conn.execute(
                "UPDATE dms SET review_status = ? WHERE id = ?",
                (body.status, dm_id),
            )
        conn.commit()
        return {"ok": True, "id": dm_id, "status": body.status}
    finally:
        conn.close()


class BulkReviewBody(BaseModel):
    ids: list[int]
    status: str


@app.patch("/api/dms/bulk-review")
def bulk_review(body: BulkReviewBody):
    if body.status not in {"approved", "skipped", "pending"}:
        raise HTTPException(400, "status must be approved|skipped|pending")
    if not body.ids:
        return {"ok": True, "updated": 0}
    conn = _conn()
    try:
        placeholders = ",".join("?" * len(body.ids))
        conn.execute(
            f"UPDATE dms SET review_status = ? WHERE id IN ({placeholders})",
            [body.status, *body.ids],
        )
        conn.commit()
        return {"ok": True, "updated": len(body.ids)}
    finally:
        conn.close()


@app.get("/api/founders")
def list_founders(handle_status: Optional[str] = None, limit: int = 50, offset: int = 0):
    conn = _conn()
    try:
        clauses, params = [], []
        if handle_status:
            clauses.append("handle_status = ?")
            params.append(handle_status)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        params += [limit, offset]
        rows = conn.execute(
            f"""SELECT id, company_id, company_name, founder_name, twitter_handle, handle_status, created_at
                FROM founders {where} ORDER BY id DESC LIMIT ? OFFSET ?""",
            params,
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@app.get("/api/screenshots")
def list_screenshots():
    p = Path(SCREENSHOT_DIR)
    if not p.exists():
        return []
    files = sorted(p.glob("*.png"), key=lambda f: f.stat().st_mtime, reverse=True)
    return [{"name": f.name, "mtime": f.stat().st_mtime, "size": f.stat().st_size} for f in files]


@app.get("/api/screenshots/{filename}")
def get_screenshot(filename: str):
    if "/" in filename or ".." in filename:
        raise HTTPException(400, "invalid filename")
    fp = Path(SCREENSHOT_DIR) / filename
    if not fp.exists():
        raise HTTPException(404, "not found")
    return FileResponse(fp, media_type="image/png")


def _tail_log_lines(n: int = 30) -> list[str]:
    if not os.path.exists(LOG_FILE):
        return []
    try:
        with open(LOG_FILE, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            read = min(size, 64 * 1024)
            f.seek(size - read)
            data = f.read().decode("utf-8", errors="ignore")
        lines = data.splitlines()
        return lines[-n:]
    except Exception:
        return []


@app.get("/api/logs/stream")
async def stream():
    async def gen():
        last_size = 0
        last_stats = None
        last_pipeline = None
        # Send initial snapshot
        yield f"event: stats\ndata: {json.dumps(_stats())}\n\n"
        yield f"event: pipeline\ndata: {json.dumps(_pipeline_status())}\n\n"
        lines = _tail_log_lines(50)
        if lines:
            yield f"event: log\ndata: {json.dumps({'lines': lines})}\n\n"

        while True:
            await asyncio.sleep(2.0)

            # Logs: emit new tail if file changed
            try:
                size = os.path.getsize(LOG_FILE) if os.path.exists(LOG_FILE) else 0
            except OSError:
                size = 0
            if size != last_size:
                last_size = size
                new_lines = _tail_log_lines(30)
                if new_lines:
                    yield f"event: log\ndata: {json.dumps({'lines': new_lines})}\n\n"

            s = _stats()
            if s != last_stats:
                last_stats = s
                yield f"event: stats\ndata: {json.dumps(s)}\n\n"

            p = _pipeline_status()
            if p != last_pipeline:
                last_pipeline = p
                yield f"event: pipeline\ndata: {json.dumps(p)}\n\n"

            # Heartbeat
            yield ": ping\n\n"

    headers = {
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
    }
    return StreamingResponse(gen(), media_type="text/event-stream", headers=headers)


@app.get("/api/health")
def health():
    return {"ok": True, "db_exists": os.path.exists(DB_PATH)}
