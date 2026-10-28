#!/usr/bin/env bash
# Refresh this project's results from ResearchGate's: wipe $DST_ROOT/ (results/ResearchGateEOD)
# and server-side copy $MIRROR_SRC/ (results/ResearchGate) into it. The source is ONLY read.
#
#   bash scripts/seed_from_rg.sh            # do it
#   DRY_RUN=1 bash scripts/seed_from_rg.sh  # list what would be deleted/copied, change nothing
#
# Called by launch.sh before every launch, and by scripts/midrun.sh before every midrun.
# Kept out of the wipe: runs/*/midrun/ (the history of midrun predictions — ResearchGate has
# none to replace it with). Left behind from the source: code/, app/, _pod_logs/ — not results.
# Refuses while any pod of this project is up: it reads code/ and writes runs/ and its
# restart marker under _pod_logs/.
. "$(dirname "$0")/_common.sh"

NAME_BASE="researchgateeod-pso-lssvm"
KEEP='runs/*/midrun/*'

PODS=$(curl -sS --max-time 30 https://rest.runpod.io/v1/pods \
  -H "Authorization: Bearer ${RUNPOD_API_KEY}" 2>/dev/null) || PODS=""
set +e
printf '%s' "$PODS" | NAME_BASE="$NAME_BASE" python3 -c '
import json, os, sys
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(2)
if isinstance(d, dict):
    d = d.get("data") or d.get("pods") or []
for pod in d:
    if isinstance(pod, dict) and str(pod.get("name", "")).startswith(os.environ["NAME_BASE"]) \
       and pod.get("desiredStatus") not in ("TERMINATED", "EXITED"):
        sys.exit(0)
sys.exit(1)
'
up=$?
set -e
if [ "$up" -eq 0 ]; then
  echo "seed: a ${NAME_BASE} pod is still up — NOT wiping $DST_ROOT/" >&2; exit 3
elif [ "$up" -eq 2 ]; then
  echo "FATAL: could not list pods, so cannot tell whether wiping $DST_ROOT/ is safe — retry" >&2; exit 1
fi

DRY=""; [ -n "${DRY_RUN:-}" ] && DRY="--dryrun"
# Trailing slashes matter: without them `results/ResearchGate` also matches
# `results/ResearchGateEOD`, and the rm would match any `results/ResearchGateEOD*`.
echo "seed: wiping $DST_ROOT/ (keeping $KEEP) ${DRY:+[dry run]}"
aws s3 rm $S3FLAGS "$DST_ROOT/" --recursive --only-show-errors --exclude "$KEEP" $DRY
echo "seed: copying $MIRROR_SRC/ -> $DST_ROOT/ ${DRY:+[dry run]}"
# RunPod's S3 has no object tagging (GetObjectTagging -> 500) and its multipart
# UploadPartCopy fails CompleteMultipartUpload ("part 1 is missing") on the >8 MB
# strategy files. So: no property copy, and a 5 GB threshold that keeps every object
# a single-request CopyObject.
SEED_CFG=$(mktemp -t rg-seed-awscfg-XXXX)
trap 'rm -f "$SEED_CFG"' EXIT
printf '[default]\ns3 =\n  multipart_threshold = 5GB\n' > "$SEED_CFG"
AWS_CONFIG_FILE="$SEED_CFG" aws s3 cp $S3FLAGS "$MIRROR_SRC/" "$DST_ROOT/" \
  --recursive --only-show-errors --copy-props none $DRY \
  --exclude 'code/*' --exclude 'app/*' --exclude '_pod_logs/*' --exclude "$KEEP" \
  || { echo "FATAL: seed copy failed — $DST_ROOT/ is a partial copy" >&2; exit 1; }
[ -n "$DRY" ] || echo "seed: done — $(aws s3 ls $S3FLAGS "$DST_ROOT/" --recursive --summarize | tail -2 | tr -s ' ' | tr '\n' ' ')"
