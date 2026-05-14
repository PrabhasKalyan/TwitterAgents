"""Queue daily follow-up DMs (sequence 2..8) for unreplied threads.

Rules:
  - Only follow up if the most recent DM in a thread is `sent`, `replied=0`,
    and was sent ≥ FOLLOWUP_INTERVAL_HOURS ago.
  - Max FOLLOWUP_DAYS follow-ups per thread (sequence ≤ FOLLOWUP_DAYS + 1).
  - Generated via Gemini with a follow-up-specific prompt.
  - Inserted with review_status='pending' so human approves before send.
"""
import os
from datetime import datetime, timezone

from config import FOLLOWUP_DAYS, FOLLOWUP_INTERVAL_HOURS
from db import connect, init_db, start_run, update_run
from logger import get_logger, get_tail

log = get_logger()

CONTEXT_PATH = os.environ.get("CONTEXT_MD", "/inputs/context.md")
MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")


def _load_context() -> str:
    if not os.path.exists(CONTEXT_PATH):
        return ""
    with open(CONTEXT_PATH) as f:
        return f.read()


def _followup_prompt(context_md: str, prev_dm: str, sequence: int) -> str:
    return f"""You write SHORT follow-up Twitter DMs. The recipient did not reply to the previous message.
Rules:
- Under 180 characters (shorter than the initial)
- Do not apologize for messaging again
- Do not repeat what was in the previous message
- Add one new angle: a question, a specific link/idea, or relevance reminder
- Conversational. No template openers ("Just following up", "Bumping this").
- No emojis

This is follow-up #{sequence - 1} (initial was #1).

Previous DM you sent:
{prev_dm}

Candidate profile:
{context_md}

Output ONLY the follow-up DM text. No preamble, no quotes."""


def run():
    from google import genai
    from google.genai import types

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        log.error("GEMINI_API_KEY not set")
        return

    init_db()
    context_md = _load_context()
    client = genai.Client(api_key=api_key)

    # Find latest DM per thread that is `sent`, unreplied, old enough, sequence < limit.
    with connect() as conn:
        rows = conn.execute(
            f"""WITH latest AS (
                  SELECT d.*, ROW_NUMBER() OVER (
                    PARTITION BY COALESCE(thread_id, id) ORDER BY sequence DESC, id DESC
                  ) AS rn
                  FROM dms d
                  WHERE founder_id IS NOT NULL
                )
                SELECT id, founder_id, company_name, founder_name, twitter_handle, dm_text,
                       COALESCE(thread_id, id) AS root_id, sequence, sent_at
                FROM latest
                WHERE rn = 1
                  AND send_status='sent'
                  AND replied=0
                  AND sequence < {FOLLOWUP_DAYS + 1}
                  AND sent_at IS NOT NULL
                  AND datetime(sent_at) <= datetime('now', '-{FOLLOWUP_INTERVAL_HOURS} hours')
                  -- No newer follow-up already queued/pending in this thread
                  AND NOT EXISTS (
                    SELECT 1 FROM dms d2
                    WHERE COALESCE(d2.thread_id, d2.id) = COALESCE(latest.thread_id, latest.id)
                      AND d2.sequence > latest.sequence
                  )"""
        ).fetchall()

    total = len(rows)
    run_id = start_run("queue_followups", total=total)
    log.info(f"queue_followups: {total} threads eligible")
    if total == 0:
        update_run(run_id, status="completed", log_tail=get_tail(), finished=True)
        return

    for i, r in enumerate(rows, 1):
        next_seq = r["sequence"] + 1
        prompt = _followup_prompt(context_md, r["dm_text"] or "", next_seq)
        config = types.GenerateContentConfig(
            system_instruction=prompt, max_output_tokens=300, temperature=0.8,
        )
        try:
            resp = client.models.generate_content(
                model=MODEL,
                contents=f"Recipient: {r['founder_name']} of {r['company_name']}.",
                config=config,
            )
            text = (resp.text or "").strip()
            if (text.startswith('"') and text.endswith('"')) or \
               (text.startswith("'") and text.endswith("'")):
                text = text[1:-1].strip()
        except Exception as e:
            log.error(f"  {r['founder_name']}: API error {e}")
            continue
        if not text:
            log.warning(f"  {r['founder_name']}: empty follow-up, skipping")
            continue

        with connect() as conn:
            conn.execute(
                """INSERT INTO dms (founder_id, company_name, founder_name, twitter_handle,
                                    dm_text, char_count, thread_id, sequence, review_status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending')""",
                (r["founder_id"], r["company_name"], r["founder_name"], r["twitter_handle"],
                 text, len(text), r["root_id"], next_seq),
            )
            conn.commit()
        log.info(f"[{i}/{total}] queued follow-up #{next_seq - 1} for @{r['twitter_handle']} ({len(text)} chars)")
        update_run(run_id, processed=i, log_tail=get_tail())

    update_run(run_id, status="completed", log_tail=get_tail(), finished=True)
    log.info("queue_followups done")


if __name__ == "__main__":
    run()
