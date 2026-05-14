# Outreach Engine

Dockerized Twitter/X DM outreach automation with a live web dashboard.

```
docker-compose.yml
├── worker       Python — batch orchestrator, SQLite writes
├── api          FastAPI — REST + SSE over /data/outreach.db
└── dashboard    Nginx — single-page dashboard, proxies /api
```

## Run on a VM (one command)

Everything is set up. From the project root on the VM:

```bash
docker compose up -d --build api dashboard && docker compose --profile worker up -d --build worker
```

That's it. The orchestrator runs continuously: it claims 20 companies, finds handles via DuckDuckGo, checks each founder's Twitter activity, generates DMs for active ones, sends approved ones (up to 100/day), then loops. Reply detection runs every 6 h; follow-ups are queued daily for 7 days per unreplied thread.

Open the dashboard at **http://&lt;VM_IP&gt;:3000**.

## Manual one-shot steps (optional)

```bash
docker compose --profile worker run --rm worker python run.py --step filter
docker compose --profile worker run --rm worker python run.py --step find-handles
docker compose --profile worker run --rm worker python run.py --step check-activity
docker compose --profile worker run --rm worker python run.py --step generate-dms
docker compose --profile worker run --rm worker python run.py --step send --dry-run
docker compose --profile worker run --rm worker python run.py --step send
docker compose --profile worker run --rm worker python run.py --step check-replies
docker compose --profile worker run --rm worker python run.py --step queue-followups
```

## Architecture (batched, continuous)

```
filter (one-time)
  ↓
[claim next 20 filtered companies]
  ↓
find_handles    DuckDuckGo HTML → verify on x.com → x.com fallback
  ↓
check_activity  visit profile → last_tweet_at → score active|semi|dormant|dead
  ↓
generate_dms    Gemini drafts; skips 'dead' founders
  ↓
[human review in dashboard]
  ↓
send_dms        approved only, respects DAILY_LIMIT
  ↓
mark batch done, loop

every 6 h:  check_replies   scan x.com/messages, mark dms.replied=1
every 24 h: queue_followups generate sequence 2..8 for unreplied threads
```

State lives in SQLite (`/data/outreach.db`). Restart-safe — the orchestrator resumes from `companies.batch_status`.

## Tunables

Edit `worker/config.py` and rebuild:

| Constant                  | Default | Purpose                              |
| ------------------------- | ------- | ------------------------------------ |
| `BATCH_SIZE`              | 20      | Companies per micro-pipeline cycle   |
| `DAILY_LIMIT`             | 100     | DMs sent per UTC day                 |
| `FOLLOWUP_DAYS`           | 7       | Max follow-ups per thread (daily)    |
| `ACTIVITY_DORMANT_DAYS`   | 90      | `>this` since last tweet → 'dead'    |
| `SEND_DELAY_MIN/MAX`      | 180/360 | Seconds between sends                |
| `ORCHESTRATOR_IDLE_SLEEP` | 1800    | Sleep when no work to claim          |

## Safety rails (hard-coded)

- `DAILY_LIMIT` enforced both at start-of-run and re-checked between every send
- Never sends to a `(handle, sequence)` already marked `sent`
- Only sends rows with `review_status = approved`
- `generate_dms.py` skips founders already in `dms`
- `generate_dms.py` skips founders scored `dead`
- Playwright takes a screenshot to `/data/screenshots/` on every send failure
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
cp .env.example .env && vim .env                                       # GEMINI_API_KEY
cp inputs/twitter_credentials.json.example inputs/twitter_credentials.json
vim inputs/twitter_credentials.json                                    # auth_token + ct0
docker compose up -d --build api dashboard
docker compose --profile worker run --rm worker python run.py --step filter
docker compose --profile worker up -d --build worker                   # start the loop
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
