#!/usr/bin/env bash
# Keep trying to place shards that lost the capacity race. Idempotent: skips any
# shard whose pod is already running. Exits once every shard is up (or attempts
# are exhausted).
#   SHARDS=10 SHARD_LIST="6 7 8 9" scripts/retry_shards.sh
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
. "$(dirname "$0")/_common.sh"
SHARDS="${SHARDS:-10}"
WANT="${SHARD_LIST:?set SHARD_LIST, e.g. \"6 7 8 9\"}"
TRIES="${TRIES:-40}"
INTERVAL="${INTERVAL:-180}"

for i in $(seq 1 "$TRIES"); do
  still=""
  running=$(curl -sS --max-time 30 https://rest.runpod.io/v1/pods \
    -H "Authorization: Bearer $(grep '^RUNPOD_API_KEY=' "$ROOT/.env" | cut -d= -f2-)" 2>/dev/null)
  # A shard that has already WRITTEN results needs no relaunch. Checking only
  # "is it running" re-spawned finished shards, which recompute identical work
  # and steal capacity from the ones that never placed.
  done_list=$(aws s3 ls $S3FLAGS "$DST_ROOT/runs/${RUN_ID:-pso_lssvm_v1}/shards/$(printf '%02d' "$SHARDS")/" \
    --recursive 2>/dev/null | grep "latest/metrics.json" \
    | sed "s|.*/shards/[0-9]*/||;s|/latest.*||" | sed "s/^0*//" | tr "\n" " ")
  for s in $WANT; do
    case " $done_list " in *" ${s} "*) continue ;; esac
    printf '%s' "$running" | grep -q "researchgateeod-pso-lssvm-s${s}\"" || still="$still $s"
  done
  if [ -z "$still" ]; then echo "all requested shards are running"; exit 0; fi
  echo "attempt $i: still need shards$still"
  # launch.sh no longer defaults RUN_MODE; a shard retry is always the full replay.
  SHARDS="$SHARDS" SHARD_LIST="$still" WATCHDOG_SEC="${WATCHDOG_SEC:-43200}" \
    RUN_MODE="${RUN_MODE:-both}" \
    bash "$ROOT/scripts/launch.sh" 2>&1 | grep -E "launched|already running|no capacity at 2" | tail -6
  sleep "$INTERVAL"
done
echo "gave up after $TRIES attempts; shards still missing:$still" >&2
exit 1
