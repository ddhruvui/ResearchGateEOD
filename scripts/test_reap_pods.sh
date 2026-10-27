#!/usr/bin/env bash
# Unit test for reap_pods.sh's decision logic — the part that decides whether a pod has
# finished and whether it is safe to delete. It extracts the REAL classify() out of
# reap_pods.sh and drives it against fabricated pod logs, so no pod, volume or API is
# touched. Run it after any change to reap_pods.sh:  scripts/test_reap_pods.sh
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
STUB="$(mktemp -d -t reaptest)"
trap 'rm -rf "$STUB"' EXIT

# classify() reads logs through tail_log; point that at files we write instead.
tail_log() { [ -f "$STUB/$1" ] || return 1; cat "$STUB/$1"; }
eval "$(awk '/^classify\(\) \{/,/^\}$/' "$HERE/reap_pods.sh")"

PASS=0; FAIL=0
mklog() { printf '%s\n' "$2" > "$STUB/$1"; }          # $1 key, $2 body
t() {  # $1 desc  $2 want  $3 name  $4 id  $5 keys(newline-sep)  [$6 KEEP_FAILED]
  local desc="$1" want="$2"
  LOGKEYS="$5"; KEEP_FAILED="${6:-}"; VERDICT=""; REASON=""
  classify "$3" "$4"
  if [ "$VERDICT" = "$want" ]; then
    PASS=$((PASS + 1)); printf '  ok   %-52s -> %-4s | %s\n' "$desc" "$VERDICT" "$REASON"
  else
    FAIL=$((FAIL + 1)); printf '  FAIL %-52s -> got %s, want %s | %s\n' "$desc" "$VERDICT" "$want" "$REASON"
  fi
}

POD=researchgateeod-pso-lssvm
K1=20260924T010000Z-aaa.log
K2=20260924T014500Z-aaa.log

DONE0='run exit=0 (exited) 2026-09-24T01:22:00Z
publishing latest/ to MongoDB ...
work=0 (exited) at 2026-09-24T01:24:00Z — terminating pod aaa
TERMINATED via rest: HTTP 204'
DONE1='run exit=1 (exited) 2026-09-24T01:22:00Z
work=1 (exited) at 2026-09-24T01:24:00Z — terminating pod aaa'
MIDRUN='starting run: mode=daily limit=0 workers=auto
[grade] 105 tickers'
# The dangerous one: the run is over but the strategy/Mongo steps are still going.
PUBLISHING='run exit=0 (exited) 2026-09-24T01:22:00Z
computing stop-loss paper trade ...'
OLDBOOT='run exit=0 (exited) 2026-09-24T01:22:00Z
terminate HTTPError: 403
!! TERMINATION NOT CONFIRMED — kill pod aaa manually'
RESTARTED='RESTART DETECTED — this pod already ran mode=both shard=3 (exit 0); not re-running
work=0 (exited) at 2026-09-24T01:50:00Z — terminating pod aaa'

echo "--- finished runs are reaped whatever the exit code (the log is on the volume) ---"
mklog "$K1" "$DONE0";  t "work=0"                                  reap "$POD" aaa "$K1"
mklog "$K1" "$DONE1";  t "work=1 — reaped but flagged"             reap "$POD" aaa "$K1"
mklog "$K1" 'work=124 (WATCHDOG TIMEOUT) at X — terminating pod aaa'
                       t "work=124 watchdog"                       reap "$POD" aaa "$K1"
mklog "$K1" "$DONE1";  t "work=1 with --keep-failed"               hold "$POD" aaa "$K1" 1
mklog "$K1" "$DONE0";  t "work=0 with --keep-failed (still reaped)" reap "$POD" aaa "$K1" 1

echo "--- a pod is never reaped while it still has work to do ---"
mklog "$K1" "$MIDRUN";     t "mid-run"                             wait "$POD" aaa "$K1"
mklog "$K1" "$PUBLISHING"; t "run exit=0 but still publishing"     wait "$POD" aaa "$K1"
t "no log on this volume yet"                                      wait "$POD" zzz ""

echo "--- pods bundled before the work= line existed ---"
mklog "$K1" "$OLDBOOT"; t "old bootstrap, terminate ladder gave up" reap "$POD" aaa "$K1"
mklog "$K1" '!! TERMINATION NOT CONFIRMED — kill pod aaa manually'
                       t "old bootstrap, run exit= past the tail"      reap "$POD" aaa "$K1"
mklog "$K1" '!! TERMINATION NOT CONFIRMED — kill pod aaa manually'
                       t "…and --keep-failed does not strand it"       reap "$POD" aaa "$K1" 1
mklog "$K1" "$OLDBOOT"; t "old bootstrap, exit 0, --keep-failed"       reap "$POD" aaa "$K1" 1

echo "--- restarts: the code comes from the run that did the work ---"
mklog "$K1" "$DONE0"; mklog "$K2" "$RESTARTED"
t "restarted pod, original run exit=0"                             reap "$POD" aaa "$K1
$K2"
case "$REASON" in *RESTARTED*) ;; *) FAIL=$((FAIL+1)); echo "  FAIL restart not reported in reason";; esac

echo "--- KEEP_POD=1 is never reaped ---"
mklog "$K1" "KEEP_POD=1 — not to be reaped
work=0 (exited) at X — terminating pod aaa"
t "KEEP_POD=1 on a finished pod"                                   hold "$POD" aaa "$K1"

echo
echo "$PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
