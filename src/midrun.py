"""Midrun: next-session direction from today's PARTIAL bar. Predict only.

    python -m src.midrun                    # every ticker, against the volume
    python -m src.midrun --dry-run          # compute and print, write nothing
    python -m src.midrun --local DIR        # read DIR/data/..., write DIR/results/... (no S3)

Per ticker:

  1. LOAD     data/ohlcv/<T>.json, plus data/midrun/<T>.json appended IN MEMORY when its
              bar is dated after the vendor's last bar (src/ib_today.py --upload puts it
              there). Neither file is written; data/ohlcv is only ever read.
  2. MODEL    exactly the official model of the last overnight run: same tuned C/gamma
              (latest/ticker_info.json), same training rows (the window ending at the
              vendor's last bar). The partial bar is never a training label — its
              "return" is not a finished day.
  3. PREDICT  next session's return from the features of the newest bar (the partial
              one when appended), and its direction.

Nothing is graded and no state is advanced: latest/, ticker_info.json and the
predictions ledger are untouched, so the overnight daily run carries on exactly as
before. Re-running during the day just predicts again from the newer partial bar.

Writes ONLY under results/ResearchGateEOD/runs/<run_id>/midrun/:
  midrun/latest/next_session.parquet|json   the most recent run
  midrun/<DATE>/<STAMP>/...                 every run, kept

Caveat: a partial bar's volume is only the day so far, so the volume-based feature
(MFI) reads lower than it will at the close.
"""
from __future__ import annotations

import os

for _v in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS",
           "NUMEXPR_NUM_THREADS", "VECLIB_MAXIMUM_THREADS"):
    os.environ.setdefault(_v, "1")

import argparse
import io
import json
import sys
import time
import traceback
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from .calendar_util import holiday_set, next_session
from .config import DEFAULT_RESULTS_PREFIX, Env, load_config, load_dotenv, load_tickers
from .daily import stamped_at
from .data import load_adjusted
from .indicators import FEATURES, build_features, next_session_return
from .lssvm import LSSVM
from .storage import MIDRUN_PREFIX, LayeredSource, ResultStore
from .walkforward import _train_slice

_SRC = None


class LocalSource:
    """READ-ONLY stand-in for LayeredSource over a local copy of the volume's keys."""

    def __init__(self, root: Path):
        self._root = Path(root)

    @property
    def bucket(self) -> str:
        return f"local:{self._root}"

    def get_json(self, key: str):
        return json.loads((self._root / key).read_text())

    def try_get_json(self, key: str):
        p = self._root / key
        return json.loads(p.read_text()) if p.exists() else None


class LocalResults:
    """Stand-in for ResultStore writing under a local results/ResearchGateEOD/ tree."""

    def __init__(self, root: Path):
        self._root = Path(root) / DEFAULT_RESULTS_PREFIX
        self.prefix = DEFAULT_RESULTS_PREFIX

    def url(self, key: str = "") -> str:
        return str(self._root / key)

    def get_bytes(self, key: str) -> bytes:
        return (self._root / key).read_bytes()

    def put_bytes(self, key: str, blob: bytes) -> None:
        p = self._root / key
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(blob)

    def put_json(self, key: str, obj) -> None:
        self.put_bytes(key, json.dumps(obj, indent=2, default=str).encode())

    def put_dataframe(self, key: str, df) -> None:
        buf = io.BytesIO()
        df.to_parquet(buf, index=False)
        self.put_bytes(key, buf.getvalue())


def _init_worker(local: str | None):
    global _SRC
    _SRC = LocalSource(Path(local)) if local else LayeredSource(Env.load())


def predict_one(src, ticker: str, cfg: dict, dials: dict | None):
    """(row, info) for one ticker. Reads only; returns nothing to write."""
    try:
        midrun = src.try_get_json(f"{MIDRUN_PREFIX}/{ticker}.json")
        midrun = midrun if isinstance(midrun, list) else None
        bars = load_adjusted(src, ticker, cfg, extra_rows=midrun)
        bt = cfg["backtest"]
        if bars.empty or len(bars) < int(bt["min_train_rows"]) + 40:
            return None, {"ticker": ticker, "skipped": "insufficient history"}

        dates = pd.DatetimeIndex(bars["date"])
        j = len(bars) - 1
        mid_dates = {str(r.get("date", ""))[:10] for r in midrun or [] if isinstance(r, dict)}
        appended = bool(bars.attrs.get("appended")) and str(dates[j].date()) in mid_dates

        feats = build_features(bars, cfg)
        X = feats[FEATURES].to_numpy(dtype=float)
        y = next_session_return(bars).to_numpy(dtype=float)
        if not np.isfinite(X[j]).all():
            return None, {"ticker": ticker, "note": "features not ready"}

        # The official model predicting from the vendor's last bar k trains on rows < k.
        # Keep exactly that window when a partial bar sits at j = k + 1: training through
        # j - 1 would make y[k] = partial/close_k - 1 a label, and that day is not over.
        k = j - 1 if appended else j
        sl = _train_slice(k, bt["window"], int(bt["window_sessions"]))
        assert sl.stop <= k, "training window must end before the official as-of bar"
        m = (np.isfinite(X).all(axis=1) & np.isfinite(y))[sl]
        Xt, yt = X[sl][m], y[sl][m]
        if len(yt) < int(bt["min_train_rows"]):
            return None, {"ticker": ticker, "skipped": "below min_train_rows"}

        # No PSO here: re-tuning is the overnight run's job and must not be duplicated
        # or diverge. Missing dials fall back to the same defaults daily.py uses.
        C = (dials or {}).get("C")
        gamma = (dials or {}).get("gamma")
        tuned = C is not None and gamma is not None
        if not tuned:
            C, gamma = 1.0, 1.0 / max(X.shape[1], 1)

        model = LSSVM(C=C, gamma=gamma, kernel=cfg["model"]["kernel"]).fit(Xt, yt)
        pred = float(model.predict(X[j:j + 1])[0])
        if not np.isfinite(pred):
            return None, {"ticker": ticker, "note": "non-finite prediction"}

        last_close = float(bars["adj_close"].iloc[j])
        row = {
            "ticker": ticker, "as_of": dates[j], "bar": "midrun" if appended else "ohlcv",
            "official_as_of": dates[k], "last_close": last_close,
            "official_close": float(bars["adj_close"].iloc[k]),
            "pred_return": pred, "direction": "up" if pred > 0 else "down",
            "implied_close": last_close * (1 + pred), "n_train": int(len(yt)),
            "C": float(C), "gamma": float(gamma),
            "dials_tuned_at": stamped_at(dials) if tuned else None,
        }
        return row, {"ticker": ticker, "bar": row["bar"], "tuned": tuned}
    except Exception:
        return None, {"ticker": ticker, "error": traceback.format_exc(limit=4)}


def _one(ticker, cfg, dials):
    return predict_one(_SRC, ticker, cfg, dials)


def midrun_key(base: str, rel: str) -> str:
    """Every write goes through here: pinned under <base>/midrun/, never latest/."""
    key = f"{base}/midrun/{rel}"
    if ".." in rel.split("/") or not key.startswith(f"{base}/midrun/"):
        raise ValueError(f"midrun write escaped its folder: {key!r}")
    return key


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--config", default=None)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=int(os.environ.get("WORKERS", "0")))
    ap.add_argument("--dry-run", action="store_true", help="compute but write nothing")
    ap.add_argument("--local", help="read DIR/data/... and DIR/results/... instead of S3; write there too")
    args = ap.parse_args(argv)

    cfg = load_config(args.config)
    if args.local:
        src, dst = LocalSource(Path(args.local)), LocalResults(Path(args.local))
    else:
        load_dotenv()
        env = Env.load()
        src, dst = LayeredSource(env), ResultStore(env)
    # Belt and braces on top of ResultStore's results/ guard: this project's own
    # prefix and nothing else — never results/ResearchGate.
    if dst.prefix != DEFAULT_RESULTS_PREFIX:
        print(f"FATAL: results prefix is {dst.prefix!r}; midrun writes only to "
              f"{DEFAULT_RESULTS_PREFIX}", file=sys.stderr)
        return 2

    tickers = load_tickers(cfg)
    if args.limit:
        tickers = tickers[:args.limit]
    base = f"{cfg['output']['prefix']}/{cfg['run_id']}"

    try:
        dials = {r["ticker"]: r for r in json.loads(dst.get_bytes(f"{base}/latest/ticker_info.json"))
                 if isinstance(r, dict) and "ticker" in r}
    except Exception:
        dials = {}
        print("!! no latest/ticker_info.json — using default C/gamma, not the tuned ones",
              file=sys.stderr)
    ib_run = src.try_get_json(f"{MIDRUN_PREFIX}/_run.json") or {}

    print(f"read      : {src.bucket} data/ohlcv + {MIDRUN_PREFIX} (READ-ONLY)", flush=True)
    print(f"write to  : {dst.url(midrun_key(base, ''))}", flush=True)
    print(f"midrun as of: {ib_run.get('as_of', 'unknown')}", flush=True)

    t0 = time.time()
    from .run import container_resources
    cpus, total_gb, _ = container_resources()
    workers = max(1, min(args.workers or max(1, cpus - 1), cpus, max(1, int(total_gb // 1.5))))
    rows, infos = [], []
    if workers <= 1:
        for t in tickers:
            r, i = predict_one(src, t, cfg, dials.get(t))
            rows += [r] if r else []
            infos.append(i)
    else:
        with ProcessPoolExecutor(max_workers=workers, initializer=_init_worker,
                                 initargs=(args.local,)) as pool:
            futs = [pool.submit(_one, t, cfg, dials.get(t)) for t in tickers]
            for f in as_completed(futs):
                r, i = f.result()
                rows += [r] if r else []
                infos.append(i)

    errs = [i for i in infos if "error" in i]
    for e in errs[:3]:
        print(f"  !! {e['ticker']}: {e['error'].splitlines()[-1]}", file=sys.stderr)
    if not rows:
        print("no predictions", file=sys.stderr)
        return 1

    df = pd.DataFrame(rows).sort_values("ticker").reset_index(drop=True)
    target = next_session(pd.Timestamp(df["as_of"].max()), holiday_set(src))
    df["for_session"] = target
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    n_mid = int((df["bar"] == "midrun").sum())
    meta = {
        "mode": "midrun", "stamp": stamp, "midrun_as_of": ib_run.get("as_of"),
        "for_session": str(target.date()), "predicted": len(df),
        "from_midrun_bar": n_mid, "from_ohlcv_bar": len(df) - n_mid,
        "up": int((df["direction"] == "up").sum()), "down": int((df["direction"] == "down").sum()),
        "untuned": sorted(i["ticker"] for i in infos if i.get("tuned") is False),
        "errors": len(errs), "seconds": round(time.time() - t0, 1),
    }

    print(f"\n{'ticker':<7}{'bar':<8}{'as_of':<12}{'close':>10}{'pred':>9}  dir")
    for r in df.itertuples():
        print(f"{r.ticker:<7}{r.bar:<8}{r.as_of.date()!s:<12}{r.last_close:>10.2f}"
              f"{r.pred_return:>+9.4f}  {r.direction}")
    print(f"\nfor {target.date()}: {meta['up']} up / {meta['down']} down · "
          f"{n_mid} from the midrun bar, {len(df) - n_mid} from ohlcv · "
          f"errors {len(errs)} · {meta['seconds']}s")

    if args.dry_run:
        print("dry run — nothing written")
        return 0
    records = json.loads(df.to_json(orient="records", date_format="iso"))
    for pre in (f"{df['as_of'].max().date()}/{stamp}", "latest"):
        dst.put_dataframe(midrun_key(base, f"{pre}/next_session.parquet"), df)
        dst.put_json(midrun_key(base, f"{pre}/next_session.json"), records)
        dst.put_json(midrun_key(base, f"{pre}/run_meta.json"), meta)
    print(f"wrote {dst.url(midrun_key(base, 'latest/'))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
