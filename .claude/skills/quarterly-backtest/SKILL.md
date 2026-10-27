---
name: quarterly-backtest
description: Run and monitor a full walk-forward rebuild of the PSO+LS-SVM backtest — 151 tickers replayed from 2021 with quarterly PSO re-tuning, sharded across 20 RunPod pods, then merged and rescored. Use when the user asks to re-run the backtest from scratch, changes a model setting (fitness, window, kernel, re-tune cadence) or the universe and wants it revalidated, or says "start over" / "rebuild". Takes ~30 minutes end to end. Not for everyday operation — use daily-run for that.
---

# Quarterly backtest (full rebuild)

Replays every session from 2021-01-04 to the latest bar for all 151 tickers,
re-tuning PSO at each quarter boundary on trailing data only. **~30 minutes end
to end, ~$2–3**, 20 parallel pods — *if every pod is deleted as soon as its shard
lands* (step 4). Measured 2026-09-17 (105 tickers through the 9/16 bar, all 20
placed at 16 vCPU / $0.48/hr): launch ~6 min (serial startup checks), each
shard's run 5–11 min, all 20 written 15 min after the first pod booted, merge +
Mongo publish ~4 min. Older figures (~3.5–6 h, $12–20) were at 100–167 tickers on
smaller placements; a shard that falls back to 2–4 vCPU will be several times slower.

Working directory is the repo root.

## Before launching — confirm the user wants this

It costs real money and wipes the current results. Confirm they want a full
rebuild rather than a daily run. Ask which settings changed and why, so the
result is attributable.

## 1. Check the config matches intent

```bash
cd /Users/dhruvdesai/Development/Stocks/ResearchGateEOD
python3 -c "
import yaml; c=yaml.safe_load(open('config/experiment.yaml'))
print('fitness  :', c['pso']['fitness'])       # rank_ic (mse reproduces the paper)
print('retune   :', c['pso']['retune'])        # quarterly
print('window   :', c['backtest']['window'], c['backtest']['window_sessions'])
print('kernel   :', c['model']['kernel'])
print('start    :', c['backtest']['start'])"
pytest tests/ -q 2>&1 | tail -3
```

All tests must pass first. `test_leakage.py` is the one that matters — it proves
no future information reaches a prediction.

## 2. Clear the previous results

Only ever `$DST_ROOT` (`s3://crimtr8kbf/results/ResearchGateEOD`), and only its
`runs/` prefix. **`data/` on the same volume is read-only market data shared
with the acquisition pipeline and must never be written to or deleted from.**
`scripts/_common.sh` exposes no root-level write target, so keep every `rm`
spelled against `$DST_ROOT/...`. Keep a local backup first.

```bash
bash -c 'set +e; . scripts/_common.sh; set +e
mkdir -p results/backup && aws s3 cp $S3FLAGS "$DST_ROOT/runs/pso_lssvm_v1/latest/" results/backup/ --recursive --quiet
aws s3 rm $S3FLAGS "$DST_ROOT/runs/" --recursive | tail -2
aws s3 ls $S3FLAGS "$SRC_BUCKET/data/ohlcv/" | head -2'
```

The last line is a deliberate check that `data/` is untouched.

The deployed dashboard is unaffected by the wipe: it reads MongoDB, which still
holds the previous run until step 5 publishes the merged rebuild over it. There
is no gap in what the UI shows.

## 2b. Stage/refresh ext tickers

The acquisition pipeline only publishes its own universe to the source volume.
Tickers outside it are staged under `results/ResearchGateEOD/` with identical relative keys and
read through `LayeredSource`. Refresh them so the replay ends on the same bar
for every ticker:

```bash
cd /Users/dhruvdesai/Development/Stocks/ResearchGateEOD
bash -c 'set -a; . ./.env; set +a; python3 -m src.fetch_ext'
```

The 151-name universe (2026-10-23) has not been checked against the source
volume yet; the old 105-name list printed `to stage …: 0`. Expect this to print
`universe 151 | on source volume N | to stage …: 151−N` and stage any names the
acquisition pipeline does not cover (SPY/QQQ count as covered via `data.source_keys`). If it
ever stages names, every staged ticker must report its last bar at the same date
the source volume carries (compare with AAPL); do not launch with failures
outstanding.

## 3. Launch 20 shards

```bash
cd /Users/dhruvdesai/Development/Stocks/ResearchGateEOD
RUN_MODE=both SHARDS=20 WATCHDOG_SEC=43200 bash scripts/launch.sh 2>&1 | grep -E "launched|placed at|no capacity at 2|all pods|some pods" | tail -25
```

20 shards × 5–6 tickers (quarterly re-tuning is ~8× the cost of tuning once:
~1,430 walk-forward fits **plus 24 × 620 PSO fits** per ticker). The 12 h watchdog
is a backstop, not a budget. Tickers are strided (`tickers[k::20]`) so history
lengths balance across shards.

Each pod unpacks the code bundle onto its own container disk
(`/opt/researchgate/app`). It must never unpack onto the shared volume: with one
volume-wide `app/`, every pod's startup `rm -rf` deleted `src/` from under the
pods placed seconds earlier (`No module named 'src'` on several shards at once).

EU-RO-1 capacity is often tight (on 2026-09-17 all 20 placed first try). If some
did not place, note which and start the retry loop:

```bash
cd /Users/dhruvdesai/Development/Stocks/ResearchGateEOD
SHARDS=20 SHARD_LIST="4 5 6 7" TRIES=80 INTERVAL=180 WATCHDOG_SEC=43200 \
  nohup bash scripts/retry_shards.sh > /tmp/retry.log 2>&1 &
```

It skips shards that are already running **or already finished**, so it is safe
to leave going.

## 4. Monitor — the reaper deletes each pod as its shard lands

**Start the reaper with the run.** Pods self-terminate again (the HTTP 403 every
pod hit from 2026-09-09 on was Cloudflare's 1010 block on the default
`Python-urllib/*` User-Agent; `bootstrap.sh` now sends its own UA, with GraphQL
`podTerminate` and `runpodctl` behind it). But 20 idle pods is real money, so
nothing here depends on that: the host-side reaper reads each pod's own log off
the volume and deletes it once the `work=` line is there — after the run, the
strategy pass and the publish, never mid-publish.

```bash
cd /Users/dhruvdesai/Development/Stocks/ResearchGateEOD && nohup scripts/reap_pods.sh --watch --until-empty > /tmp/reap.log 2>&1 &
```

It also covers the restart loop that used to cost whole rebuilds: a pod that
cannot delete itself is relaunched by RunPod and **re-ran its whole shard**,
rewriting `latest/` each pass (2026-09-17: 18 pods re-ran 2–4 times). `bootstrap.sh`
now idles rather than exits *and* keeps a marker so a restarted container reports
the original run instead of replaying it.

The monitor below then only watches progress and flags failures. Wrap in `bash -c` — zsh will not word-split `$S3FLAGS` and every aws call fails
silently, reporting 0 shards done while results sit on the volume. Keep the aws
timeouts: without them one hung call froze a monitor for 30 minutes while
finished pods billed.

```bash
bash -c '
cd /Users/dhruvdesai/Development/Stocks/ResearchGateEOD
set +e; . scripts/_common.sh; set +e +u +o pipefail
AWSX="$S3FLAGS --cli-connect-timeout 15 --cli-read-timeout 60"
SH="$DST_ROOT/runs/pso_lssvm_v1/shards/20"
prev=""; seen=" "
while true; do
  done_list=" $(aws s3 ls $AWSX "$SH/" --recursive 2>/dev/null | grep "latest/metrics.json" | sed "s|.*/shards/20/||;s|/latest.*||;s/^0//" | tr "\n" " ") "
  pods=$(curl -sS --max-time 25 https://rest.runpod.io/v1/pods -H "Authorization: Bearer $RUNPOD_API_KEY" 2>/dev/null \
    | python3 -c "import json,sys
try: print(\"\n\".join(x[\"name\"].rsplit(\"-s\",1)[1]+\" \"+x[\"id\"] for x in json.load(sys.stdin) if (x.get(\"name\") or \"\").startswith(\"researchgateeod-pso-lssvm-s\")))
except Exception: print(\"ERR\")")
  [ "$pods" = "ERR" ] && { sleep 60; continue; }
  logs=$(aws s3 ls $AWSX "$DST_ROOT/_pod_logs/" 2>/dev/null)
  while read -r s id; do
    [ -z "$id" ] && continue
    k=$(printf "%s\n" "$logs" | grep -- "-$id.log" | awk "{print \$4}" | tail -1); [ -z "$k" ] && continue
    aws s3 cp $AWSX "$DST_ROOT/_pod_logs/$k" /tmp/qb_s$s.log --quiet 2>/dev/null
    bad=$(grep -m1 -E "Traceback|No module|Killed|MemoryError|run exit=[1-9]|WATCHDOG TIMEOUT|!! bundle|!! pip" /tmp/qb_s$s.log)
    [ -n "$bad" ] && case "$seen" in *" F$s "*) ;; *) echo "FAIL s$s ($id): $bad"; seen="$seen F$s ";; esac
    case "$done_list" in *" $s "*) grep -q "run exit=0" /tmp/qb_s$s.log && {
      case "$seen" in *" D$s "*) ;; *) echo "s$s done (pod $id — the reaper takes it from here)"; seen="$seen D$s ";; esac; } ;; esac
  done <<< "$pods"
  n=$(printf "%s" "$done_list" | wc -w | tr -d " "); alive=$(printf "%s" "$pods" | grep -c .)
  line="shards $n/20 | pods $alive"; [ "$line" != "$prev" ] && { echo "$line"; prev="$line"; }
  [ "$n" -ge 20 ] && [ "$alive" -eq 0 ] && { echo "ALL 20 COMPLETE, no pods left"; exit 0; }
  [ "$n" -lt 20 ] && [ "$alive" -eq 0 ] && { echo "no pods left at $n/20 — relaunch the missing shards"; exit 0; }
  sleep 120
done'
```

The Monitor tool caps a watch at 30 minutes; a healthy run fits, otherwise re-arm
it (the loop is stateless apart from not repeating its own lines).

On a `FAIL` line the reaper deletes that pod too — a failed run is still a
finished one, its log is already on the volume, and a pod left up **blocks the
relaunch** of its own shard (`launch.sh` skips a name that is already running).
Read `/tmp/qb_s<k>.log`, fix the cause, and relaunch just that shard with
`retry_shards.sh` (`SHARD_LIST="<k>"`). To keep a failed pod alive to look at,
run the reaper with `--keep-failed`, or launch that shard with `KEEP_POD=1`.

Shard pace varies with how many quarters its tickers span and with the vCPU the
pod placed at. One straggler holding up the end is normal.

## 5. Merge

Only when all 20 are in:

```bash
cd /Users/dhruvdesai/Development/Stocks/ResearchGateEOD
set -a && . ./.env && set +a
python3 -m src.merge --shards 20 --local-out results/latest
```

It **refuses to publish partial results** to `latest/` (exit 2) — that guard is
deliberate, since a reader cannot tell 80 tickers from 100 once published. To
get an early read, `--allow-partial` routes to `latest_partial/` instead.

The merge rescores **globally** across all tickers, so accuracy and baselines
are not averages of per-shard averages.

It then **publishes to MongoDB itself** (`src.merge` → `publish(full=True)`):
every row of the run is replaced, because a rebuild changes old rows too, not
just appends. Expect `[publish] ResearchGateEOD: predictions replaced +147,469 …` (105 tickers through 2026-09-16)
after `written to s3://…/latest/`; about three minutes. The `.env` sourced above
is what supplies `MONGO_URI`.

## 5b. Confirm the dashboard switched to the rebuild

```bash
API=https://research-gate-be.vercel.app
curl -s --max-time 20 "$API/api/summary" | python3 -c "
import json,sys; d=json.load(sys.stdin); o=d['overall']; m=d.get('runMeta') or {}
print('published', d['publishedAt'], '| merged', m.get('merged_utc'), '| shards', m.get('shards_found'), '/', m.get('shards_expected'))
print('rows', d['bySource'], '| range', d['range']['start'], '->', d['range']['end'])
print('acc %.4f  edge %+.4f  n=%s' % (o['direction_accuracy'], o['edge_vs_always_up'], o['n']))"
```

`runMeta.merged_utc` present (a daily run has `mode: daily` instead) and
`publishedAt` after the merge mean the UI is on the new run; allow two minutes
for the API and edge caches. If `[publish] FAILED` appeared, the merged results
are on the volume — publish them by hand, reading S3 and writing Mongo with
nothing saved locally:

```bash
bash -c 'set -a; . ./.env; set +a; python3 -m src.strategy && python3 -m src.publish_mongo --full'
```

`src.merge` runs `src.strategy` itself just before publishing, because a rebuild
replaces every prediction and the $10k stop-loss paper trade is computed from
them. Re-run it by hand as above whenever you publish a rebuild by hand.

`--full` matters here: without it the publisher would only append rows it does
not have and leave the old run's rows in place under the same run id (a
fingerprint guard usually catches that and falls back to a full replace, but do
not rely on it).

## 6. Report

Give the pooled edge over the always-up baseline, the per-year table, and the
predicted/actual correlation, and say that the dashboard now shows the rebuild. State the baseline explicitly — accuracy alone is
meaningless, since a coin flip is the wrong benchmark and the always-up baseline
sits near 51.9% for this universe.

Current reference result (105 tickers, 2021-01-04 → 2026-09-16, merged
2026-09-17): **50.88% accuracy vs 51.89% baseline, edge −1.01pp, pred/actual
correlation 0.003 (Spearman 0.007), negative in every year except 2022
(+0.3pp).** The previous 164-name rebuild was 51.00% vs 52.39%, −1.39pp.

## Gotchas that have actually bitten

| Symptom | Cause |
|---|---|
| `exit 137` seconds in | worker count from `os.cpu_count()` (host = 191 cores), not the cgroup |
| Monitor reports 0 done while results exist | zsh not word-splitting `$AWSF`; wrap in `bash -c` |
| `Container Disk must be <= 20` | `RUNPOD_CONTAINER_DISK_GB` above the flavor cap |
| Pod id equals the volume id | greedy `sed` for `"id"`; parse JSON properly |
| Finished shards relaunching | retry loop checking "running" instead of "completed" |
| `No module named 'src'` on several shards within minutes of launch | code unpacked to one shared `app/` on the volume; each pod's `rm -rf` wiped the others' — unpack to container disk (fixed in `bootstrap.sh` 2026-09-17) |
| Several `_pod_logs/` files per pod id, extra timestamped run dirs per shard | container exited after a failed terminate and RunPod restarted it, re-running the shard. `bootstrap.sh` now idles instead, and its restart guard stops a restarted container replaying the shard; the reaper (step 4) clears the pod |
| Monitor silent for 30 min while pods finished | an aws call hung with no read timeout — keep `--cli-read-timeout` / `--cli-connect-timeout` |
| `[publish] FAILED: ServerSelectionTimeoutError` after the merge | Atlas unreachable or wrong `DB_PASSWORD` in `.env`; results are on the volume, republish per 5b |
| Dashboard shows the rebuild's metrics but the daily live log is gone | expected — the wipe discards the accumulated live rows and the rebuild replaces the run; the pre-wipe copy is in `results/backup/` |
