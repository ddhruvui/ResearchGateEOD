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

**Automatic, from Yahoo:** the **midrun-yahoo** Action
([.github/workflows/midrun-yahoo.yml](.github/workflows/midrun-yahoo.yml)) runs the midrun and
publishes it to the dashboard Monday-Friday at 08:05, 12:05, 16:05 and 20:05 New York time:

- **08:05:** the pre-market so far. Yahoo has prices from 04:00 but no volume before the open.
- **12:05:** the regular session so far.
- **16:05:** the regular close.
- **20:05:** the after-hours close.

It runs `YAHOO=1 KEEP_IB=1 SKIP_SEED=1 bash scripts/midrun.sh`. If you uploaded IB bars in the
last 30 minutes, it predicts from those instead. On exchange holidays it finds no bar and
publishes nothing. Run it by hand from the Actions tab (**midrun-yahoo → Run workflow**).

**By hand, pick the price source each time:**

| Command | Predicts from |
|---|---|
| `bash scripts/midrun.sh` | IB's bars, fetched now (needs TWS) |
| `YAHOO=1 bash scripts/midrun.sh` | Yahoo's bars, fetched now (no TWS), even right after an IB upload |
| `YAHOO=1 KEEP_IB=1 bash scripts/midrun.sh` | Yahoo's bars, unless an IB upload is fresh; then IB's |
| `SKIP_IB=1 bash scripts/midrun.sh` | whatever `data/midrun/` holds now (see below) |

The **last-price** Action ([.github/workflows/last-price.yml](.github/workflows/last-price.yml))
uploads Yahoo's bar so far to `data/midrun/` every 10 minutes once the session opens (same files,
same format as IB's). So `SKIP_IB=1` usually gets a Yahoo bar at most about 10 minutes old.

An IB upload (`bash scripts/midrun.sh`, or `python3 -m src.ib_today --upload`) wins over the
Action. The Action leaves it alone for 30 minutes, and for as long as it is newer than Yahoo's
newest trade. That covers the overnight session after 20:00 ET, which only IB has. `YAHOO=1`
replaces it straight away. `data/midrun/_run.json`'s `source` says whose bars are there
(`ib` or `yahoo`).

IB has a bar for today from the 09:30 ET open until midnight, Yahoo from 04:00 ET (pre-market)
until midnight. Without one, `bash scripts/midrun.sh` fails with "no bar", and `YAHOO=1` stops
with "nothing to predict".

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
