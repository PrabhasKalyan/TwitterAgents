"""Load batches.csv and mark filtered_in companies."""
import csv
import os

from db import connect, init_db, start_run, update_run
from logger import get_logger, get_tail

log = get_logger()

CSV_PATH = os.environ.get("BATCHES_CSV", "/inputs/batches.csv")

ALLOWED_STAGES = {"Early", "Growth"}
KEYWORDS = {
    "ai", "artificial intelligence", "generative ai", "machine learning",
    "developer tools", "infrastructure", "b2b", "saas",
}


def _matches_keywords(tags: str, industry: str, subindustry: str) -> bool:
    blob = " ".join([tags or "", industry or "", subindustry or ""]).lower()
    return any(kw in blob for kw in KEYWORDS)


def run():
    init_db()
    if not os.path.exists(CSV_PATH):
        log.error(f"batches.csv not found at {CSV_PATH}")
        return

    with open(CSV_PATH, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    total = len(rows)
    run_id = start_run("filter", total=total)
    log.info(f"Filtering {total} companies from {CSV_PATH}")
    update_run(run_id, log_tail=get_tail())

    inserted = 0
    filtered = 0
    with connect() as conn:
        # Reset so re-running is idempotent
        conn.execute("DELETE FROM companies")
        conn.commit()

        for i, row in enumerate(rows, 1):
            name = (row.get("name") or "").strip()
            if not name:
                continue
            tags = row.get("tags") or ""
            industry = row.get("industry") or ""
            subindustry = row.get("subindustry") or ""
            status = (row.get("status") or "").strip()
            stage = (row.get("stage") or "").strip()
            try:
                team_size = int(row.get("team_size") or 0)
            except ValueError:
                team_size = 0
            is_hiring = (row.get("is_hiring") or "").strip().lower() == "true"

            keep = (
                status == "Active"
                and stage in ALLOWED_STAGES
                and _matches_keywords(tags, industry, subindustry)
            )

            conn.execute(
                """INSERT INTO companies
                   (name, one_liner, tags, website, yc_url, team_size, status, stage, is_hiring, filtered_in)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    name,
                    row.get("one_liner") or "",
                    tags,
                    row.get("website") or "",
                    row.get("yc_url") or "",
                    team_size,
                    status,
                    stage,
                    is_hiring,
                    keep,
                ),
            )
            inserted += 1
            if keep:
                filtered += 1

            if i % 100 == 0 or i == total:
                conn.commit()
                update_run(run_id, processed=i, log_tail=get_tail())
                log.info(f"  processed {i}/{total} (kept {filtered})")

        conn.commit()

    log.info(f"Done. Inserted {inserted}, marked {filtered} as filtered_in.")
    update_run(run_id, processed=total, status="completed", log_tail=get_tail(), finished=True)


if __name__ == "__main__":
    run()
