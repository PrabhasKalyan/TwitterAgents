"""Generate DM drafts using Gemini for each founder with a found handle."""
import os

from db import connect, init_db, start_run, update_run
from logger import get_logger, get_tail

log = get_logger()

CONTEXT_PATH = os.environ.get("CONTEXT_MD", "/inputs/context.md")
MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")


def _load_context() -> str:
    if not os.path.exists(CONTEXT_PATH):
        log.error(f"context.md not found at {CONTEXT_PATH}")
        return ""
    with open(CONTEXT_PATH) as f:
        return f.read()


def _system_prompt(context_md: str) -> str:
    return f"""You write cold Twitter DMs for a job seeker targeting startup founders.
Rules:
- Under 260 characters (leave buffer)
- Conversational, sounds like a real person
- References something specific about their company
- Mentions 1 relevant skill/project from the candidate that fits this company
- Ends with a low-friction ask ("open to a quick chat?" not "please interview me")
- No template openers ("I came across", "I noticed", "Hope this finds you")
- No emojis unless company tone clearly calls for it

Candidate profile:
{context_md}

Output ONLY the DM text. No preamble, no quotes, no explanation."""


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
    system = _system_prompt(context_md)

    with connect() as conn:
        rows = conn.execute(
            """SELECT f.id AS founder_id, f.company_id, f.company_name, f.founder_name, f.twitter_handle,
                      c.one_liner, c.tags
               FROM founders f
               LEFT JOIN companies c ON c.id = f.company_id
               WHERE f.handle_status = 'found'
                 AND f.twitter_handle IS NOT NULL
                 AND f.id NOT IN (SELECT founder_id FROM dms WHERE founder_id IS NOT NULL)"""
        ).fetchall()

    total = len(rows)
    run_id = start_run("generate_dms", total=total)
    log.info(f"Generating DMs for {total} founders with model {MODEL}")

    config = types.GenerateContentConfig(
        system_instruction=system,
        max_output_tokens=400,
        temperature=0.8,
    )

    for i, r in enumerate(rows, 1):
        user_msg = (
            f"Company: {r['company_name']}. "
            f"What they do: {r['one_liner'] or '(unknown)'}. "
            f"Tags: {r['tags'] or '(unknown)'}."
        )
        try:
            resp = client.models.generate_content(
                model=MODEL,
                contents=user_msg,
                config=config,
            )
            dm_text = (resp.text or "").strip()
            # Strip wrapping quotes if the model added them
            if (dm_text.startswith('"') and dm_text.endswith('"')) or \
               (dm_text.startswith("'") and dm_text.endswith("'")):
                dm_text = dm_text[1:-1].strip()
        except Exception as e:
            log.error(f"  {r['founder_name']}: API error {e}")
            continue

        if not dm_text:
            log.warning(f"  {r['founder_name']}: empty response, skipping")
            continue

        char_count = len(dm_text)
        with connect() as conn:
            conn.execute(
                """INSERT INTO dms (founder_id, company_name, founder_name, twitter_handle, dm_text, char_count)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (r["founder_id"], r["company_name"], r["founder_name"],
                 r["twitter_handle"], dm_text, char_count),
            )
            conn.commit()

        log.info(f"[{i}/{total}] {r['founder_name']} @{r['twitter_handle']} ({char_count} chars)")
        update_run(run_id, processed=i, log_tail=get_tail())

    update_run(run_id, status="completed", log_tail=get_tail(), finished=True)
    log.info("generate_dms done")


if __name__ == "__main__":
    run()
