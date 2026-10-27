#!/usr/bin/env bash
# Bundle this project, push it under results/ResearchGateEOD/ on the volume, and run it on a RunPod CPU pod.
#
#   RUN_MODE=daily scripts/launch.sh            # the everyday grade -> learn -> guess
#   RUN_MODE=both SHARDS=20 scripts/launch.sh   # full walk-forward rebuild
#   RUN_MODE=both RUN_LIMIT=5 scripts/launch.sh # smoke test on 5 tickers
#   RUN_MODE=daily DRY_RUN=1 scripts/launch.sh  # show what would happen, touch nothing
#
# RUN_MODE has no default on purpose: see the guard below.
#
# The pod mounts crimtr8kbf at /workspace. Its data/ tree is READ-ONLY market data;
# the pod writes only under /workspace/$RESULTS_PREFIX (see bootstrap.sh + src/storage.py).
. "$(dirname "$0")/_common.sh"

IMAGE="${RUNPOD_IMAGE:-python:3.11-slim}"
# EU-RO-1 frequently has no large CPU instances free. Try descending sizes rather
# than failing outright; a smaller pod runs the same job, just slower.
VCPU_LADDER="${RUNPOD_VCPU_LADDER:-${RUNPOD_VCPU:-16} 8 4 2}"
FLAVORS="${RUNPOD_CPU_FLAVORS:-[\"cpu3c\",\"cpu3g\",\"cpu3m\",\"cpu5c\",\"cpu5g\",\"cpu5m\"]}"
NAME_BASE="researchgateeod-pso-lssvm"
SHARDS="${SHARDS:-1}"
DRY_RUN="${DRY_RUN:-}"

# RUN_MODE used to default to `both` — the full walk-forward replay, ~30 min of
# compute that rewrites latest/. That made a bare `bash scripts/launch.sh` (the
# env prefix forgotten) start a rebuild silently: 2026-09-22 03:08, pod
# y2y907il0hny3t ran `mode=both` when a daily was meant. There is no safe default
# for a choice between "one minute" and "rewrite everything", so state it.
RUN_MODE="${RUN_MODE:-}"
case "$RUN_MODE" in
  daily|both|backtest|predict) ;;
  "") echo "FATAL: set RUN_MODE explicitly — daily | both | backtest | predict" >&2
      echo "  daily rebuild of yesterday's guess : RUN_MODE=daily SHARDS=1 WATCHDOG_SEC=3600 bash scripts/launch.sh" >&2
      echo "  full walk-forward rebuild          : RUN_MODE=both SHARDS=20 WATCHDOG_SEC=43200 bash scripts/launch.sh" >&2
      exit 2 ;;
  *)  echo "FATAL: unknown RUN_MODE '$RUN_MODE' — expected daily | both | backtest | predict" >&2; exit 2 ;;
esac
echo "run mode: $RUN_MODE  (shards=$SHARDS)"

BUNDLE=$(mktemp -t rg-bundle-XXXX).tar.gz
# --exclude MUST precede the file list: BSD tar (macOS) otherwise treats the
# flags as filenames and aborts. This ordering works for both BSD and GNU tar.
tar czf "$BUNDLE" -C "$ROOT" \
  --exclude='__pycache__' --exclude='*.pyc' --exclude='.env' \
  src config scripts tickers.json requirements.txt
echo "bundle: $(du -h "$BUNDLE" | cut -f1)"

if [ -n "$DRY_RUN" ]; then
  # $NAME is only assigned in the launch loop below, so naming it here aborted the
  # dry run with "NAME: unbound variable" (_common.sh sets -u). Spell out what the
  # loop would build instead.
  if [ "$SHARDS" -le 1 ]; then DRY_NAMES="$NAME_BASE"
  else DRY_NAMES="${NAME_BASE}-s0 .. ${NAME_BASE}-s$((SHARDS-1))"; fi
  echo "DRY_RUN: would upload bundle -> $DST_ROOT/code/bundle.tar.gz"
  echo "DRY_RUN: would create pod(s) $DRY_NAMES in $DC (mode=$RUN_MODE) mounting $RESULTS_VOLUME_ID at /workspace (writes confined to /workspace/$RESULTS_PREFIX)"
  rm -f "$BUNDLE"; exit 0
fi

aws s3 cp $S3FLAGS "$BUNDLE" "$DST_ROOT/code/bundle.tar.gz"
aws s3 cp $S3FLAGS "$ROOT/scripts/bootstrap.sh" "$DST_ROOT/code/bootstrap.sh"
rm -f "$BUNDLE"

make_payload() {
PAYLOAD=$(cat <<JSON
{
  "name": "${NAME}",
  "computeType": "CPU",
  "cloudType": "SECURE",
  "vcpuCount": ${VCPU},
  "cpuFlavorIds": ${FLAVORS},
  "imageName": "${IMAGE}",
  "networkVolumeId": "${RESULTS_VOLUME_ID}",
  "containerDiskInGb": ${RUNPOD_CONTAINER_DISK_GB:-20},
  "volumeMountPath": "/workspace",
  "dataCenterIds": ["${DC}"],
  "dockerStartCmd": ["bash", "/workspace/${RESULTS_PREFIX}/code/bootstrap.sh"],
  "env": {
    "AWS_ACCESS_KEY_ID": "${AWS_ACCESS_KEY_ID}",
    "AWS_SECRET_ACCESS_KEY": "${AWS_SECRET_ACCESS_KEY}",
    "SOURCE_VOLUME_ID": "${SOURCE_VOLUME_ID}",
    "RESULTS_VOLUME_ID": "${RESULTS_VOLUME_ID}",
    "RESULTS_PREFIX": "${RESULTS_PREFIX}",
    "RUNPOD_S3_REGION": "${S3_REGION}",
    "RUNPOD_S3_ENDPOINT": "${S3_ENDPOINT}",
    "RUNPOD_TERMINATE_KEY": "${RUNPOD_API_KEY}",
    "KEEP_POD": "${KEEP_POD:-}",
    "RUN_MODE": "${RUN_MODE}",
    "SHARD": "${SHARD:-0}",
    "SHARDS": "${SHARDS:-1}",
    "RUN_LIMIT": "${RUN_LIMIT:-0}",
    "WORKERS": "${WORKERS:-0}",
    "WATCHDOG_SEC": "${WATCHDOG_SEC:-25200}",
    "MONGO_URI": "${MONGO_URI:-}",
    "DB_PASSWORD": "${DB_PASSWORD:-}",
    "MONGO_DB": "${MONGO_DB:-ResearchGateEOD}"
  }
}
JSON
)
}

# Cheapest-first GPU type ids, from the GraphQL catalogue. Only consulted when
# CPU capacity is exhausted — this workload is CPU-only (dense linear algebra in
# numpy), so the GPU sits idle; a GPU pod is bought purely for its host CPU and
# RAM when no CPU pod can be placed. GPU_MAX_PRICE caps the spend.
gpu_candidates() {
  curl -sS --max-time 30 -X POST "https://api.runpod.io/graphql" \
    -H "Content-Type: application/json" -H "Authorization: Bearer ${RUNPOD_API_KEY}" \
    -d '{"query":"query { gpuTypes { id secureCloud lowestPrice(input:{gpuCount:1}) { uninterruptablePrice } } }"}' \
    2>/dev/null | python3 -c '
import json, os, sys
cap = float(os.environ.get("GPU_MAX_PRICE", "0.50"))
try:
    g = json.load(sys.stdin)["data"]["gpuTypes"]
except Exception:
    sys.exit(0)
# secureCloud only: the network volume lives in a RunPod-operated datacenter,
# and community-cloud hosts cannot mount it — trying them wastes every attempt.
rows = [(x["lowestPrice"]["uninterruptablePrice"], x["id"]) for x in g
        if x.get("lowestPrice") and x["lowestPrice"].get("uninterruptablePrice")
        and x.get("secureCloud")]
for price, gid in sorted(rows):
    if price <= cap:
        print(f"{price}\t{gid}")
'
}

launch_gpu() {
  local n=0
  echo "  CPU exhausted — falling back to GPU (cheapest first, cap \$${GPU_MAX_PRICE:-0.50}/hr)" >&2
  while IFS=$'\t' read -r PRICE GID; do
    [ -z "$GID" ] && continue
    n=$((n+1)); [ "$n" -gt "${GPU_MAX_TRIES:-8}" ] && break
    echo "  trying GPU \$${PRICE}/hr: ${GID}"
    PAYLOAD=$(cat <<JSON
{
  "name": "${NAME}",
  "computeType": "GPU",
  "cloudType": "SECURE",
  "gpuTypeIds": ["${GID}"],
  "gpuCount": 1,
  "imageName": "${IMAGE}",
  "networkVolumeId": "${RESULTS_VOLUME_ID}",
  "containerDiskInGb": ${RUNPOD_CONTAINER_DISK_GB:-20},
  "volumeMountPath": "/workspace",
  "dataCenterIds": ["${DC}"],
  "dockerStartCmd": ["bash", "/workspace/${RESULTS_PREFIX}/code/bootstrap.sh"],
  "env": {
    "AWS_ACCESS_KEY_ID": "${AWS_ACCESS_KEY_ID}",
    "AWS_SECRET_ACCESS_KEY": "${AWS_SECRET_ACCESS_KEY}",
    "SOURCE_VOLUME_ID": "${SOURCE_VOLUME_ID}",
    "RESULTS_VOLUME_ID": "${RESULTS_VOLUME_ID}",
    "RESULTS_PREFIX": "${RESULTS_PREFIX}",
    "RUNPOD_S3_REGION": "${S3_REGION}",
    "RUNPOD_S3_ENDPOINT": "${S3_ENDPOINT}",
    "RUNPOD_TERMINATE_KEY": "${RUNPOD_API_KEY}",
    "KEEP_POD": "${KEEP_POD:-}",
    "RUN_MODE": "${RUN_MODE}",
    "RUN_LIMIT": "${RUN_LIMIT:-0}",
    "SHARD": "${SHARD:-0}",
    "SHARDS": "${SHARDS:-1}",
    "WORKERS": "${WORKERS:-0}",
    "WATCHDOG_SEC": "${WATCHDOG_SEC:-25200}",
    "MONGO_URI": "${MONGO_URI:-}",
    "DB_PASSWORD": "${DB_PASSWORD:-}",
    "MONGO_DB": "${MONGO_DB:-ResearchGateEOD}"
  }
}
JSON
)
    RESP=$(curl -sS -w $'\n%{http_code}' -X POST https://rest.runpod.io/v1/pods \
      -H "Authorization: Bearer ${RUNPOD_API_KEY}" -H 'Content-Type: application/json' -d "$PAYLOAD")
    CODE=$(printf '%s' "$RESP" | tail -n1); BODY=$(printf '%s' "$RESP" | sed '$d')
    if [ "$CODE" = "200" ] || [ "$CODE" = "201" ]; then
      POD_ID=$(printf '%s' "$BODY" | RESULTS_VOLUME_ID="$RESULTS_VOLUME_ID" SOURCE_VOLUME_ID="$SOURCE_VOLUME_ID" python3 -c '
import json, os, sys
try: d = json.load(sys.stdin)
except Exception: sys.exit(0)
if isinstance(d, list): d = d[0] if d else {}
pid = d.get("id", "")
if pid and pid not in (os.environ.get("RESULTS_VOLUME_ID",""), os.environ.get("SOURCE_VOLUME_ID","")):
    print(pid)
')
      if [ -n "$POD_ID" ]; then
        echo "  placed on GPU ${GID} at \$${PRICE}/hr (GPU unused; bought for host CPU)"
        return 0
      fi
    fi
  done < <(gpu_candidates)
  return 1
}

launch_one() {
POD_ID=""
for VCPU in $VCPU_LADDER; do
  make_payload
  echo "creating pod $NAME in $DC (${VCPU} vCPU) ..."
  RESP=$(curl -sS -w $'\n%{http_code}' -X POST https://rest.runpod.io/v1/pods \
    -H "Authorization: Bearer ${RUNPOD_API_KEY}" -H 'Content-Type: application/json' -d "$PAYLOAD")
  CODE=$(printf '%s' "$RESP" | tail -n1); BODY=$(printf '%s' "$RESP" | sed '$d')
  if [ "$CODE" = "200" ] || [ "$CODE" = "201" ]; then
    # Parse JSON properly. A greedy sed for "id" grabs the LAST match, which in a
    # create response is the nested networkVolume id — that produced a
    # DELETE against the wrong resource and left the real pod orphaned and billing.
    POD_ID=$(printf '%s' "$BODY" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(0)
if isinstance(d, list):
    d = d[0] if d else {}
pid = d.get("id", "")
# Never accept a volume id as a pod id.
import os
if pid and pid not in (os.environ.get("RESULTS_VOLUME_ID",""), os.environ.get("SOURCE_VOLUME_ID","")):
    print(pid)
')
    if [ -z "$POD_ID" ]; then
      echo "  created, but could not parse a pod id from the response:" >&2
      printf '%s\n' "$BODY" | head -c 500 >&2; echo >&2
      echo "  Check the RunPod console and kill it manually if it is running." >&2
      exit 1
    fi
    echo "  placed at ${VCPU} vCPU"
    break
  fi
  if printf '%s' "$BODY" | grep -q "no longer any instances available"; then
    echo "  no capacity at ${VCPU} vCPU — trying smaller" >&2
    continue
  fi
  echo "pod create failed (HTTP $CODE):" >&2; echo "$BODY" >&2
  echo "Hint: adjust RUNPOD_CPU_FLAVORS in .env (cpu3c cpu3g cpu3m cpu5c cpu5g cpu5m)" >&2
  exit 1
done
if [ -z "$POD_ID" ]; then
  if [ "${GPU_FALLBACK:-1}" = "1" ]; then
    launch_gpu || { echo "  no CPU or GPU capacity in $DC for $NAME" >&2; return 1; }
  else
    echo "  no CPU capacity in $DC at any size for $NAME (GPU fallback disabled)" >&2
    return 1
  fi
fi
printf '%s\t%s\t%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$NAME" "$POD_ID" >> "$ROOT/launched-pods.log"
echo "  launched $NAME -> ${POD_ID}"

# `RUNNING` is not proof the container started. The only reliable signal is the
# bootstrap log appearing on the volume; poll for it and kill a dead placement.
i=0
while [ $i -lt "${STARTUP_CHECKS:-12}" ]; do
  sleep "${STARTUP_POLL_SEC:-15}"; i=$((i+1))
  if aws s3 ls $S3FLAGS "$DST_ROOT/_pod_logs/" 2>/dev/null | grep -q -- "-${POD_ID}.log"; then
    echo "  $NAME confirmed started"; return 0
  fi
done
echo "  !! $NAME produced no bootstrap log — killing $POD_ID so it stops billing" >&2
curl -sS -X DELETE "https://rest.runpod.io/v1/pods/$POD_ID" -H "Authorization: Bearer ${RUNPOD_API_KEY}" -o /dev/null
return 42
}

# Never double-launch a shard that is already up (makes retries idempotent).
RUNNING_PODS_JSON=$(curl -sS --max-time 30 https://rest.runpod.io/v1/pods \
  -H "Authorization: Bearer ${RUNPOD_API_KEY}" 2>/dev/null) || RUNNING_PODS_JSON=""

# Is a pod with exactly this name already up? Substring-grepping the raw JSON
# missed the single-pod case entirely (see below), so ask properly. A list we
# could not fetch or parse means "unknown" -> launch, matching the old behaviour:
# a duplicate pod costs a placement, a skipped launch costs the whole day's run.
already_running() {
  printf '%s' "$RUNNING_PODS_JSON" | POD_NAME="$1" python3 -c '
import json, os, sys
want = os.environ["POD_NAME"]
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(1)
if isinstance(d, dict):
    d = d.get("data") or d.get("pods") or []
for pod in d:
    if isinstance(pod, dict) and pod.get("name") == want \
       and pod.get("desiredStatus") not in ("TERMINATED", "EXITED"):
        sys.exit(0)
sys.exit(1)
'
}

FAILED=0
if [ "$SHARDS" -le 1 ]; then
  # The guard used to live only in the sharded branch below, so the single-pod
  # path — every daily run — could not be made idempotent: relaunching while one
  # was up placed a second pod writing the same latest/. 2026-09-22: pods
  # y2y907il0hny3t (03:08) and 715wkv10mk82o7 (03:17), both `researchgate-pso-lssvm`.
  NAME="$NAME_BASE"
  if already_running "$NAME"; then
    echo "  $NAME already running — skipping (delete that pod first to force a relaunch)"
  else
    SHARD=0 launch_one || FAILED=1
  fi
else
  # SHARD_LIST lets a retry target only the shards that failed to place.
  LIST="${SHARD_LIST:-}"
  if [ -z "$LIST" ]; then
    s=0; while [ "$s" -lt "$SHARDS" ]; do LIST="$LIST $s"; s=$((s+1)); done
  fi
  echo "launching shards:$LIST (of $SHARDS) ..."
  for s in $LIST; do
    NAME="${NAME_BASE}-s${s}"
    if already_running "$NAME"; then
      echo "  $NAME already running — skipping"; continue
    fi
    SHARD=$s launch_one || FAILED=1
  done
fi
[ "$FAILED" -eq 0 ] && echo "all pods launched" || echo "some pods failed to launch" >&2
exit $FAILED
