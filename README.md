# Outreach Engine

Dockerized Twitter/X DM outreach automation with a live web dashboard.

```
docker-compose.yml
├── worker       Python — batch orchestrator, SQLite writes
├── api          FastAPI — REST + SSE over /data/outreach.db
└── dashboard    Nginx — single-page dashboard, proxies /api
```

## Run on a VM

```bash
# 1. Build + start the API and dashboard (foreground services)
docker compose up -d --build api dashboard

# 2. (only if you previously ran with the old, low-accuracy code)
#    Wipe stale founder/DM rows and reset batch progress.
sqlite3 data/outreach.db "
  DELETE FROM founders;
  DELETE FROM dms;
  UPDATE companies SET batch_status='pending';
" 2>/dev/null

# 3. Build + start the worker (runs the continuous orchestrator)
docker compose --profile worker up -d --build worker

# 4. Tail the logs
docker compose logs -f worker
```

If it's a brand-new VM and `data/outreach.db` doesn't exist yet, skip step 2 — it will be created on first run.

To force a no-cache rebuild after pulling new code:

```bash
docker compose --profile worker down
docker compose --profile worker build --no-cache worker
docker compose --profile worker up -d worker
```

Open the dashboard at **http://&lt;VM_IP&gt;:3000**.

## What the worker does (continuous loop)

```
filter (one-time from inputs/batches.csv)
  ↓
[claim next 20 filtered-in companies]
  ↓
find_handles   per founder:
                 1. company website /team /about /founders pages → direct X links
                 2. YC company page → direct X links + name candidates
                 3. company's official X account → /following crawl (corroboration)
               verify every candidate: bio mentions company OR follows company X
               assign confidence: high | medium | low
               follow the founder on X
  ↓
check_activity  visit profile → last_tweet_at → bucket: active | semi | dormant | dead
  ↓
generate_dms    Gemini drafts; skips 'dead' founders; AUTO-APPROVED
  ↓
send_dms        respects DAILY_LIMIT, longer pacing
  ↓
mark batch done, loop

every 6 h:  check_replies   scan x.com/messages, mark dms.replied=1
every 24 h: queue_followups daily follow-ups for unreplied threads (7 days)
```

State lives in SQLite (`/data/outreach.db`). Restart-safe — the orchestrator resumes from `companies.batch_status`.

## Manual one-shot steps (optional)

```bash
docker compose --profile worker run --rm worker python run.py --step filter
docker compose --profile worker run --rm worker python run.py --step find-handles
docker compose --profile worker run --rm worker python run.py --step follow-founders
docker compose --profile worker run --rm worker python run.py --step check-activity
docker compose --profile worker run --rm worker python run.py --step generate-dms
docker compose --profile worker run --rm worker python run.py --step send --dry-run
docker compose --profile worker run --rm worker python run.py --step send
docker compose --profile worker run --rm worker python run.py --step check-replies
docker compose --profile worker run --rm worker python run.py --step queue-followups
```

## Tunables — edit `worker/config.py`, then rebuild

| Constant                       | Default | Purpose                                              |
| ------------------------------ | ------- | ---------------------------------------------------- |
| `BATCH_SIZE`                   | 20      | Companies per micro-pipeline cycle                   |
| `DAILY_LIMIT`                  | 100     | DMs sent per UTC day (incl. follow-ups)              |
| `FOLLOWUP_DAYS`                | 7       | Daily follow-ups per unreplied thread                |
| `FOLLOWUP_INTERVAL_HOURS`      | 24      | Min hours between consecutive messages in a thread   |
| `ACTIVITY_DORMANT_DAYS`        | 90      | `>this` since last tweet ⇒ 'dead' (skip DM)          |
| `SEND_DELAY_MIN/MAX`           | 180/360 | Seconds between sends                                |
| `ORCHESTRATOR_IDLE_SLEEP`      | 1800    | Sleep when no work to claim                          |
| `FOLLOW_AFTER_VERIFY`          | True    | Follow founder's handle after verifying              |
| `AUTO_APPROVE_DMS`             | True    | Skip human review — send picks DMs up immediately    |
| `MIN_FOLLOWERS_FOR_NAME_ONLY`  | 50      | Reject imposters when only name match is available   |
| `COMPANY_FOLLOWING_CRAWL`      | 60      | Rows of company X /following to scan per company     |
| `MAX_TEAM_PAGES_PER_COMPANY`   | 4       | Cap website team-page visits per company             |

## Handle confidence levels

Every found handle is graded and the grade is shown on the dashboard:

| Level    | What triggered it                                                        |
| -------- | ------------------------------------------------------------------------ |
| `high`   | Direct link from YC card OR company website team page, **or** bio mentions company name / website domain |
| `medium` | Profile follows the company's X account AND display-name token overlap ≥ 0.5 |
| `low`    | Display-name token overlap ≥ 2/3 AND ≥ 50 followers                      |

Anything below `low` is rejected (`handle_status='not_found'`). Each row has an `evidence` field recording which signal won, surfaced on every DM card.

## Safety rails (hard-coded)

- `DAILY_LIMIT` enforced at start and re-checked between every send
- Never sends to a `(handle, sequence)` already marked `sent`
- Only sends rows with `review_status = approved` (which is now the default for generated DMs)
- `generate_dms.py` skips founders already in `dms` and founders scored `dead`
- Playwright screenshots every send failure to `/data/screenshots/`
- Follow-ups stop the moment `replied=1`

## Files

```
project/
├── docker-compose.yml
├── .env                            GEMINI_API_KEY=...
├── inputs/
│   ├── batches.csv                 YC company list (read-only mount)
│   ├── context.md                  candidate profile
│   └── twitter_credentials.json    auth_token + ct0
├── data/
│   ├── outreach.db                 SQLite (created on first run)
│   └── screenshots/                failure screenshots
├── logs/outreach.log               tailed by the SSE stream
├── worker/
│   ├── config.py                   tunables (committed, no env)
│   ├── orchestrator.py             continuous batch loop
│   ├── run.py                      CLI
│   └── steps/                      filter, find_handles, check_activity,
│                                   generate_dms, send_dms,
│                                   check_replies, queue_followups
├── api/main.py                     FastAPI + SSE
└── dashboard/index.html            single-file UI
```

## First-time setup on a fresh VM

```bash
git clone <repo> outreach && cd outreach
cp .env.example .env && vim .env                                   # set GEMINI_API_KEY
cp inputs/twitter_credentials.json.example inputs/twitter_credentials.json
vim inputs/twitter_credentials.json                                # auth_token + ct0
docker compose up -d --build api dashboard
docker compose --profile worker run --rm worker python run.py --step filter
docker compose --profile worker up -d --build worker
```

Open port **3000**. Port 8000 stays internal; nginx proxies `/api/*` to it.

## Twitter cookies

Both cookies must come from the same logged-in session:
- `auth_token` — HttpOnly, `.x.com`
- `ct0` — CSRF token, `.x.com`

DevTools → Application → Cookies on a tab where you are logged in.

## Logs

```bash
docker compose logs -f worker
docker compose logs -f api
tail -f logs/outreach.log
```

The dashboard streams the worker log live via SSE.

## Common operational commands

```bash
# Resume after a code change (no DB wipe)
docker compose --profile worker down && docker compose --profile worker up -d --build worker

# Full reset of pipeline state (keeps filtered companies, retries everything else)
sqlite3 data/outreach.db "DELETE FROM founders; DELETE FROM dms; UPDATE companies SET batch_status='pending';"

# Nuclear reset (re-run filter from batches.csv)
rm data/outreach.db data/outreach.db-wal data/outreach.db-shm
docker compose --profile worker run --rm worker python run.py --step filter

# Free port 8000 if it's stuck
docker rm -f outreach-api 2>/dev/null && docker compose up -d api
```
