#!/usr/bin/env bash
# Pull the latest run from under results/ResearchGateEOD/ into ./results/ and print the summary.
#   scripts/results.sh            # latest
#   scripts/results.sh 20260824T… # a specific stamp
. "$(dirname "$0")/_common.sh"

RUN_ID="${RUN_ID:-pso_lssvm_v1}"
STAMP="${1:-latest}"
OUT="$ROOT/results/$STAMP"
mkdir -p "$OUT"
echo "pulling $DST_ROOT/runs/$RUN_ID/$STAMP/ -> $OUT"
aws s3 cp $S3FLAGS "$DST_ROOT/runs/$RUN_ID/$STAMP/" "$OUT/" --recursive
echo
[ -f "$OUT/metrics.json" ] && python3 - "$OUT/metrics.json" <<'PY'
import json, sys
m = json.load(open(sys.argv[1]))
o = m.get("overall", {})
if not o.get("n"): print("no scored predictions"); raise SystemExit
print(f"predictions   : {o['n']:,}")
print(f"direction acc : {o['direction_accuracy']:.4f}  (p={o['p_value_vs_coin']:.3g})")
print(f"always-up base: {o['baseline_always_up']:.4f}   edge {o['edge_vs_always_up']:+.4f}")
print(f"last-dir base : {o['baseline_last_dir']:.4f}")
print(f"MSE model/zero: {o['mse_model']:.3e} / {o['mse_zero_baseline']:.3e}")
print("\nby year:")
for y, b in sorted(m.get("by_year", {}).items(), key=lambda kv: int(kv[0])):
    if b.get("n"):
        print(f"  {y}  n={b['n']:7,}  acc={b['direction_accuracy']:.4f}  edge={b['edge_vs_always_up']:+.4f}")
PY
