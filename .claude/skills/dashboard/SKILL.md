---
name: dashboard
description: Check, refresh, or debug the deployed outcome dashboard — the Render UI (ResearchGateFE) over the Vercel API (ResearchGateBE) over MongoDB Atlas, fed by src/publish_mongo.py. Use when the user says "open the dashboard", "is the UI up", "publish the results to the dashboard", "the dashboard looks stale", or reports the "last published N days ago" banner. Not for launching pipeline runs — use daily-run or quarterly-backtest for those.
---

# Dashboard

Three pieces, none of them on this laptop:

| Piece | Repo | Runs on |
|---|---|---|
| UI (React/Vite) | github.com/ddhruvui/ResearchGateFE | Render static site |
| API (Express) | github.com/ddhruvui/ResearchGateBE | Vercel serverless function |
| Data | this repo, `src/publish_mongo.py` | MongoDB Atlas, database `ResearchGateEOD` |

The pipeline publishes `runs/pso_lssvm_v1/latest/` to Mongo — `scripts/bootstrap.sh`
after a daily run, `src.merge` after a rebuild. The API reads Mongo with a 60 s
cache (plus 60 s at Vercel's edge). The UI is a static bundle that calls the API
at the `VITE_API_BASE` it was built with.

Deployed URLs (also in RUNBOOK.md → Dashboard):

- UI: `https://researchgatefe.onrender.com` — open this in the Browser pane to see it rendered
- API: `https://research-gate-be.vercel.app`

Working directory is the repo root (`/Users/dhruvdesai/Development/Stocks/ResearchGateEOD`).

## 1. Is it showing the current run? — check before doing anything

```bash
API=https://research-gate-be.vercel.app          # RUNBOOK.md → Dashboard
curl -s --max-time 20 "$API/api/health"
```

```json
{"ok":true,"runId":"pso_lssvm_v1","db":"ResearchGateEOD","rows":220929,
 "publishedAt":"2026-09-04T17:27:05+00:00","forSession":"2026-09-04",
 "origin":"s3://crimtr8kbf/results/ResearchGateEOD/runs/pso_lssvm_v1/latest"}
```

Compare `forSession` with what the volume holds:

```bash
bash -c '. scripts/_common.sh; aws s3 cp $S3FLAGS "$DST_ROOT/runs/pso_lssvm_v1/latest/run_meta.json" -'
```

Same `for_session` and matching `rows_total` → **there is nothing to do.**
Report the numbers and stop. Do not republish "just in case"; it is harmless
but it is not what the user asked.

## 2. Republish when Mongo is behind the volume

```bash
bash -c 'set -a; . ./.env; set +a; python3 -m src.publish_mongo'
```

Seconds when only a day's rows are new; ~3 min for a first seed of 220k rows.
Idempotent — graded rows are locked, so it appends only what Mongo lacks. After a
rebuild add `--full` (`src.merge` already does). Then re-check §1; allow two
minutes for the API and edge caches to expire.

The bottom card, **"$10,000 in each stock, with a stop loss"**, is fed separately
by `latest/strategy.json` (`/api/strategy`). If that card is missing or behind
while the rest of the page is current, recompute it first and then republish:

```bash
bash -c 'set -a; . ./.env; set +a; python3 -m src.strategy && python3 -m src.publish_mongo'
```

`src.strategy` is a pure reader of `latest/predictions.parquet` — it cannot
change a prediction, a grade or a metric, so it is always safe to re-run.

`MONGO_URI` / `DB_PASSWORD` / `MONGO_DB` come from `.env` (gitignored). The pod
gets the same three keys through `scripts/launch.sh`, so a daily run publishes on
its own; the pod log shows `publishing latest/ to MongoDB` and the `[publish]`
lines. No `publishing` line at all means `MONGO_URI` was empty when launched.

## 3. Without the deployed API — local dev, or before the user has deployed

The two repos are siblings of this one:

```bash
cd /Users/dhruvdesai/Development/Stocks/ResearchGateBE && npm install && npm run dev   # 8891, reads Atlas
cd /Users/dhruvdesai/Development/Stocks/ResearchGateFE && npm install && npm run dev   # 5273, proxies /api
```

The backend needs `.env` with the same three Mongo keys (`.env.example` lists
them). It reads the same Atlas database, so it shows exactly what Render will.
Open `http://localhost:5273` in the Browser pane and screenshot it — do not ask
the user to check for you. (For the deployed site, open the Render URL above the
same way.)

## 4. The banners

**"Last published N days ago"** — `publishedAt` is more than five days old. The
daily run did not go through, or its publish step failed. Look for `[publish]
FAILED` in the newest `results/ResearchGateEOD/_pod_logs/` entry, fix per the table, then §2.

**"The API did not answer"** — the UI could not reach the API at all.

| Finding | Cause | Fix |
|---|---|---|
| UI error names `VITE_API_BASE` as empty or wrong | not set on Render at build time | set it in Render → Environment, redeploy |
| `/api/health` → 500 | Vercel is missing `MONGO_URI`/`DB_PASSWORD`, or Atlas Network Access blocks Vercel | Vercel → Settings → Environment Variables; Atlas → Network Access → allow `0.0.0.0/0` (serverless has no fixed IP) |
| `/api/health` → 404 "has not been published" | Mongo has no `runs` doc for that run id | §2 |
| `[publish] FAILED: ServerSelectionTimeoutError` | Atlas unreachable from the pod, or wrong password | check `.env`; retry §2 from the laptop |
| `[publish] FAILED: ... <db_password>` | `DB_PASSWORD` empty | fill it in `.env` |

## 5. Endpoints

| Endpoint | Returns |
|---|---|
| `/api/health` | row counts, `publishedAt`, `forSession`, S3 origin |
| `/api/summary` | overall metrics, live-only block, per-year table, date range |
| `/api/equity?costBps=N&tickers=A,B` | equity curve points + stats (0/1/5/10 bps are pre-computed; anything else scans 220k rows, ~10–25 s) |
| `/api/tickers` | per-ticker leaderboard |
| `/api/next-session` | live predictions, sorted, with the up/down split |
| `/api/predictions?ticker=A,B&from=&to=&source=live&page=1&limit=500` | graded rows, paginated |
| `/api/runs` | every published run |

## 6. Reporting what is on screen

**Never quote direction accuracy on its own.** The always-up baseline is ~52%
for free; the number that matters is the edge over it — **about −1.1pp** across
the backtest. Read the exact current value off `edge_vs_always_up` in
`/api/summary` rather than repeating a figure from here; it drifts as live rows
accumulate into the pooled total.

The live-only figure on the next-session panel is computed over a handful of
sessions, and 105 same-day predictions across correlated names are worth ~2–6
independent observations. Say plainly that it means nothing yet.
