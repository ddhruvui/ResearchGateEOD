#!/usr/bin/env bash
# Intraday direction in one command, as if the current price were today's close:
#
#   1. scripts/seed_from_rg.sh       results/ResearchGate -> results/ResearchGateEOD (source read only)
#   2. python -m src.ib_today --upload   IB bars so far -> data/midrun/<T>.json
#      (YAHOO=1: python -m src.yahoo_last_price --midrun --force --need-bars, Yahoo's instead;
#       before the 09:30 ET open, the pre-market so far)
#   3. python -m src.midrun          data/ohlcv + data/midrun + EOD's tuned model -> EOD .../midrun/
#   4. python -m src.publish_mongo   latest/ and midrun/ -> MongoDB database ResearchGateEOD
#
# Which bars step 3 predicts from:
#   bash scripts/midrun.sh               # IB's, fetched now (needs TWS)
#   YAHOO=1 bash scripts/midrun.sh       # Yahoo's, fetched now, even over a fresh IB upload (no TWS)
#   YAHOO=1 KEEP_IB=1 bash scripts/midrun.sh  # Yahoo's, unless an IB upload is fresh (<30 min, or
#                                        # newer than Yahoo overnight): then IB's. midrun-yahoo.yml
#   SKIP_IB=1 bash scripts/midrun.sh     # whatever data/midrun holds: the last-price Action's
#                                        # Yahoo bars (<= ~10 min old) or a recent IB upload
# Other switches:
#   DRY_RUN=1 bash scripts/midrun.sh  # seed lists only, bars fetched but not uploaded, predict and Mongo print only
#   SKIP_SEED=1 bash scripts/midrun.sh   # reuse the EOD copy already there
#   SKIP_MONGO=1 bash scripts/midrun.sh  # do not publish to MongoDB
#
# With YAHOO=1 and no bar for today (weekend, holiday, before 04:00 ET) it stops after step 2
# with exit 0: nothing new to predict from.
#
# data/midrun/_run.json's "source" says whose bars are there (ib / yahoo). Never writes
# data/ohlcv, results/ResearchGate or the ResearchGate MongoDB database.
set -euo pipefail
cd "$(dirname "$0")/.."

if [ -z "${SKIP_SEED:-}" ]; then
  echo "== 1/4 copy ResearchGate results -> ResearchGateEOD"
  set +e; bash scripts/seed_from_rg.sh; rc=$?; set -e
  case "$rc" in
    0) ;;
    3) echo "   continuing with the EOD copy already there" ;;
    *) echo "FATAL: seed failed (exit $rc) — not predicting from a partial copy" >&2; exit 1 ;;
  esac
fi

if [ -n "${YAHOO:-}" ]; then
  echo "== 2/4 Yahoo bars so far -> data/midrun"
  FORCE="--force"; [ -n "${KEEP_IB:-}" ] && FORCE=""
  if [ -n "${DRY_RUN:-}" ]; then
    python3 -m src.yahoo_last_price --midrun $FORCE --dry-run
    echo "   [dry run] not uploaded; step 3 reads the bars already in data/midrun"
  else
    # 0: data/midrun holds today's bars; 3: no bar for today; anything else: failed.
    set +e; python3 -m src.yahoo_last_price --midrun --need-bars $FORCE; rc=$?; set -e
    case "$rc" in
      0) ;;
      3) echo "   no bar for today (weekend, holiday or before 04:00 ET) — nothing to predict"; exit 0 ;;
      *) echo "FATAL: Yahoo upload failed (exit $rc) — not predicting" >&2; exit 1 ;;
    esac
  fi
elif [ -z "${SKIP_IB:-}" ]; then
  echo "== 2/4 IB bars so far -> data/midrun"
  if [ -n "${DRY_RUN:-}" ]; then
    python3 -m src.ib_today
    echo "   [dry run] not uploaded; step 3 reads the bars already in data/midrun"
  else
    python3 -m src.ib_today --upload
  fi
fi

echo "== 3/4 predict next session from the data/midrun price"
python3 -m src.midrun ${DRY_RUN:+--dry-run}

# Same publish the ResearchGate pods run after a daily, into the ResearchGateEOD database
# only (src/publish_mongo.py refuses any other MONGO_DB). A failed publish leaves the
# results above in place, so it warns rather than failing the run.
if [ -z "${SKIP_MONGO:-}" ]; then
  echo "== 4/4 publish to MongoDB (ResearchGateEOD)"
  if [ -n "${DRY_RUN:-}" ]; then
    python3 -m src.publish_mongo --midrun --dry-run || echo "!! mongo dry run failed" >&2
  else
    # latest/ is the seed's copy; a publish only appends rows Mongo does not have yet.
    python3 -m src.publish_mongo || echo "!! mongo publish of latest/ failed" >&2
    python3 -m src.publish_mongo --midrun || echo "!! mongo publish of midrun failed" >&2
  fi
fi
