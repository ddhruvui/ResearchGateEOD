#!/usr/bin/env bash
# Pod entrypoint. The volume crimtr8kbf is mounted at /workspace. Its data/ tree
# is READ-ONLY market data shared with the acquisition pipeline. This script
# writes ONLY under /workspace/$RESULTS_PREFIX (results/ResearchGateEOD): its log,
# nothing else (the code bundle unpacks to container disk). The python code writes through
# src/storage.py::ResultStore, which is confined to the same prefix.
set +e

# Refuse to touch the mount unless the prefix is results/<name>. Failing here
# writes no log, so launch.sh's startup check kills the pod within minutes.
case "${RESULTS_PREFIX:-}" in
  results/?*) ;;
  *) echo "!! RESULTS_PREFIX must be results/<name> (got '${RESULTS_PREFIX:-}') — refusing to touch /workspace"; sleep 30; exit 1 ;;
esac
case "/$RESULTS_PREFIX/" in
  *"/../"*|*"/./"*|*"//"*) echo "!! RESULTS_PREFIX has a '..', '.' or empty segment — refusing"; sleep 30; exit 1 ;;
esac
RP="/workspace/${RESULTS_PREFIX}"

LOGDIR="$RP/_pod_logs"
mkdir -p "$LOGDIR" 2>/dev/null
LOG="$LOGDIR/$(date -u +%Y%m%dT%H%M%SZ)-${RUNPOD_POD_ID:-nopod}.log"
exec > >(tee -a "$LOG") 2>&1
echo "bootstrap start $(date -u +%FT%TZ) pod=${RUNPOD_POD_ID:-?}"
python -c 'import sys; print("python", sys.version)'

# Unpack onto this pod's own container disk, never the shared volume: every shard
# pod runs this script, and with one volume-wide app/ each new pod's rm -rf deleted
# src/ out from under the pods started seconds before it (2026-09-16: 20 shards
# placed 18 s apart, several died at once with "No module named 'src'").
WORK="/opt/researchgate/app"
case "$WORK" in /workspace/*) echo "!! code must not unpack onto the volume: $WORK"; exit 1 ;; esac
rm -rf "$WORK"; mkdir -p "$WORK"
tar xzf "$RP/code/bundle.tar.gz" -C "$WORK" || { echo "!! bundle extract failed"; }
cd "$WORK" || exit 1
ls -la

# RESTART GUARD. RunPod relaunches dockerStartCmd whenever the container exits, so a
# pod that cannot delete itself comes straight back and RE-RUNS the whole job: on
# 2026-09-16 eighteen shard pods re-ran 2-4 times, each pass rewriting latest/. The
# marker holds the run's exit code and is written only AFTER the run returns, so a
# restart reports what the ORIGINAL run did instead of replaying it — while a pod
# killed MID-run leaves no marker and does resume, which is what we want there.
MARKER="$LOGDIR/.ran-${RUNPOD_POD_ID:-nopod}"
[ -n "${KEEP_POD:-}" ] && echo "KEEP_POD=1 — the host-side reaper will leave this pod alone"
if [ -f "$MARKER" ]; then
  ec="$(cat "$MARKER" 2>/dev/null)"
  case "$ec" in ''|*[!0-9-]*) ec=98 ;; esac   # 98: marker unreadable, outcome unknown
  echo "RESTART DETECTED — this pod already ran mode=${RUN_MODE:-both} shard=${SHARD:-0} (exit $ec); not re-running"
else

echo "installing deps ..."
timeout 900 python -m pip install --quiet --no-input --disable-pip-version-check -r requirements.txt \
  || echo "!! pip install failed"

# One BLAS thread per process: we parallelise across tickers, so per-process
# thread pools only oversubscribe cores and RAM (observed OOM at 2 vCPU).
export OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1 \
       NUMEXPR_NUM_THREADS=1 VECLIB_MAXIMUM_THREADS=1
MEMB=$(cat /sys/fs/cgroup/memory.max 2>/dev/null || cat /sys/fs/cgroup/memory/memory.limit_in_bytes 2>/dev/null || echo "?")
case "$MEMB" in ''|max|?) MEMSTR="unknown" ;; *) MEMSTR="$((MEMB/1024/1024))MB" ;; esac
echo "cpus=$(nproc 2>/dev/null || echo ?) mem=${MEMSTR} (cgroup limit)"
echo "starting run: mode=${RUN_MODE:-both} limit=${RUN_LIMIT:-0} workers=${WORKERS:-auto}"
# `daily` is the incremental path (grade -> learn -> guess); everything else is
# the full walk-forward in src/run.py.
if [ "${RUN_MODE:-both}" = "daily" ]; then
  MODULE="src.daily"; ARGS=""
else
  MODULE="src.run"; ARGS="--mode ${RUN_MODE:-both}"
fi
[ "${RUN_LIMIT:-0}" != "0" ] && ARGS="$ARGS --limit ${RUN_LIMIT}"
timeout "${WATCHDOG_SEC:-25200}" python -m "$MODULE" $ARGS
ec=$?
echo "run exit=$ec ($([ $ec -eq 124 ] && echo 'WATCHDOG TIMEOUT' || echo exited)) $(date -u +%FT%TZ)"
sync 2>/dev/null

# Refresh the $10k stop-loss paper trade over the predictions this run just wrote.
# It only READS latest/predictions.parquet and writes its own keys, so a failure
# here cannot affect the run that already landed — hence no change to $ec.
if [ "$ec" -eq 0 ] && { [ "${RUN_MODE:-both}" = "daily" ] || [ "${SHARDS:-1}" -le 1 ]; }; then
  echo "computing stop-loss paper trade ..."
  timeout 1800 python -m src.strategy || echo "!! strategy failed (the run itself succeeded)"
fi

# Publish latest/ to MongoDB for the deployed dashboard. Only when this pod wrote
# latest/ itself (a daily run, or an unsharded full run); a sharded backtest is
# merged and published from the laptop by src.merge. The run has already landed
# on the volume, so a Mongo failure is never fatal.
if [ "$ec" -eq 0 ] && [ -n "${MONGO_URI:-}" ] && { [ "${RUN_MODE:-both}" = "daily" ] || [ "${SHARDS:-1}" -le 1 ]; }; then
  FULL=""; [ "${RUN_MODE:-both}" != "daily" ] && FULL="--full"
  echo "publishing latest/ to MongoDB ${FULL} ..."
  timeout 900 python -m src.publish_mongo $FULL || echo "!! mongo publish failed (the run itself succeeded)"
fi

echo "$ec" > "$MARKER" 2>/dev/null || true   # restart guard: the run happened
fi

# Repeated here, not just at the top: the reaper only reads the last few KB of the log,
# and this is the one line that stops it deleting a pod you are still looking at.
[ -n "${KEEP_POD:-}" ] && echo "KEEP_POD=1 — not to be reaped"

# THE line scripts/reap_pods.sh reads off the volume to know this pod is finished. It
# comes after the run AND the strategy/publish steps on purpose: `run exit=` is printed
# with minutes of work still to go, and reaping on that would kill a pod mid-publish.
echo "work=$ec ($([ "$ec" -eq 124 ] 2>/dev/null && echo 'WATCHDOG TIMEOUT' || echo exited)) at $(date -u +%FT%TZ) \
— terminating pod ${RUNPOD_POD_ID:-?}"
sync 2>/dev/null   # flush the tee'd log to the volume before the pod is destroyed

# TERMINATE. Every DELETE sent from inside a pod came back 403 from 2026-09-09 on, and
# this project concluded pods simply may not delete themselves. They may: the cause is
# neither the pod nor the key. Cloudflare fronts rest.runpod.io and answers "error code:
# 1010" (banned browser signature) to the DEFAULT Python User-Agent, and urllib sends
# `Python-urllib/3.11`. Proof, all from one machine, same key, same URL: urllib default
# UA -> 403, urllib with any other UA -> 404, curl -> 404, curl FORCED to
# `Python-urllib/3.11` -> 403. The laptop "it works from here" test used curl, which is
# why it looked like an inside-the-pod restriction. Hence the UA below — do not remove it.
# The ladder then tries GraphQL podTerminate (different host, same UA fix) and runpodctl,
# and NAMES whichever rung worked, so a future block shows up in the log as itself rather
# than as a mystery. None of it is load-bearing: scripts/reap_pods.sh deletes this pod
# from the laptop if every rung fails.
for attempt in $(seq 1 6); do
  timeout 90 python - <<'PY'
import json, os, shutil, ssl, subprocess, sys, urllib.error, urllib.request as u

pid = os.environ.get("RUNPOD_POD_ID", "")
key = os.environ.get("RUNPOD_TERMINATE_KEY", "")
if not pid or not key:
    print("MISSING RUNPOD_POD_ID or RUNPOD_TERMINATE_KEY", file=sys.stderr)
    sys.exit(2)
UA = "researchgate-pod/1.0"                        # anything but Python-urllib/*; see above
CTXS = (None, ssl._create_unverified_context())    # verified TLS first, unverified fallback


def _req(url, **kw):
    r = u.Request(url, **kw)
    r.add_header("Authorization", "Bearer " + key)
    r.add_header("User-Agent", UA)
    return r


def rest():
    err = "no attempt"
    for ctx in CTXS:
        try:
            st = u.urlopen(_req("https://rest.runpod.io/v1/pods/" + pid, method="DELETE"),
                           timeout=30, context=ctx).status
            if st == 204:
                return True, "HTTP 204"
            err = "HTTP %s" % st
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return True, "HTTP 404 (already gone)"
            err = "HTTP %s%s" % (e.code, " (Cloudflare 1010 — User-Agent blocked)"
                                 if e.code == 403 else "")
        except Exception as e:
            err = str(e)
    return False, err


def graphql():
    body = json.dumps({
        "query": "mutation($id: String!) { podTerminate(input: {podId: $id}) }",
        "variables": {"id": pid},
    }).encode()
    err = "no attempt"
    for ctx in CTXS:
        try:
            req = _req("https://api.runpod.io/graphql", data=body, method="POST")
            req.add_header("Content-Type", "application/json")
            payload = json.loads(u.urlopen(req, timeout=30, context=ctx).read().decode() or "{}")
            errs = payload.get("errors") or []
            if not errs:
                return True, "podTerminate accepted"
            code = (errs[0].get("extensions") or {}).get("code")
            if code == "POD_NOT_FOUND":
                return True, "podTerminate (already gone)"
            err = str(errs[0].get("message") or code)
        except urllib.error.HTTPError as e:
            err = "HTTP %s" % e.code
        except Exception as e:
            err = str(e)
    return False, err


def runpodctl():
    exe = shutil.which("runpodctl")
    if not exe:
        return False, "not installed on this image"
    try:
        p = subprocess.run([exe, "remove", "pod", pid], timeout=60,
                           env=dict(os.environ, RUNPOD_API_KEY=key),
                           capture_output=True, text=True)
    except Exception as e:
        return False, str(e)
    out = ((p.stdout or "") + " " + (p.stderr or "")).strip().replace("\n", " ")[:160]
    return (p.returncode == 0), ("rc=%s %s" % (p.returncode, out))


for name, fn in (("rest", rest), ("graphql", graphql), ("runpodctl", runpodctl)):
    ok, msg = fn()
    if ok:
        print("TERMINATED via %s: %s" % (name, msg))
        sys.exit(0)
    print("terminate via %s failed: %s" % (name, msg), file=sys.stderr)
sys.exit(1)
PY
  [ $? -eq 0 ] && exit 0
  echo "terminate attempt $attempt did not confirm — retrying in 20s"; sleep 20
done

echo "!! TERMINATION NOT CONFIRMED after retries — leaving this pod to the host-side reaper"
echo "   (scripts/reap_pods.sh --watch --until-empty, from the laptop)"
# Idle rather than exit: an exited container is relaunched by RunPod, and while the
# restart guard above stops the job re-running, idling keeps one pod to one log and one
# run. The reaper deletes this pod within a poll or two of the `work=` line above.
exec sleep infinity
