# Outreach Engine

Dockerized Twitter/X DM outreach automation with a live web dashboard.

```
docker-compose.yml
├── worker       Python — pipeline steps, SQLite writes
├── api          FastAPI — REST + SSE over /data/outreach.db
└── dashboard    Nginx — single-page dashboard, proxies /api
```

## First-time setup

```bash
cp .env.example .env                                          # add GEMINI_API_KEY
cp inputs/twitter_credentials.json.example inputs/twitter_credentials.json
# fill in auth_token + ct0 cookies from your logged-in x.com session

docker compose build
docker compose up -d                                          # api + dashboard
```

Open the dashboard at **http://localhost:3000** (or `http://<VM_IP>:3000`).

## Running the pipeline

The worker uses `profiles: [worker]` and does not auto-start — invoke each step manually:

```bash
docker compose --profile worker run --rm worker python run.py --step filter
docker compose --profile worker run --rm worker python run.py --step find-handles
docker compose --profile worker run --rm worker python run.py --step generate-dms
# Review DMs on the dashboard, click Approve
docker compose --profile worker run --rm worker python run.py --step send
docker compose --profile worker run --rm worker python run.py --step send --dry-run

# Or the full pipeline minus send:
docker compose --profile worker run --rm worker python run.py --all
```

## Safety constraints (hard-coded)

- `send_dms.py`: aborts when `daily_send_log` hits **20/day**
- Never sends to a handle already marked `sent`
- Only sends rows with `review_status = approved`
- `generate_dms.py`: skips founders that already have a DM row
- Playwright takes a screenshot to `/data/screenshots/` on every send failure

## Files

```
project/
├── docker-compose.yml
├── .env                            GEMINI_API_KEY=...
├── inputs/
│   ├── batches.csv                 YC company list (mounted read-only)
│   ├── context.md                  candidate profile
│   └── twitter_credentials.json    auth_token + ct0
├── data/
│   ├── outreach.db                 SQLite (created on first run)
│   └── screenshots/                failure screenshots
├── logs/outreach.log               tailed by the SSE stream
├── worker/  (run.py + steps/)
├── api/main.py                     FastAPI app
└── dashboard/index.html            single-file UI
```

## VM deployment

```bash
git clone <repo> outreach && cd outreach
cp .env.example .env && vim .env                     # GEMINI_API_KEY (from aistudio.google.com)
cp inputs/twitter_credentials.json.example inputs/twitter_credentials.json
vim inputs/twitter_credentials.json
docker compose up -d --build
```

Open port **3000** on your VM. Port 8000 stays internal; nginx proxies `/api/*` to it.

## Notes on Twitter cookies

Both cookies need to be from the same logged-in session:
- `auth_token` — HttpOnly cookie, scoped to `.x.com`
- `ct0` — CSRF token, scoped to `.x.com`

Grab them from DevTools → Application → Cookies on a tab where you are logged in.
