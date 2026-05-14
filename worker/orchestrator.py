"""Continuous batch loop: process companies BATCH_SIZE at a time, end-to-end.

Cycle:
  1. Claim next BATCH_SIZE unprocessed filtered-in companies (batch_status='pending').
  2. find_handles for that batch.
  3. check_activity for any newly-found founders in that batch.
  4. generate_dms for active/semi-active founders in that batch.
  5. send any already-approved DMs (up to DAILY_LIMIT/day).
  6. Mark batch companies as batch_status='done'.
  7. Periodically: check_replies, queue_followups.

Human review happens out-of-band in the dashboard between cycles.
"""
import time

from config import (BATCH_SIZE, FOLLOWUP_QUEUE_INTERVAL,
                    ORCHESTRATOR_IDLE_SLEEP, REPLY_CHECK_INTERVAL)
from db import connect, init_db
from logger import get_logger
from steps import (check_activity, check_replies, find_handles, generate_dms,
                   queue_followups, send_dms)

log = get_logger()


def _claim_next_batch() -> list[int]:
    """Atomically mark the next BATCH_SIZE unprocessed companies as 'processing'."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT id FROM companies WHERE filtered_in=1 AND batch_status='pending' "
            "ORDER BY id ASC LIMIT ?",
            (BATCH_SIZE,),
        ).fetchall()
        if not rows:
            return []
        ids = [r["id"] for r in rows]
        placeholders = ",".join("?" * len(ids))
        conn.execute(
            f"UPDATE companies SET batch_status='processing', "
            f"batch_started_at=CURRENT_TIMESTAMP WHERE id IN ({placeholders})",
            ids,
        )
        conn.commit()
        return ids


def _founders_for_companies(company_ids: list[int]) -> list[int]:
    if not company_ids:
        return []
    placeholders = ",".join("?" * len(company_ids))
    with connect() as conn:
        rows = conn.execute(
            f"SELECT id FROM founders WHERE company_id IN ({placeholders}) "
            f"AND handle_status='found' AND twitter_handle IS NOT NULL",
            company_ids,
        ).fetchall()
        return [r["id"] for r in rows]


def _mark_done(company_ids: list[int]):
    if not company_ids:
        return
    placeholders = ",".join("?" * len(company_ids))
    with connect() as conn:
        conn.execute(
            f"UPDATE companies SET batch_status='done' WHERE id IN ({placeholders})",
            company_ids,
        )
        conn.commit()


def run_once() -> bool:
    """Process one batch. Returns True if work was done, False if idle."""
    company_ids = _claim_next_batch()
    if not company_ids:
        return False

    log.info(f"=== batch of {len(company_ids)} company ids: {company_ids[0]}..{company_ids[-1]} ===")
    try:
        log.info("step: find_handles")
        find_handles.run(company_ids=company_ids)

        founder_ids = _founders_for_companies(company_ids)
        log.info(f"step: check_activity ({len(founder_ids)} founders)")
        if founder_ids:
            check_activity.run(founder_ids=founder_ids)

        log.info("step: generate_dms")
        if founder_ids:
            generate_dms.run(founder_ids=founder_ids)

        log.info("step: send (any already-approved)")
        send_dms.run()
    finally:
        _mark_done(company_ids)
    return True


def loop():
    init_db()
    last_replies = 0.0
    last_followups = 0.0
    log.info(f"orchestrator started — batch_size={BATCH_SIZE}")

    while True:
        worked = run_once()

        now = time.time()
        if now - last_replies > REPLY_CHECK_INTERVAL:
            log.info("=== periodic: check_replies ===")
            try:
                check_replies.run()
            except Exception as e:
                log.error(f"check_replies failed: {e}")
            last_replies = now
        if now - last_followups > FOLLOWUP_QUEUE_INTERVAL:
            log.info("=== periodic: queue_followups ===")
            try:
                queue_followups.run()
            except Exception as e:
                log.error(f"queue_followups failed: {e}")
            last_followups = now

        if not worked:
            log.info(f"idle — sleeping {ORCHESTRATOR_IDLE_SLEEP}s")
            time.sleep(ORCHESTRATOR_IDLE_SLEEP)


if __name__ == "__main__":
    loop()
