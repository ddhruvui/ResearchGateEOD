#!/usr/bin/env bash
# Sourced by launch.sh / results.sh / retry_shards.sh. Loads .env and sets the S3 flags.
#
# ONE volume since 2026-09-16: crimtr8kbf holds the market data under data/
# (READ-ONLY, shared with the acquisition pipeline) and everything this project
# writes under RESULTS_PREFIX (results/ResearchGateEOD). Two names come out of here:
#   SRC_BUCKET  the volume root — only ever `aws s3 ls` or `cp` FROM it
#   DST_ROOT    s3://<volume>/<RESULTS_PREFIX> — the ONLY write / rm target
# Never add a write path against SRC_BUCKET, and never `aws s3 rm` anything that
# is not spelled "$DST_ROOT/...".
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
ENV_FILE="$ROOT/.env"

[ -f "$ENV_FILE" ] || { echo "missing $ENV_FILE — copy .env.example and fill it in" >&2; exit 1; }
# Load .env WITHOUT clobbering anything the caller already exported, so
# `RUNPOD_VCPU=8 scripts/launch.sh` actually takes effect.
_saved=$(export -p)
set -a; . "$ENV_FILE"; set +a
eval "$_saved"; unset _saved

: "${AWS_ACCESS_KEY_ID:?set in .env}"
: "${AWS_SECRET_ACCESS_KEY:?set in .env}"
: "${RUNPOD_API_KEY:?account rpa_ key, set in .env}"

SOURCE_VOLUME_ID="${SOURCE_VOLUME_ID:-crimtr8kbf}"
RESULTS_VOLUME_ID="${RESULTS_VOLUME_ID:-$SOURCE_VOLUME_ID}"
# `-` not `:-`: unset -> default, but an EMPTY value is an error (matches src/config.py).
RESULTS_PREFIX="${RESULTS_PREFIX-results/ResearchGateEOD}"
RESULTS_PREFIX="${RESULTS_PREFIX#/}"; RESULTS_PREFIX="${RESULTS_PREFIX%/}"
S3_REGION="${RUNPOD_S3_REGION:-eu-ro-1}"
S3_ENDPOINT="${RUNPOD_S3_ENDPOINT:-https://s3api-eu-ro-1.runpod.io}"
DC="${RUNPOD_DATACENTER:-EU-RO-1}"

# The prefix IS the write boundary on a shared volume: refuse anything that is
# not results/<name>, so data/ (and the acquisition's other root prefixes) can
# never be the target of a write or an rm from these scripts.
case "$RESULTS_PREFIX" in
  results/?*) ;;
  *) echo "FATAL: RESULTS_PREFIX must be results/<name> (got '$RESULTS_PREFIX'); data/ is read-only" >&2; exit 2 ;;
esac
case "/$RESULTS_PREFIX/" in
  *"/../"*|*"/./"*|*"//"*) echo "FATAL: RESULTS_PREFIX has a '..', '.' or empty segment: '$RESULTS_PREFIX'" >&2; exit 2 ;;
esac

S3FLAGS="--region $S3_REGION --endpoint-url $S3_ENDPOINT"
SRC_BUCKET="s3://$SOURCE_VOLUME_ID"                     # read-only root
DST_ROOT="s3://$RESULTS_VOLUME_ID/$RESULTS_PREFIX"       # the only write target

command -v aws >/dev/null || { echo "aws CLI not found — install awscli" >&2; exit 1; }
