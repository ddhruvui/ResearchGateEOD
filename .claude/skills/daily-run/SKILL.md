---
name: daily-run
description: Run and monitor the everyday PSO+LS-SVM pipeline — grade yesterday's stored prediction against the bar that just landed, learn from it, and record a new prediction for the next session. Use when the user says "run the daily", "we have new data for <date>", "grade yesterday", or asks for today's prediction. Not for full rebuilds — use quarterly-backtest for those.
---

# Daily run

One pass: **grade → learn → guess**. Never replays history. ~1 min of compute for
151 tickers; the slow part is placing a pod.

Working directory is the repo root (`/Users/dhruvdesai/Development/Stocks/ResearchGateEOD`).

## 0. Ext tickers — check staging for the current universe

The universe changed to 151 names on 2026-10-23 (70 added); source-volume
coverage for the new names has not been checked yet. SPY and QQQ come
through the `data.source_keys` override in `config/experiment.yaml`. Run
`python3 -m src.fetch_ext` (see quarterly-backtest step 2) and stage anything
it reports before relying on this step being a no-op. Only if `tickers.json` gains a name the acquisition pipeline does not
publish, stage it first with
`bash -c 'set -a; . ./.env; set +a; python3 -m src.fetch_ext'` — staged bars do
not refresh on their own, so from then on this step runs before every daily.

## 1. Confirm the new bar actually landed

Do not skip this. The run is a no-op without it, and you will waste a pod.
Bars are published by the DataAcquistion repo to `data/ohlcv/<T>.json` on
`crimtr8kbf`; read them through this repo's `_common.sh` (read-only `$SRC_BUCKET`):

```bash
cd /Users/dhruvdesai/Development/Stocks/ResearchGateEOD && bash -c 'set +e; . scripts/_common.sh; set +e
for t in AAPL MSFT AVGO; do aws s3 cp $S3FLAGS "$SRC_BUCKET/data/ohlcv/$t.json" /tmp/chk_$t.json --quiet; done
python3 -c "
import json
for t in (\"AAPL\",\"MSFT\",\"AVGO\"):
    d=json.load(open(f\"/tmp/chk_{t}.json\"))
    print(t, [r[\"date\"] for r in d[-3:]])
"'
```

The newest date must be **later** than the `as_of` of the stored prediction
(see step 2). If it is not, stop and tell the user the vendor has not published
yet — do not launch anything.

## 2. Check what is already recorded

```bash
bash -c 'set +e; . scripts/_common.sh; set +e
aws s3 cp $S3FLAGS "$DST_ROOT/runs/pso_lssvm_v1/latest/next_session.parquet" /tmp/ns.parquet --quiet'
python3 -c "
import pandas as pd; d=pd.read_parquet('/tmp/ns.parquet')
print(f'{len(d)} stored guesses, as_of {d[\"as_of\"].astype(str).unique()[0]} -> for {d[\"for_session\"].astype(str).unique()[0]}')"
```

**If this shows fewer than 151 rows, stop.** A partial file means a limited run
overwrote it. Restore with `python3 -m src.merge --shards 20` before continuing.

**If the file does not exist at all, stop.** No rebuild has merged (e.g. after a
results wipe), so there is nothing to grade and `src.daily` would exit 1 with
`no prior predictions.parquet; run a full backtest first`. Tell the user a
`/quarterly-backtest` is needed first — do not launch.

## 3. Check nothing is already running

```bash
cd /Users/dhruvdesai/Development/Stocks/ResearchGateEOD && bash -c 'set +e; . scripts/_common.sh; set +e
curl -sS --max-time 25 https://rest.runpod.io/v1/pods -H "Authorization: Bearer $RUNPOD_API_KEY" \
 | python3 -c "import json,sys; print([x[\"name\"] for x in json.load(sys.stdin) if \"researchgate\" in (x.get(\"name\") or \"\")] or \"none\")"'
```

A pod already running will do the work. Since 2026-09-22 `launch.sh` also checks
this itself for the single-pod path and prints `already running — skipping`, so a
second `/daily-run` no longer places a duplicate pod — but check anyway, so you
watch the log of the pod that is actually doing the work.

`RUN_MODE` is mandatory: a bare `bash scripts/launch.sh` now exits 2 instead of
silently starting the full walk-forward rebuild.

## 4. Launch

```bash
cd /Users/dhruvdesai/Development/Stocks/ResearchGateEOD
RUN_MODE=daily SHARDS=1 WATCHDOG_SEC=3600 bash scripts/launch.sh 2>&1 | grep -vE "^Completed|upload:" | tail -10
```

CPU capacity in EU-RO-1 is frequently exhausted. The launcher tries 16→8→4→2
vCPU, then falls back to the **cheapest secure-cloud GPU** (live-priced, capped
at `GPU_MAX_PRICE`, default $0.50/hr). The GPU is never used for compute — it is
bought for the host CPU. If it reports no capacity at all, retry every ~40 s;
placement usually succeeds within a few attempts.

Note the pod id from `launched researchgateeod-pso-lssvm -> <id>`.

## 5. Monitor

Use the Monitor tool, wrapped in `bash -c` (zsh does not word-split `$AWSF`,
which silently breaks every aws call and reports a plausible-looking zero):

```bash
bash -c '
cd /Users/dhruvdesai/Development/Stocks/ResearchGateEOD
set +e
export AWS_ACCESS_KEY_ID=$(grep "^AWS_ACCESS_KEY_ID=" .env | cut -d= -f2-)
export AWS_SECRET_ACCESS_KEY=$(grep "^AWS_SECRET_ACCESS_KEY=" .env | cut -d= -f2-)
AWSF="--region eu-ro-1 --endpoint-url https://s3api-eu-ro-1.runpod.io"
prev=""
for i in $(seq 1 90); do
  K=$(aws s3 ls $AWSF s3://crimtr8kbf/results/ResearchGateEOD/_pod_logs/ 2>/dev/null | grep POD_ID | awk "{print \$4}" | tail -1)
  if [ -n "$K" ]; then
    aws s3 cp $AWSF "s3://crimtr8kbf/results/ResearchGateEOD/_pod_logs/$K" /tmp/dr.log --quiet 2>/dev/null
    cur=$(grep -E "resources:|to grade|graded |LIVE-only|next guess|run exit|Killed|Traceback|nothing to do|publishing latest|\[publish\]|mongo publish failed|work=|TERMINATED via" /tmp/dr.log 2>/dev/null)
    if [ "$cur" != "$prev" ]; then
      diff <(printf "%s\n" "$prev") <(printf "%s\n" "$cur") 2>/dev/null | grep "^>" | sed "s/^> //"
      prev="$cur"
    fi
    grep -q -E "TERMINATED via|TERMINATION NOT CONFIRMED" /tmp/dr.log 2>/dev/null && { echo "RUN FINISHED"; exit 0; }
  fi
  sleep 20
done'
```

Substitute the real pod id for `POD_ID`. The grep must cover failure signatures
(`Killed`, `Traceback`), not just the happy path — silence otherwise looks
identical to a crash. `run exit=0` is not the end: the pod then publishes
`latest/` to MongoDB for the dashboard (`publishing latest/ to MongoDB` …
`[publish] ResearchGateEOD: predictions appended +151 …`) and only then
tries to self-terminate, which is what the loop waits for.

**Confirm the pod is gone.** The log ends in `TERMINATED via rest: HTTP 204` and
the pod deletes itself. The 403s that made every run since 2026-09-09 need a
manual delete were Cloudflare's 1010 block on the default `Python-urllib/*`
User-Agent; `bootstrap.sh` now sends its own UA and falls back to GraphQL
`podTerminate` and `runpodctl`, naming whichever rung worked.

If the log ends in `!! TERMINATION NOT CONFIRMED` instead, the pod is idling and
billing, and the reaper clears it — one pass is enough once `work=` is in the log:

```bash
cd /Users/dhruvdesai/Development/Stocks/ResearchGateEOD && scripts/reap_pods.sh
```

It reads each pod's own log off the volume and deletes only pods whose `work=`
line is there, so it is safe to run at any time, including mid-run. To not have
to think about it at all, start it in the background right after the launch:

```bash
cd /Users/dhruvdesai/Development/Stocks/ResearchGateEOD && nohup scripts/reap_pods.sh --watch --until-empty > /tmp/reap.log 2>&1 &
```

Either way, end with no pods left:

```bash
cd /Users/dhruvdesai/Development/Stocks/ResearchGateEOD && bash -c 'set +e; . scripts/_common.sh; set +e
curl -sS --max-time 25 https://rest.runpod.io/v1/pods -H "Authorization: Bearer $RUNPOD_API_KEY" \
 | python3 -c "import json,sys; print([x[\"name\"] for x in json.load(sys.stdin)] or \"none\")"'
```

Expect `none`.

## 6. Verify on the dashboard and report

The pod refreshes the stop-loss paper trade and publishes `latest/` to MongoDB
itself (`scripts/bootstrap.sh` → `python -m src.strategy` → `python -m
src.publish_mongo`), so the deployed UI has the run a couple of minutes after
`run exit=0`. Verify there — nothing needs downloading:

```bash
API=https://research-gate-be.vercel.app
curl -s --max-time 20 "$API/api/summary" | python3 -c "
import json,sys; d=json.load(sys.stdin); o=d['overall']; lo=d.get('liveOnly') or {}
print('published', d['publishedAt'], '| for', d['forSession'], '| asOf', d['asOf'])
print('rows', d['bySource'], '| range', d['range']['start'], '->', d['range']['end'])
print('acc %.4f  edge %+.4f  n=%s' % (o['direction_accuracy'], o['edge_vs_always_up'], o['n']))
print('live-only n=%s acc=%.4f edge=%+.4f' % (lo.get('n'), lo.get('direction_accuracy', 0), lo.get('edge_vs_always_up', 0)))"
curl -s --max-time 20 "$API/api/next-session" | python3 -c "
import json,sys; d=json.load(sys.stdin)
print(f'next: {len(d[\"rows\"])} rows for {d[\"forSession\"]}, {d[\"up\"]} up / {d[\"down\"]} down')"
curl -s --max-time 20 "$API/api/strategy" | python3 -c "
import json,sys; d=json.load(sys.stdin); r=d['rollup']
print('stop-loss:', d['meta']['stamp'], '|', r['nTickers'], 'tickers,', r['startCapital'], 'each')
for k,b in sorted(r['byStop'].items()):
    print('  %s stop -> %s (%+.1f%%), %s of %s tickers ahead' % (
        k, format(b['end'], ',.2f'), b['totalReturn']*100, b['winners'], r['nTickers']))"
```

`forSession` must be the session after the bar that just landed, `bySource.live`
must have grown by the graded count, and `publishedAt` must be later than the
pod's `stamp`. The API caches for 60 s and Vercel's edge for another 60 s, so
re-check once if it looks a run behind.

If the pod log shows `[publish] FAILED`, or no `publishing latest/` line at all
(`MONGO_URI` was empty in `.env` when it was launched), the results are safely
on the volume. Publish them from here — this reads S3 and writes Mongo without
saving anything locally:

```bash
bash -c 'set -a; . ./.env; set +a; python3 -m src.strategy && python3 -m src.publish_mongo'
```

`src.strategy` only reads `latest/predictions.parquet`, so re-running it can
never disturb a grade or a metric. Its `meta.stamp` should be from this run; if
`/api/strategy` 404s or its stamp is old while `/api/summary` is current, that
step is what failed.

Report: rows graded, live-row count, the next session's date and up/down split,
and that the dashboard shows it.

**Always caveat the live-only accuracy.** It is computed over very few sessions,
and same-day predictions across 105 stocks are ~2–6 independent observations,
not 105 — measured cross-sectional correlation is 0.08–0.25. It swings by tens
of points for weeks. Quote the backtest figure (currently **−1.0pp edge over the
always-up baseline** — 50.88% vs 51.89%, 105-ticker rebuild through the
2026-09-16 bar) as the real number, and say plainly that the live figure means
nothing yet.

## Expected outcomes

| Log line | Meaning |
|---|---|
| `graded 151 · new guesses 151 · errors 0` | success |
| `nothing to do` | already processed, or vendor has not published. Correct, not a failure |
| `run exit=137` | OOM. Should not recur (worker count is cgroup-aware), but report it |
| `[publish] ResearchGateEOD: predictions appended +151 …` | the dashboard has the run |
| `[publish] FAILED: …` | run succeeded, dashboard stale — republish from the laptop (step 6). `ServerSelectionTimeoutError` from the pod usually means Atlas Network Access does not allow it |
| no `publishing latest/` line after `run exit=0` | `MONGO_URI` was empty in `.env` at launch — republish from the laptop |
| `to grade : <151` | stored file was clobbered — re-merge before rerunning; a handful short is also what stale staged ext tickers look like, if any are ever staged again (step 0) |
| `no prior predictions.parquet … run a full backtest first` | no rebuild has merged — step 2 should have caught it; run `/quarterly-backtest` |
| `TERMINATED via rest: HTTP 204` | the pod deleted itself and is gone — nothing to clean up |
| `!! TERMINATION NOT CONFIRMED` | every rung of the terminate ladder refused; the pod is idling and billing — `scripts/reap_pods.sh` (step 5). A 403 here means the Cloudflare UA block is back, so check the User-Agent in `bootstrap.sh` |
| `RESTART DETECTED — this pod already ran …` | RunPod relaunched a container that could not delete itself; the restart guard stopped the job re-running. Reap the pod |
