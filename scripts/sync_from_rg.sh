#!/usr/bin/env bash
# Morning sync: once ResearchGate's daily has finished, copy its results into this project and
# publish them to MongoDB ResearchGateEOD. No pod — ResearchGate already did the work.
#
#   bash scripts/sync_from_rg.sh            # wait for ResearchGate, copy, publish
#   DRY_RUN=1 bash scripts/sync_from_rg.sh  # same checks; copy lists only, publish prints only
#   WAIT_MIN=0 bash scripts/sync_from_rg.sh # do not wait
#
# Run by .github/workflows/sync-from-researchgate.yml at 06:00 ET. ResearchGate's daily starts
# at 04:30 ET and wrote at 04:38 on 2026-10-28; copying while it runs could take a half-written
# latest/, so this waits until BOTH hold:
#   - no researchgate-pso-lssvm pod is up (its strategy + Mongo publish come after run_meta), and
#   - ResearchGate's latest/run_meta.json is stamped today (New York date),
# polling every POLL_SEC for up to WAIT_MIN minutes. If ResearchGate made nothing new today
# (weekend, holiday, no new bar) it syncs anyway once no pod is up — the copy is then the same
# results again. If a ResearchGate pod is still up at the deadline it copies nothing and fails.
# Reads results/ResearchGate and the pod list only; writes only results/ResearchGateEOD and
# the ResearchGateEOD database.
. "$(dirname "$0")/_common.sh"

RG_POD="researchgate-pso-lssvm"     # ResearchGate's pods; ours are researchgateeod-pso-lssvm
RG_META="$MIRROR_SRC/runs/pso_lssvm_v1/latest/run_meta.json"
WAIT_MIN="${WAIT_MIN:-90}"
POLL_SEC="${POLL_SEC:-300}"
TODAY="$(TZ=America/New_York date +%F)"

# 0 = a ResearchGate pod is up, 1 = none, 2 = could not tell.
rg_pod_up() {
  curl -sS --max-time 30 https://rest.runpod.io/v1/pods \
    -H "Authorization: Bearer ${RUNPOD_API_KEY}" 2>/dev/null | RG_POD="$RG_POD" python3 -c '
import json, os, sys
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(2)
if isinstance(d, dict):
    d = d.get("data") or d.get("pods") or []
for pod in d:
    if isinstance(pod, dict) and str(pod.get("name", "")).startswith(os.environ["RG_POD"]) \
       and pod.get("desiredStatus") not in ("TERMINATED", "EXITED"):
        sys.exit(0)
sys.exit(1)
'
}

# New York date of ResearchGate's latest run, or empty.
rg_run_date() {
  aws s3 cp $S3FLAGS "$RG_META" - 2>/dev/null | python3 -c '
import json, sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
try:
    s = json.load(sys.stdin)["stamp"]
    t = datetime.strptime(s, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    print(t.astimezone(ZoneInfo("America/New_York")).date())
except Exception:
    pass
'
}

# rg_run_date hides read errors (a missing stamp just means "not today"), so check once up front
# that the object is readable at all — otherwise an S3 client/credential problem looks like
# "no run today" for WAIT_MIN minutes and only surfaces at the wipe.
if ! err=$(aws s3 cp $S3FLAGS "$RG_META" - 2>&1 >/dev/null); then
  echo "::error::cannot read $RG_META — $err" >&2; exit 1
fi

deadline=$(( $(date +%s) + WAIT_MIN * 60 ))
while :; do
  set +e; rg_pod_up; up=$?; set -e
  ran="$(rg_run_date || true)"
  echo "$(TZ=America/New_York date +%H:%M) ET: ResearchGate pod up=$([ $up -eq 0 ] && echo yes || { [ $up -eq 1 ] && echo no || echo unknown; }), latest run dated ${ran:-?} (today $TODAY)"
  if [ "$up" -eq 1 ] && [ "$ran" = "$TODAY" ]; then
    break
  fi
  if [ "$(date +%s)" -ge "$deadline" ]; then
    if [ "$up" -eq 1 ]; then
      echo "::warning::ResearchGate has no run dated $TODAY (weekend, holiday or no new bar?) — syncing its latest anyway"
      break
    fi
    echo "::error::ResearchGate still running (or pods unlistable) after ${WAIT_MIN} min — not copying a possibly half-written run" >&2
    exit 1
  fi
  sleep "$POLL_SEC"
done

echo "== copy ResearchGate results -> ResearchGateEOD"
set +e; bash "$HERE/seed_from_rg.sh"; rc=$?; set -e
if [ "$rc" -ne 0 ]; then
  echo "::error::copy failed (exit $rc; 3 = a ResearchGateEOD pod is up)" >&2; exit 1
fi

echo "== publish to MongoDB ResearchGateEOD"
cd "$ROOT"
python3 -m src.publish_mongo ${DRY_RUN:+--dry-run}
