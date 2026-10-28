#!/usr/bin/env bash
# Intraday direction in one command, as if IB's current price were today's close:
#
#   1. scripts/seed_from_rg.sh       results/ResearchGate -> results/ResearchGateEOD (source read only)
#   2. python -m src.ib_today --upload   IB bars so far -> data/midrun/<T>.json
#   3. python -m src.midrun          data/ohlcv + data/midrun + EOD's tuned model -> EOD .../midrun/
#
#   bash scripts/midrun.sh            # all three
#   DRY_RUN=1 bash scripts/midrun.sh  # seed lists only, IB bars saved locally only, predict prints only
#   SKIP_SEED=1 bash scripts/midrun.sh   # reuse the EOD copy already there
#   SKIP_IB=1 bash scripts/midrun.sh     # reuse the bars already in data/midrun
#
# Needs TWS running for step 2. Never writes data/ohlcv or results/ResearchGate.
set -euo pipefail
cd "$(dirname "$0")/.."

if [ -z "${SKIP_SEED:-}" ]; then
  echo "== 1/3 copy ResearchGate results -> ResearchGateEOD"
  set +e; bash scripts/seed_from_rg.sh; rc=$?; set -e
  case "$rc" in
    0) ;;
    3) echo "   continuing with the EOD copy already there" ;;
    *) echo "FATAL: seed failed (exit $rc) — not predicting from a partial copy" >&2; exit 1 ;;
  esac
fi

if [ -z "${SKIP_IB:-}" ]; then
  echo "== 2/3 IB bars so far -> data/midrun"
  if [ -n "${DRY_RUN:-}" ]; then
    python3 -m src.ib_today
    echo "   [dry run] not uploaded; step 3 reads the bars already in data/midrun"
  else
    python3 -m src.ib_today --upload
  fi
fi

echo "== 3/3 predict next session from the IB price"
python3 -m src.midrun ${DRY_RUN:+--dry-run}
