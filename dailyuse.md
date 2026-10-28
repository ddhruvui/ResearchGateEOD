# Daily use

**Automatic:** this project does not run its own daily. ResearchGate's daily runs at 04:30 New York
time; at 06:00 GitHub Actions
([.github/workflows/sync-from-researchgate.yml](.github/workflows/sync-from-researchgate.yml))
waits for it to finish, copies `results/ResearchGate/` into `results/ResearchGateEOD/` and
publishes to MongoDB `ResearchGateEOD`. No pod. Needs the repo secret `RESEARCHGATEEOD_ENV` = the
full contents of `.env`. Run it by hand from the Actions tab
(**sync-from-researchgate → Run workflow**), or from here:

```bash
bash scripts/sync_from_rg.sh
```

**During the day:** `bash scripts/midrun.sh` predicts the next session as if IB's current price
were today's close (copy from ResearchGate, upload IB bars, predict, publish).

To run this project's own daily on a pod instead (not needed while it mirrors ResearchGate):

```bash
RUN_MODE=daily SHARDS=1 WATCHDOG_SEC=3600 bash scripts/launch.sh
```

The pod deletes itself when the run finishes. If it can't, this clears it (safe to run any time —
it only deletes pods whose run is over):

```bash
scripts/reap_pods.sh
```

`/daily-run` does both for you.

## Every day

After the new day's data is published, type:

```
/daily-run
```

Takes about a minute and costs about $0.01. The dashboard updates a couple of minutes later:
https://researchgatefe.onrender.com

If it says `nothing to do`, the new data isn't in yet. Try again later.

## Every quarter

Nothing extra. The daily run re-tunes each stock by itself on the first run of a new quarter.
That run just takes a little longer.

## Only when you change a setting or the ticker list

```
/quarterly-backtest
```

This is a full rebuild: about 30 minutes and $2–3. It wipes the current results, so don't run it casually.

## Check the dashboard

```
/dashboard
```

More detail: [RUNBOOK.md](RUNBOOK.md)
