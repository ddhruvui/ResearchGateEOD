# Runbook — what to type

Three skills. Copy a prompt, paste it, done.

---

## Every day (after EODHD publishes)

> **We have new data for &lt;DATE&gt;. Run the daily pipeline and monitor it.**

Or invoke it directly:

```
/daily-run
```

**What happens:** grades yesterday's stored prediction against the bar that just
landed, adds that bar to each stock's training window, records a fresh prediction
for the next session, pushes everything under `results/ResearchGateEOD/` on the
volume, and publishes it to
MongoDB so the deployed dashboard shows it a couple of minutes later. Nothing is
downloaded to this machine.

**How long:** ~40 s of compute. Most of the wait is placing a pod — EU-RO-1 is
often out of CPU, and it retries before falling back to a GPU.

**Cost:** ~$0.01 on CPU, ~$0.02 if it falls back to GPU — *provided the pod is
deleted once it publishes*. The pod deletes itself at the end of the run; every
pod from 2026-09-09 on could not, because Cloudflare 1010-blocks the default
`Python-urllib/*` User-Agent that `bootstrap.sh` was sending (fixed — it now sends
its own UA, then tries GraphQL `podTerminate` and `runpodctl`). Nothing relies on
that: `scripts/reap_pods.sh` deletes finished pods from the laptop, and the skill
runs it.

**Needs a merged rebuild.** The daily grades the forecast the last run stored. If
`results/ResearchGateEOD/runs/` is empty (e.g. after a results wipe) there is nothing
to grade — run the full rebuild first.

**Safe to repeat.** Running it twice does nothing the second time — a recorded
forecast is written once and never regenerated.

### Variants

> Did the daily run go through? Show me the live track record so far.

> We have data for &lt;DATE&gt;. Grade it, then show me tomorrow's predictions with
> today's close and the expected value.

> Run the daily for &lt;DATE&gt; and confirm the dashboard shows it.

---

## Full rebuild (only when a model setting changes)

> **Re-run the full backtest from scratch with quarterly re-tuning, and monitor it.**

Or:

```
/quarterly-backtest
```

**What happens:** clears `results/ResearchGateEOD/runs/` on the volume, checks for
ext tickers to stage, replays all 151 tickers
from 2021-01-04 across 20 parallel pods with PSO re-tuning at every quarter
boundary, then merges and rescores
globally and publishes the merged run to MongoDB, replacing the old run's rows
so the dashboard switches to the rebuild. While the pods run, the dashboard
keeps showing the previous run.

**How long:** ~30 minutes end to end. **Cost:** ~$2–3.

Measured 2026-09-17: all 20 pods placed at 16 vCPU, each shard ran 5–11 min,
merge and publish ~4 min. A pod that places on 2–4 vCPU is several times slower, and
the cost only holds because the monitor deletes each pod as soon as its shard
lands — left alone, finished pods idle and bill.

**This wipes the current results**, including the accumulated live log. It backs
up to `results/backup/` first. Do not run it casually.

### When it is actually warranted

- You changed `pso.fitness`, `pso.retune`, `backtest.window*`, `model.kernel`, or the feature set
- You changed `tickers.json`
- You want the backtest to match a rule change already applied to the daily run

### Variants

> I changed the fitness to `mse` in config/experiment.yaml — re-run the backtest
> so it matches, and tell me how it compares to rank_ic.

> Re-run the backtest but only 10 tickers first, so I can sanity-check before
> committing the full run.

> The rebuild finished — merge it, publish it, and confirm the dashboard switched
> to the new run.

---

## Reading the output

**Never quote direction accuracy on its own.** Stocks drift up, so predicting
"up" every day is right ~51.9% of the time for free (105 tickers, 2021–2026).
The number that matters is the **edge over that baseline**.

| Figure | Current | Trust it? |
|---|---|---|
| Backtest edge | **−1.01pp** over 147,046 predictions (50.88% vs 51.89%; negative every year but 2022) | Yes |
| Correlation with actuals | 0.003 | Yes — this is what "no signal" looks like |
| Live-only edge | swings wildly | **No** — see below |

The live figure is computed over a handful of sessions, and 105 same-day
predictions across correlated stocks are worth ~2–6 independent observations,
not 105. It will read +10pp one day and −10pp the next. It needs months.

---

## Dashboard

The UI is deployed. **Render** serves the React site, **Vercel** serves the API,
and both read what the pipeline published to **MongoDB Atlas** (database
`ResearchGateEOD`). Nothing has to run on this laptop.

| Piece | Repo | Runs on |
|---|---|---|
| UI | [ddhruvui/ResearchGateFE](https://github.com/ddhruvui/ResearchGateFE) | Render static site |
| API | [ddhruvui/ResearchGateBE](https://github.com/ddhruvui/ResearchGateBE) | Vercel serverless function |
| Data | this repo, `src/publish_mongo.py` | MongoDB Atlas, db `ResearchGateEOD` |

Deployed URLs (the `dashboard` skill reads them from here):

- API: `https://research-gate-be.vercel.app` — try `https://research-gate-be.vercel.app/api/health`
- UI: `https://researchgatefe.onrender.com`

> **Open the dashboard and tell me what it says.**

Or:

```
/dashboard
```

**It refreshes itself.** The daily pod publishes to Mongo right after it writes
`latest/` (`scripts/bootstrap.sh`), and `python3 -m src.merge` publishes after a
full rebuild. The API caches for 60 s and Vercel's edge for another 60 s, so a
new run is on screen within a couple of minutes.

To publish by hand — after a merge done elsewhere, or if the pod's publish step
failed:

```bash
bash -c 'set -a; . ./.env; set +a; python3 -m src.publish_mongo'
```

Safe to repeat. Graded rows are locked, so a re-publish only appends rows Mongo
does not have yet (seconds). After a rebuild add `--full`; `src.merge` already
does. A full seed (147k rows after the 105-ticker rebuild) takes about 2½ minutes.

The **stop-loss table at the bottom of the dashboard** ("$10,000 in each stock")
comes from `latest/strategy.json`, which both the pod and `src.merge` refresh
just before they publish. If that table is missing or stale while the rest of the
page is current, the strategy step is what failed — recompute and republish:

```bash
bash -c 'set -a; . ./.env; set +a; python3 -m src.strategy && python3 -m src.publish_mongo'
```

`src.strategy` only reads `latest/predictions.parquet`; it cannot change a
prediction, a grade or a metric, so it is always safe to re-run.

A **"last published N days ago" banner** means Mongo has not been written to
for more than five days — the daily run did not go through, or its publish step
failed. Check the pod log for `publishing latest/ to MongoDB` and `[publish]
FAILED`, then run the daily or republish by hand.

### Variants

> Is the dashboard showing today's run?

> The dashboard says it was last published four days ago — work out why.

> Publish the latest results to the dashboard.

---

## Something went wrong

| Symptom | What it means | Fix |
|---|---|---|
| `nothing to do` | already processed, or vendor hasn't published | Not a failure. Check the source's newest bar |
| `to grade : <151` | stored forecast file was clobbered (or, if any ext tickers are ever staged again, they went stale) | `python3 -m src.merge --shards 20` to restore; `python3 -m src.fetch_ext` to refresh staged names |
| `no prior predictions.parquet … run a full backtest first` | no rebuild has merged since results were cleared | Run the full rebuild |
| `!! TERMINATION NOT CONFIRMED`, pod still listed after the run | every rung of the terminate ladder refused; the pod is idling and billing | `scripts/reap_pods.sh` (or `--watch --until-empty` alongside a run). A 403 here means the Cloudflare UA block is back — check the User-Agent in `bootstrap.sh` |
| `No module named 'src'` on several shards right after launch | pods shared one unpack dir on the volume and wiped each other's code | Fixed in `scripts/bootstrap.sh` (unpacks to container disk); if it recurs, check nothing unpacks under `/workspace` |
| Several `_pod_logs/` files for one pod id; extra run dirs per shard | a pod exited after a failed terminate and RunPod restarted it, re-running the job | Fixed twice over: `bootstrap.sh` idles instead of exiting, and its `.ran-<pod>` marker stops a restarted container replaying the job. Reap the pod |
| `run exit=137` | out of memory | Should not recur; report it if it does |
| `no CPU or GPU capacity` | EU-RO-1 full | Retry in a few minutes; volumes are pinned to that datacenter |
| Monitor says 0 done but results exist | zsh word-splitting bug | Monitor commands must be wrapped in `bash -c` |
| `[publish] FAILED` in the pod log | Mongo unreachable, or `MONGO_URI`/`DB_PASSWORD` wrong in `.env` | Results are on the volume; republish by hand with `python3 -m src.publish_mongo` |

---

## Hard rules

- **`data/` on `crimtr8kbf` (pinned in `src/config.py`) is read-only.** Market
  data, shared with the acquisition pipeline. Never written, never deleted from.
  Everything this project writes goes under `results/ResearchGateEOD/` on the same
  volume (`RESULTS_PREFIX`; `$DST_ROOT` in the scripts) — including any ext
  tickers `python3 -m src.fetch_ext` stages (none are needed for the current
  universe; `--dry-run` lists them).
- **A graded verdict is locked.** Prices get restated; the record does not change.
- **Live and replayed rows are tagged** (`source` = `live` / `backtest`) so a real
  forward track record stays separable from a rehearsal.

Full method, deviations from the paper, and cost tables: [README.md](README.md).
