"""SQLite helpers shared by all worker steps."""
import os
import sqlite3
from contextlib import contextmanager
from datetime import date

DB_PATH = os.environ.get("DATABASE_URL", "/data/outreach.db")

TABLES_SQL = """
CREATE TABLE IF NOT EXISTS companies (
  id INTEGER PRIMARY KEY,
  name TEXT, one_liner TEXT, tags TEXT, website TEXT,
  yc_url TEXT, team_size INTEGER, status TEXT, stage TEXT, is_hiring BOOLEAN,
  filtered_in BOOLEAN DEFAULT FALSE,
  batch_status TEXT DEFAULT 'pending',
  batch_started_at DATETIME,
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS founders (
  id INTEGER PRIMARY KEY,
  company_id INTEGER REFERENCES companies(id),
  company_name TEXT,
  founder_name TEXT,
  twitter_handle TEXT,
  handle_status TEXT DEFAULT 'pending',
  search_source TEXT,
  confidence TEXT,
  evidence TEXT,
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS founder_activity (
  founder_id INTEGER PRIMARY KEY REFERENCES founders(id),
  last_tweet_at DATETIME,
  tweet_count_recent INTEGER,
  followers INTEGER,
  following INTEGER,
  activity_score TEXT,
  checked_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS dms (
  id INTEGER PRIMARY KEY,
  founder_id INTEGER REFERENCES founders(id),
  company_name TEXT,
  founder_name TEXT,
  twitter_handle TEXT,
  dm_text TEXT,
  char_count INTEGER,
  review_status TEXT DEFAULT 'pending',
  send_status TEXT DEFAULT 'queued',
  sent_at DATETIME,
  error_msg TEXT,
  screenshot_path TEXT,
  thread_id INTEGER,
  sequence INTEGER DEFAULT 1,
  replied BOOLEAN DEFAULT 0,
  reply_checked_at DATETIME,
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS pipeline_runs (
  id INTEGER PRIMARY KEY,
  step TEXT,
  status TEXT,
  started_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  finished_at DATETIME,
  items_processed INTEGER DEFAULT 0,
  items_total INTEGER DEFAULT 0,
  log_tail TEXT
);

CREATE TABLE IF NOT EXISTS daily_send_log (
  id INTEGER PRIMARY KEY,
  date TEXT,
  count INTEGER DEFAULT 0,
  UNIQUE(date)
);
"""

INDEXES_SQL = """
CREATE INDEX IF NOT EXISTS idx_companies_filtered ON companies(filtered_in);
CREATE INDEX IF NOT EXISTS idx_companies_batch ON companies(batch_status);
CREATE INDEX IF NOT EXISTS idx_founders_status ON founders(handle_status);
CREATE INDEX IF NOT EXISTS idx_dms_review ON dms(review_status);
CREATE INDEX IF NOT EXISTS idx_dms_send ON dms(send_status);
CREATE INDEX IF NOT EXISTS idx_dms_thread ON dms(thread_id);
CREATE INDEX IF NOT EXISTS idx_dms_replied ON dms(replied);
"""

# Idempotent column additions for upgrades from older schemas.
MIGRATIONS = [
    ("companies", "batch_status", "TEXT DEFAULT 'pending'"),
    ("companies", "batch_started_at", "DATETIME"),
    ("founders", "search_source", "TEXT"),
    ("founders", "confidence", "TEXT"),
    ("founders", "evidence", "TEXT"),
    ("dms", "thread_id", "INTEGER"),
    ("dms", "sequence", "INTEGER DEFAULT 1"),
    ("dms", "replied", "BOOLEAN DEFAULT 0"),
    ("dms", "reply_checked_at", "DATETIME"),
]


def _migrate(conn):
    for table, col, decl in MIGRATIONS:
        cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if col not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
    conn.commit()


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with connect() as conn:
        # 1) Tables (CREATE IF NOT EXISTS is a no-op on existing tables).
        conn.executescript(TABLES_SQL)
        # 2) Add any missing columns on older DBs BEFORE creating indexes that reference them.
        _migrate(conn)
        # 3) Indexes (safe now that columns exist).
        conn.executescript(INDEXES_SQL)
        conn.commit()


@contextmanager
def connect():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    try:
        yield conn
    finally:
        conn.close()


def start_run(step: str, total: int = 0) -> int:
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO pipeline_runs (step, status, items_total) VALUES (?, 'running', ?)",
            (step, total),
        )
        conn.commit()
        return cur.lastrowid


def update_run(run_id: int, processed: int = None, total: int = None,
               status: str = None, log_tail: str = None, finished: bool = False):
    sets, vals = [], []
    if processed is not None:
        sets.append("items_processed = ?")
        vals.append(processed)
    if total is not None:
        sets.append("items_total = ?")
        vals.append(total)
    if status is not None:
        sets.append("status = ?")
        vals.append(status)
    if log_tail is not None:
        sets.append("log_tail = ?")
        vals.append(log_tail)
    if finished:
        sets.append("finished_at = CURRENT_TIMESTAMP")
    if not sets:
        return
    vals.append(run_id)
    with connect() as conn:
        conn.execute(f"UPDATE pipeline_runs SET {', '.join(sets)} WHERE id = ?", vals)
        conn.commit()


def todays_send_count() -> int:
    today = date.today().isoformat()
    with connect() as conn:
        row = conn.execute("SELECT count FROM daily_send_log WHERE date = ?", (today,)).fetchone()
        return row["count"] if row else 0


def increment_send_count():
    today = date.today().isoformat()
    with connect() as conn:
        conn.execute(
            "INSERT INTO daily_send_log (date, count) VALUES (?, 1) "
            "ON CONFLICT(date) DO UPDATE SET count = count + 1",
            (today,),
        )
        conn.commit()
