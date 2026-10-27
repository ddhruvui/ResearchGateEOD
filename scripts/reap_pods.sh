#!/usr/bin/env bash
# HOST-SIDE POD REAPER. Deletes pods whose run has FINISHED, from this laptop, so a run
# never depends on a pod being able to kill itself.
#
# Why this exists: from 2026-09-09 every DELETE a pod sent for itself came back 403, and
# this project wrote that off as "pods may not delete themselves". They may — the block
# was Cloudflare's 1010 on the default `Python-urllib/*` User-Agent, and bootstrap.sh now
# sends its own UA (see the TERMINATE block there). But the failure mode that exposed is
# permanent: when self-termination breaks for ANY reason, nothing notices, and the pod
#   1. bills forever — 20 idle shard pods are real money — and
#   2. blocks the next launch: launch.sh SKIPS a pod whose name is already up, so the
#      next daily silently never runs.
# A pod cannot be trusted to clean up after itself, so this does it from the host.
#
# It deletes a pod only after reading that pod's OWN log off the volume and seeing the
# run finish — the `work=<rc> (…) — terminating pod <id>` line bootstrap.sh prints AFTER
# the run, the strategy pass and the Mongo publish. `run exit=` is NOT enough: minutes of
# publishing still follow it. Older pods that predate that line are recognised by their
# `!! TERMINATION NOT CONFIRMED` tail instead.
#   Any exit code is reaped by default: the work is over either way and the log is already
#   on the volume, so there is nothing left on the pod to look at. A non-zero code is
#   reported loudly; --keep-failed holds those pods instead (they then block a relaunch of
#   the same shard, so clear them with --force once you are done reading).
#   KEEP_POD=1 pods are never reaped (launch.sh passes it through; it means "I am
#   inspecting this one").
#   A pod with no log on this volume is never reaped.
#
# Usage:
#   scripts/reap_pods.sh                      # one pass: report every pod, reap the finished ones
#   scripts/reap_pods.sh --watch              # keep polling (default: until --deadline, 9h)
#   scripts/reap_pods.sh --watch --until-empty    # …and stop once no pods are left
#   scripts/reap_pods.sh --dry-run            # report only, delete nothing
#   scripts/reap_pods.sh --keep-failed        # leave pods whose run exited non-zero up
#   scripts/reap_pods.sh s3 s7                # restrict to these pods (name fragment or id)
#   scripts/reap_pods.sh --force 3tkh6tp97rh5x4   # delete these ids without reading a log
#
# Launch a run and forget about it:
#   RUN_MODE=daily SHARDS=1 bash scripts/launch.sh
#   nohup scripts/reap_pods.sh --watch --until-empty > /tmp/reap.log 2>&1 &
#
# Env: REAP_POLL (60s), REAP_DEADLINE (32400s), REAP_TAIL_BYTES (8192), REAP_KEEP_FAILED,
#      REAP_NAME_PREFIX (researchgateeod-).
# Every deletion is appended to reaped-pods.log next to launched-pods.log.
. "$(dirname "$0")/_common.sh"
: "${RUNPOD_API_KEY:?account rpa_ key, set in .env}"

POLL="${REAP_POLL:-60}"
DEADLINE="${REAP_DEADLINE:-32400}"        # 9h: the pod watchdog is 7h, plus slack
TAIL_BYTES="${REAP_TAIL_BYTES:-8192}"
PREFIX="${REAP_NAME_PREFIX:-researchgateeod-}"
WATCH=""; UNTIL_EMPTY=""; DRY=""; FORCE=""; ONLY=""
KEEP_FAILED="${REAP_KEEP_FAILED:-}"

while [ $# -gt 0 ]; do
  case "$1" in
    --watch)        WATCH=1 ;;
    --until-empty)  UNTIL_EMPTY=1 ;;
    --poll)         POLL="$2"; shift ;;
    --deadline)     DEADLINE="$2"; shift ;;
    --dry-run|-n)   DRY=1 ;;
    --keep-failed)  KEEP_FAILED=1 ;;
    --force)        FORCE=1 ;;
    # print the header block: line 2 through the last line that is still a comment
    -h|--help)      sed -n '2,/^[^#]/p' "$0" | sed '$d' | sed 's/^#\{1,\} \{0,1\}//'; exit 0 ;;
    -*)             echo "unknown flag '$1' (see --help)" >&2; exit 2 ;;
    *)              ONLY="$ONLY $1" ;;
  esac
  shift
done
# --force skips the "has this run finished?" check, so it must never be able to sweep
# everything: it only accepts pods named explicitly.
if [ -n "$FORCE" ] && [ -z "$ONLY" ]; then
  echo "--force needs explicit pod ids/names (it skips the finished-run check)" >&2
  exit 2
fi

TMP="$(mktemp -d -t reappods)"
trap 'rm -rf "$TMP"' EXIT
say() { echo "[$(date -u +%H:%M:%SZ)] reap: $*"; }

# name<TAB>id for every $PREFIX* pod. Returns non-zero on an unreadable API response so a
# network blip is "try again later", never "nothing is running".
list_pods() {
  local J
  J="$(curl -sS --max-time 30 https://rest.runpod.io/v1/pods \
        -H "Authorization: Bearer $RUNPOD_API_KEY" 2>/dev/null)" || return 1
  printf '%s' "$J" | POD_PREFIX="$PREFIX" python3 -c '
import json, os, sys
want = os.environ["POD_PREFIX"]
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(1)
pods = d if isinstance(d, list) else d.get("pods", d.get("data", []))
for p in pods:
    if not isinstance(p, dict):
        continue
    name = str(p.get("name") or "")
    # No desiredStatus filter: a pod RunPod has stopped still bills for its container
    # disk and still blocks a relaunch of that shard name, so it is ours to delete too.
    if name.startswith(want) and p.get("id"):
        print("%s\t%s" % (name, p["id"]))
' || return 1
}

LOGKEYS=""
refresh_logkeys() {
  LOGKEYS="$(aws s3 ls $S3FLAGS "$DST_ROOT/_pod_logs/" 2>/dev/null | awk '{print $4}')" || LOGKEYS=""
}

# Last TAIL_BYTES of a log. A 20-shard rebuild leaves 20 logs to re-read every poll, and
# everything we key on is in the last ~2 KB. Falls back to a full copy if the volume's S3
# gateway ever stops honouring Range.
tail_log() {
  aws s3api get-object $S3FLAGS --bucket "$RESULTS_VOLUME_ID" \
      --key "$RESULTS_PREFIX/_pod_logs/$1" --range "bytes=-${TAIL_BYTES}" "$TMP/tail" >/dev/null 2>&1 \
    || aws s3 cp $S3FLAGS "$DST_ROOT/_pod_logs/$1" "$TMP/tail" --quiet >/dev/null 2>&1 \
    || return 1
  cat "$TMP/tail"
}

delete_pod() {   # $1 name  $2 id  $3 why
  if [ -n "$DRY" ]; then say "DRY-RUN would reap $1 ($2) — $3"; return 0; fi
  local CODE
  CODE="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 30 -X DELETE \
    "https://rest.runpod.io/v1/pods/$2" -H "Authorization: Bearer $RUNPOD_API_KEY" 2>/dev/null)" \
    || CODE="000"
  case "$CODE" in
    204|404)
      say "REAPED $1 ($2) — $3 [HTTP $CODE]"
      printf '%s\t%s\t%s\t%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$1" "$2" "$3" \
        >> "$ROOT/reaped-pods.log" 2>/dev/null || true ;;
    *)
      say "!! FAILED to reap $1 ($2): HTTP $CODE — will retry next pass" ;;
  esac
}

# Reads one pod's logs and decides what to do with it.
#   VERDICT=reap|hold|wait   REASON=<human text>
classify() {
  local name="$1" id="$2" keys nkeys newest k t code restarts="" found=""
  VERDICT="wait"; REASON="no log on this volume yet"; EXITCODE=""

  keys="$(printf '%s\n' "$LOGKEYS" | grep -- "-${id}\.log$" | sort || true)"
  [ -n "$keys" ] || return 0
  nkeys="$(printf '%s\n' "$keys" | grep -c . || true)"
  newest="$(printf '%s\n' "$keys" | tail -1)"
  # >1 log for one pod id means the container died and RunPod relaunched it. With the
  # restart guard in bootstrap.sh that costs only the pod's time; before it, shard pods
  # re-ran the whole shard and rewrote latest/ each pass (2026-09-16, 18 of them).
  [ "$nkeys" -gt 1 ] && restarts=" [RESTARTED ${nkeys}x]"

  t="$(tail_log "$newest")" || { REASON="log unreadable ($newest)"; return 0; }
  if printf '%s' "$t" | grep -q 'KEEP_POD=1'; then
    VERDICT="hold"; REASON="KEEP_POD=1 — deliberately kept alive, never reaped"; return 0
  fi

  # Take the code from the OLDEST log that carries one — that is the run that did the
  # work. After a restart the newest log is only the guard re-reporting, and it reports
  # the sentinel 98 when the marker was unreadable.
  for k in $keys; do
    if [ "$k" = "$newest" ]; then t="$t"; else t="$(tail_log "$k")" || continue; fi
    code="$(printf '%s\n' "$t" | sed -nE 's/.*work=(-?[0-9]+).*/\1/p' | tail -1)"
    # Pods bundled before the `work=` line exists: their run is over once the terminate
    # ladder has given up, and only then is `run exit=` safe to read. On a long log (a
    # rebuild shard) `run exit=` can be off the end of the tail we fetch — the ladder
    # still proves the run finished, so report 99 rather than stranding the pod.
    if [ -z "$code" ] && printf '%s' "$t" | grep -q 'TERMINATION NOT CONFIRMED'; then
      code="$(printf '%s\n' "$t" | sed -nE 's/.*run exit=(-?[0-9]+).*/\1/p' | tail -1)"
      [ -n "$code" ] || code=99      # 99: finished, exit code beyond the tail
      found="$k (pre-work= bootstrap)"
    fi
    [ -n "$code" ] && { found="${found:-$k}"; break; }
  done
  if [ -z "$found" ]; then
    VERDICT="wait"; REASON="still running (${newest})${restarts}"
    return 0
  fi

  EXITCODE="$code"
  if [ "$code" = "0" ]; then
    VERDICT="reap"; REASON="exit=0${restarts}"
  elif [ "$code" = "99" ]; then
    # The sentinel from the pre-work= fallback above, not a real exit code: the run is
    # over but its code is past the end of the tail. Reaped even under --keep-failed —
    # there is nothing here to say it failed.
    VERDICT="reap"; REASON="finished, exit code past the tail of ${found}${restarts}"
  elif [ -z "$KEEP_FAILED" ]; then
    VERDICT="reap"; REASON="exit=${code} (FAILED — see ${found})${restarts}"
  else
    VERDICT="hold"
    REASON="exit=${code} FAILED — held by --keep-failed, read ${found} then: scripts/reap_pods.sh --force ${id}${restarts}"
  fi
}

matches_only() {   # $1 name  $2 id
  [ -z "$ONLY" ] && return 0
  local w
  for w in $ONLY; do
    case "$1" in *"$w"*) return 0 ;; esac
    [ "$2" = "$w" ] && return 0
  done
  return 1
}

# --force: delete exactly what was named, no log read. Used to clear a pod this script is
# deliberately holding, or one whose log never made it to the volume.
if [ -n "$FORCE" ]; then
  PODS="$(list_pods)" || { say "pods API unreadable"; exit 1; }
  n=0
  while IFS=$'\t' read -r NAME ID; do
    [ -n "${ID:-}" ] || continue
    matches_only "$NAME" "$ID" || continue
    delete_pod "$NAME" "$ID" "--force"; n=$((n + 1))
  done <<< "$PODS"
  [ "$n" = "0" ] && say "nothing matched:$ONLY"
  exit 0
fi

STARTED="$(date +%s)"
while true; do
  PODS="$(list_pods)" || PODS="__ERR__"
  if [ "$PODS" = "__ERR__" ]; then
    say "WARN: pods API unreachable — reaping nothing this pass"
  else
    refresh_logkeys
    LIVE=0
    if [ -n "$PODS" ]; then
      while IFS=$'\t' read -r NAME ID; do
        [ -n "${ID:-}" ] || continue
        LIVE=$((LIVE + 1))
        matches_only "$NAME" "$ID" || continue
        classify "$NAME" "$ID"
        case "$VERDICT" in
          reap) delete_pod "$NAME" "$ID" "$REASON" ;;
          hold) say "HOLD  ${NAME#$PREFIX} ($ID) — $REASON" ;;
          *)    say "wait  ${NAME#$PREFIX} ($ID) — $REASON" ;;
        esac
      done <<< "$PODS"
    fi
    [ "$LIVE" = "0" ] && say "no ${PREFIX}* pods running"
    if [ -n "$WATCH" ] && [ -n "$UNTIL_EMPTY" ] && [ "$LIVE" = "0" ]; then
      say "nothing left to watch — done"; exit 0
    fi
  fi

  [ -n "$WATCH" ] || exit 0
  if [ $(( $(date +%s) - STARTED )) -ge "$DEADLINE" ]; then
    say "deadline (${DEADLINE}s) reached — stopping; re-run to keep watching"; exit 0
  fi
  sleep "$POLL"
done
