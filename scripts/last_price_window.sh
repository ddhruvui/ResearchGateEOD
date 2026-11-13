#!/usr/bin/env bash
# Exit 0 when New York time is inside the LastPrice window, Sunday 20:00 to Friday 20:00,
# else exit 1. Used by .github/workflows/last-price.yml, whose UTC cron is a superset that
# covers the window in both EDT and EST; this check is what makes it DST-correct.
#
#   scripts/last_price_window.sh            # now, in America/New_York
#   scripts/last_price_window.sh 5 2005     # ISO weekday (1=Mon .. 7=Sun) and HHMM, for tests
#
# Friday runs up to 20:09 still count: GitHub often starts a scheduled run a few minutes
# late, and the 20:00 run is the one that should land.
set -euo pipefail

if [ $# -eq 2 ]; then
  dow=$1; hm=$2
else
  dow=$(TZ=America/New_York date +%u)
  hm=$(TZ=America/New_York date +%H%M)
fi
hm=$((10#$hm))

case "$dow" in
  7)       [ "$hm" -ge 2000 ] ;;   # Sunday from 20:00
  1|2|3|4) true ;;                 # Monday-Thursday, all day
  5)       [ "$hm" -lt 2010 ] ;;   # Friday until 20:00 (+ late-start slack)
  *)       false ;;                # Saturday
esac
