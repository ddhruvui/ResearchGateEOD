# PSO + LS-SVM next-session prediction

> **To run this, say:**
> *"We have new data for &lt;DATE&gt;. Run the daily pipeline and monitor it."* (`/daily-run`)
> or *"Re-run the full backtest from scratch with quarterly re-tuning, and monitor it."* (`/quarterly-backtest`)
>
> Copy-paste prompts, expected output, and troubleshooting: **[RUNBOOK.md](RUNBOOK.md)**

Implementation of **"A Machine Learning Model for Stock Market Prediction"** —
Hegazy, Soliman & Abdul Salam, *IJCST* 4(12), Dec 2013
([MachineLearningModel.pdf](MachineLearningModel.pdf)) — applied to a hand-picked
universe of 151 US large caps and ETFs (`tickers.json`), with a walk-forward
backtest and a daily live prediction.

The paper's idea: an LS-SVM predicts the next price from five technical
indicators, and Particle Swarm Optimisation picks the LS-SVM's free parameters
instead of a human guessing them.

---

## Volumes — the read-only rule

| Volume | Prefix | Role | Access |
|---|---|---|---|
| `crimtr8kbf` | `data/` | market data (EOD bars, splits, calendar), published by the acquisition pipeline | **READ-ONLY** |
| `crimtr8kbf` | `results/ResearchGateEOD/` | all results, pod logs, code bundle (`RESULTS_PREFIX`) | read/write |

Since 2026-09-16 there is one volume. `data/` — and every other root prefix the
acquisition pipeline owns — is never written to. Enforced four independent ways:

1. **`SourceStore` / `LayeredSource` have no write methods.** No put, copy or
   delete exists on either class, so there is no code path from a read to a
   write. `tests/test_storage_guard.py` asserts this by introspection.
2. **`ResultStore` is confined to `RESULTS_PREFIX`.** Every key a caller passes
   is relative and is pinned under `results/<name>/` by `_full()`, which rejects
   absolute keys, `..` and empty segments. It has no delete method, and its raw
   S3 client is private — the tests grep `src/` to prove nothing else touches
   it. `Env.load()` refuses a `RESULTS_PREFIX` outside `results/` and a
   repointed `SOURCE_VOLUME_ID`.
3. **The pod writes only under `/workspace/results/ResearchGateEOD/`.**
   `scripts/bootstrap.sh` validates `RESULTS_PREFIX` before touching the mount
   and keeps its log and the unpacked bundle under that path; the python code
   goes through `ResultStore`.
4. **The shell helpers have no root-level write target.** `scripts/_common.sh`
   exposes `SRC_BUCKET` (the read-only root, `ls`/`cp` from only) and
   `DST_ROOT` (`s3://crimtr8kbf/results/ResearchGateEOD`); every `aws s3 rm` in
   the skills is spelled against `$DST_ROOT/...`.

Anything extra you want alongside the source data — derived columns, caches,
diagnostics — is written under `results/ResearchGateEOD/`.

---

## How the loop works

Every session does three things, in order:

1. **Grade** the previous session's prediction against what actually happened.
2. **Learn** — the realised bar joins the training window.
3. **Predict** the next session.

The backtest is that same loop replayed over history, where the answers are
already known. From **2021-01-04** to the last bar there are **1,415 graded
predictions per ticker**; the model then predicts the next session live, which
is graded on the following run.

### Why it cannot see the future

Rows are sessions `0..N-1`. Features `F[j]` use bars `<= j`. Targets are
`y[j] = adj_close[j+1]/adj_close[j] - 1`, the return realised on session `j+1`.
To predict `y[j]` the model gets input `F[j]` and trains only on pairs
`(F[d], y[d])` for `d <= j-1`. The assertion in `walkforward.run_ticker`
enforces that; `tests/test_leakage.py` proves it twice — once on the slice
arithmetic, and once by corrupting every future bar by 3.7x and asserting that
no earlier prediction moves by more than 1e-14.

Knowing *when* the market is open is not lookahead — the exchange calendar is
published years ahead. A Friday close predicts **Tuesday** when Monday is a
holiday, and those wider gaps are scored separately (`by_gap_days`) so a
calendar artefact is not mistaken for model failure.

---

## Scoring

Closeness to the real price is deliberately not the headline. "Tomorrow equals
today" is nearly right every day, so a low error proves nothing. What is
reported instead:

* **direction accuracy** with an exact binomial p-value against a coin flip
* **always-up baseline** — exploits the equity drift; the edge over this is the
  number that matters
* **last-direction baseline** — repeats yesterday's move
* **zero-return baseline** for MSE/MAE
* a **per-year breakdown**, because a pooled figure hides a model that only
  works in calm markets

---

## Deviations from the paper, and why

| Paper | Here | Reason |
|---|---|---|
| Predicts the next **price level** | Predicts the next **return** | Prices run 16 -> 275 over the window; kernels extrapolate badly outside their training range. Direction falls out of the sign, and returns are stationary. |
| Tunes **C, epsilon, gamma** | Tunes **C, gamma** | Epsilon is the width of Vapnik's insensitive tube. It belongs to SVR and appears nowhere in the LS-SVM the paper derives (eq 2-6 use squared error). There is nothing for it to control. |
| **MLP/tanh kernel** (eq 10) | **RBF** default (eq 9) | `tanh(k x'z + th)` is not positive semi-definite for general parameters, so the eq-6 system is not guaranteed well posed. Set `model.kernel: mlp` for paper fidelity. |
| Scores **MSE only** | Direction + baselines | See Scoring. MSE on price levels cannot distinguish a real edge from a persistence effect. |
| PSO fitness = **MSE** | **rank IC** (`pso.fitness`) | The paper never defines its fitness beyond "measures the closeness of the corresponding solution to the optimum"; the only criterion it names anywhere is MSE. That is a trap — see below. |
| Tunes **once** | **Quarterly**, trailing window | Tuning once means the dials age indefinitely. Quarterly re-tuning uses only data strictly before each tuning point, so the identical rule applies in backtest and in live use. |
| `MACD = 0.075*EMA - 0.15*EMA` | Standard MACD(12, 26, 9) | Those coefficients are EMA alphas, not weights: `2/(n+1)` gives 0.074 at n=26, 0.154 at n=12, 0.2 at n=9. The paper's formula IS standard MACD, written in terms of alpha. |
| 13 tickers, 3 years | 105 tickers, 2021 to date | Four of the paper's 13 are unavailable (BK/FMC outside the universe; HSP and LIFE delisted 2015/2014). |

### Why the PSO fitness is not MSE

Expand the objective: `E[(p-a)^2] = E[a^2] - 2E[pa] + E[p^2]`. When the signal is
weak the cross term `E[pa]` is negligible, so the cheapest way to reduce MSE is to
shrink `E[p^2]` — i.e. **predict near zero**. Under no signal the MSE-optimal
forecast *is* zero.

That is not theoretical. The first full backtest (`fitness: mse`) produced
predictions averaging **21% of real move sizes**, correlating **0.024** with them,
while MSE itself looked respectable at 1.05x the zero baseline. PSO had optimised
exactly what it was asked to; the request was wrong.

`tests/test_model.py::test_mse_rewards_shrinkage_but_ic_does_not` pins this down:
scaling every prediction by 0.01 destroys all magnitude information yet **improves**
MSE, while IC, rank IC and directional accuracy are unchanged — they are
scale-invariant and cannot be gamed that way.

Rank IC (Spearman) is the default because it is scale-invariant, dense (every
validation day contributes, unlike binary directional accuracy over ~252 days),
and robust to the fat tails in daily returns. `fitness: mse` reproduces the paper.

### Modes

| Mode | What it does | Cost |
|---|---|---|
| `RUN_MODE=both` | full walk-forward replay + live prediction | ~2 h/ticker at quarterly re-tuning |
| `RUN_MODE=backtest` | replay only | as above |
| `RUN_MODE=predict` | live prediction only, reusing stored dials | seconds/ticker |
| `RUN_MODE=daily` | **grade stored guess -> learn -> guess again** | ~0.2 s/ticker |

`daily` is the everyday path (`src/daily.py`). It grades the prediction already
written to `next_session.parquet` **as stored** — never recomputed — so the log
records what the model actually said when it said it. A graded verdict is locked:
re-running never revises it, even if the vendor later restates the price. Rows
carry a `source` column (`live` vs `backtest`) so a genuine forward track record
stays separable from a replay. It is idempotent, and exits 0 having changed
nothing if the source has no new bar yet.

### Re-tuning cadence and what it costs

Every tuning event sees only the trailing window strictly before it. Quarterly is
the finest cadence a full backtest can still validate:

| Cadence | Tuning events | Fits/ticker | vs once | Backtest time/ticker |
|---|---|---|---|---|
| once | 1 | 2,035 | 1x | 15 min |
| annual | 6 | 5,135 | 2.5x | 38 min |
| **quarterly** | **24** | **16,295** | **8x** | **~2 h** |
| monthly | 67 | 42,955 | 21x | 5.3 h |
| weekly | 294 | 183,695 | 90x | 22.6 h |

Weekly is affordable in live operation but makes the backtest a ~5-day job — you
would then be running a system your evidence never tested.

### Known biases, stated plainly

* **Universe selection.** `tickers.json` is a hand-picked list of 151 names
  chosen in October 2026 (large caps across sectors, plus SPY and QQQ), so a
  backtest starting 2021 partly picks names for how they performed over the test
  window. `HistoricalTickerComponents` in the source volume's
  `data/universe/GSPC.INDX.json` supports a point-in-time rebuild if this matters.
* **Adjusted prices are restated.** `adjusted_close` for 2021 reflects every
  split and dividend since. Splits rescale the series uniformly and are harmless
  to returns; dividend adjustment is a small genuine lookahead.
* **Late listings.** ABNB, DASH, SNOW (late-2020 listings), HOOD (2021), CEG
  (2022), ARM (2023) and GEV (2024) have little or no pre-2021 history and PLTR
  has 65 rows; `min_train_rows` staggers them in when they qualify, so the
  universe grows over the run to 151.

---

## Running it

```sh
cp .env.example .env          # fill in RunPod S3 + account keys
pip install -r requirements.txt
pytest tests/ -q              # 15 tests, including the leakage proof

RUN_MODE=both RUN_LIMIT=3 scripts/launch.sh   # smoke test on a pod
RUN_MODE=both SHARDS=20 scripts/launch.sh     # full run: backtest + next-session prediction
scripts/results.sh            # pull the latest results and print the summary
```

Daily operation after the first full backtest:

```sh
RUN_MODE=daily SHARDS=1 WATCHDOG_SEC=3600 bash scripts/launch.sh
```

`predict` mode skips the 1,415-step replay and reuses the hyper-parameters PSO
found on the last full backtest — seconds per ticker instead of minutes.

### Dashboard

The results are viewed on a deployed UI, not on this machine. The pipeline
publishes `runs/<run_id>/latest/` to MongoDB Atlas (database `ResearchGateEOD`),
and two small repos serve it:

| Piece | Repo | Runs on |
|---|---|---|
| API (Express) | [ddhruvui/ResearchGateBE](https://github.com/ddhruvui/ResearchGateBE) | Vercel |
| UI (React/Vite) | [ddhruvui/ResearchGateFE](https://github.com/ddhruvui/ResearchGateFE) | Render |

Publishing is automatic: `scripts/bootstrap.sh` runs `src.publish_mongo` after a
daily run, and `src.merge` runs it after a rebuild. By hand:

```sh
python3 -m src.publish_mongo            # append what Mongo lacks (idempotent)
python3 -m src.publish_mongo --full     # after a rebuild: replace the run's rows
```

It needs `MONGO_URI`, `DB_PASSWORD` and `MONGO_DB` in `.env` (see
`.env.example`); with them unset it prints a notice and does nothing. The
publisher also pre-computes the equity curves for the UI's cost levels, so the
API never scans the 220k-row `predictions` collection on a page load, and copies
`strategy.json` into a `strategy` collection (one document per ticker) that the
UI's stop-loss table reads. See
`RUNBOOK.md` for the day-to-day commands.

### The stop-loss paper trade

Direction accuracy answers "was the call right". `src/strategy.py` answers a
harder question: **follow every call with $10,000 and a stop loss, and where do
you end up?** It is a pure reader of `latest/predictions.parquet` — it never
regrades or rewrites a prediction, and deleting everything it produces leaves
the rest of the pipeline untouched.

For each recorded call, on the session it was made for:

```
expected  = adj_close[D] * (1 + pred_return)        the model's price target
direction = LONG if pred_return > 0 else SHORT
```

* **No trade** when the open has already passed the target (open >= expected for
  a long, <= for a short). The predicted move happened overnight, before any
  order could be worked; the money sits out.
* Otherwise the **open is the entry**, and the position closes on the first of:
  the **stop** (`entry*(1-sl)` long, `entry*(1+sl)` short), the **target**, or
  the **close**. A day that touched both the stop and the target cannot be
  ordered from a daily bar, so the **stop** is taken — the conservative read,
  and the one that stops a tight stop scoring well on a tie-break.

Capital compounds per ticker from $10,000 with fractional shares, and the
balance is floored to whole cents. Stops of 2%, 5% and 8% are run side by side.

Two readings of this rule are wrong by construction, and `tests/test_strategy.py`
pins both out. Selling at the target on a gap-through day books a loss on a
session that moved the predicted way (buy at 102, sell at the 101 target) — ~32%
of sessions. Exiting at the day's low when the target is missed assumes you sell
at the single worst tick, every session — another ~32%. Together they turned
$10,000 into $0.04 over 1,428 sessions on a 10-ticker sample; the rule above
leaves it between $6.6k and $12.6k depending on the stop.

```sh
python3 -m src.strategy                 # write latest/strategy.{parquet,json}
python3 -m src.strategy --dry-run       # totals only, write nothing
```

It runs automatically after a daily run (`scripts/bootstrap.sh`) and after a
rebuild (`src.merge`), before the Mongo publish that ships it.

### Cost

An LS-SVM fit solves a dense `(n+1)x(n+1)` system, so cost is `O(n^3)` in the
training-window size. At the default rolling window of 1,260 sessions a fit is
~170 ms, giving roughly **1 hour for the full 100-ticker backtest on a 16-vCPU
pod**. The knobs, in order of effect: `backtest.window_sessions` (cubic),
`backtest.refit_every` (linear), `pso.swarm_size` x `pso.iterations`.

> Note: use `numpy.linalg.solve`, not `scipy.linalg.solve`, for the bordered
> system. The matrix is symmetric *indefinite*, and scipy's symmetric path was
> measured 49x slower here for an answer identical to 1e-14.

## Layout

```
config/experiment.yaml   every tunable in one place
src/storage.py           the read-only guarantee
src/data.py              adjusted OHLCV (split-only volume factor)
src/indicators.py        the paper's five indicators, all causal
src/lssvm.py             eq 1-10
src/pso.py               eq 11-12
src/walkforward.py       the daily loop + the no-leak assertion
src/metrics.py           direction, baselines, per-year
src/run.py               entrypoint
src/daily.py             grade -> learn -> guess, the everyday path
src/strategy.py          the $10k-per-stock stop-loss paper trade (reads only)
src/publish_mongo.py     latest/ -> MongoDB Atlas, for the deployed dashboard
scripts/launch.sh        bundle -> results/ResearchGateEOD/code -> CPU pod -> self-terminate
scripts/reap_pods.sh     host-side reaper: deletes pods whose run has finished
tests/                   leakage proof, model sanity, storage guards
```
