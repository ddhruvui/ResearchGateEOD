"""src.midrun: appends the intraday bar in memory only, predicts with the official
model's training window, and writes nowhere but runs/<run_id>/midrun/."""
from __future__ import annotations

import hashlib
import json

import numpy as np
import pandas as pd
import pytest

from src.config import load_config
from src.data import load_adjusted, newer_rows
from src.midrun import LocalSource, main, midrun_key, predict_one


def _bars(n=600, start="2024-01-02", seed=0):
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range(start, periods=n)
    close = 100 * np.exp(np.cumsum(rng.normal(0, 0.01, n)))
    rows = []
    for d, c in zip(dates, close):
        o = c * (1 + rng.normal(0, 0.003))
        rows.append({"date": str(d.date()), "open": round(o, 4),
                     "high": round(max(o, c) * 1.005, 4), "low": round(min(o, c) * 0.995, 4),
                     "close": round(c, 4), "adjusted_close": round(c, 4),
                     "volume": int(rng.integers(1_000_000, 5_000_000))})
    return rows


def _next_day(rows, close_mult=1.01):
    last = rows[-1]
    d = (pd.Timestamp(last["date"]) + pd.offsets.BDay(1)).date()
    c = last["close"] * close_mult
    return [{"date": str(d), "open": last["close"], "high": max(c, last["close"]) * 1.002,
             "low": min(c, last["close"]) * 0.998, "close": c, "adjusted_close": c,
             "volume": 1_234_567}]


class _Src:
    def __init__(self, files):
        self.files = files

    def get_json(self, key):
        return self.files[key]

    def try_get_json(self, key):
        return self.files.get(key)


CFG = load_config()


def test_newer_rows_keeps_only_bars_after_the_vendor():
    rows = _bars(5)
    nxt = _next_day(rows)
    assert newer_rows(rows, nxt) == nxt
    assert newer_rows(rows, [dict(rows[-1])]) == []          # same day: vendor wins
    assert newer_rows(rows, [dict(rows[0])]) == []
    assert newer_rows(rows, None) == []


def test_load_adjusted_appends_in_memory_and_leaves_the_source_alone():
    rows = _bars(300)
    before = json.dumps(rows)
    src = _Src({"data/ohlcv/AAPL.json": rows})
    df = load_adjusted(src, "AAPL", CFG, extra_rows=_next_day(rows))
    assert len(df) == 301 and df.attrs["appended"] == 1
    assert str(df["date"].iloc[-1].date()) == _next_day(rows)[0]["date"]
    assert json.dumps(rows) == before
    assert load_adjusted(src, "AAPL", CFG).attrs["appended"] == 0


def test_partial_bar_is_predicted_from_but_never_trained_on():
    rows = _bars()
    vendor_only = predict_one(_Src({"data/ohlcv/AAPL.json": rows}), "AAPL", CFG, None)[0]
    preds = []
    for mult in (0.95, 1.05):        # the partial bar's close must not move the model
        r = predict_one(_Src({"data/ohlcv/AAPL.json": rows,
                              "data/midrun/AAPL.json": _next_day(rows, mult)}), "AAPL", CFG, None)[0]
        assert r["bar"] == "midrun"
        assert r["as_of"] == pd.Timestamp(_next_day(rows)[0]["date"])
        assert r["official_as_of"] == vendor_only["as_of"]
        assert r["n_train"] == vendor_only["n_train"]
        preds.append(r["pred_return"])
    assert preds[0] != preds[1]      # ... but its features do drive the prediction


def test_stale_midrun_bar_falls_back_to_the_vendor_bar():
    rows = _bars()
    r = predict_one(_Src({"data/ohlcv/AAPL.json": rows,
                          "data/midrun/AAPL.json": [dict(rows[-1], close=1.0)]}), "AAPL", CFG, None)[0]
    assert r["bar"] == "ohlcv" and r["last_close"] == pytest.approx(rows[-1]["close"])


def test_tuned_dials_are_used_as_stored():
    rows = _bars()
    r = predict_one(_Src({"data/ohlcv/AAPL.json": rows}), "AAPL", CFG,
                    {"C": 3.0, "gamma": 0.05, "tuned_at": "2026-10-01"})[0]
    assert (r["C"], r["gamma"], r["dials_tuned_at"]) == (3.0, 0.05, "2026-10-01")


@pytest.mark.parametrize("bad", ["../latest/next_session.parquet", "x/../../latest/a.json"])
def test_midrun_key_cannot_reach_latest(bad):
    with pytest.raises(ValueError):
        midrun_key("runs/pso_lssvm_v1", bad)


def _tree(root):
    return {str(p.relative_to(root)): hashlib.md5(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file()}


def test_end_to_end_writes_only_under_midrun(tmp_path):
    for t in ("AAL", "AAPL"):
        rows = _bars(seed=len(t))
        (tmp_path / "data/ohlcv").mkdir(parents=True, exist_ok=True)
        (tmp_path / "data/midrun").mkdir(parents=True, exist_ok=True)
        (tmp_path / f"data/ohlcv/{t}.json").write_text(json.dumps(rows))
        (tmp_path / f"data/midrun/{t}.json").write_text(json.dumps(_next_day(rows)))
    latest = tmp_path / "results/ResearchGateEOD/runs/pso_lssvm_v1/latest"
    latest.mkdir(parents=True)
    (latest / "ticker_info.json").write_text(json.dumps([{"ticker": "AAL", "C": 2.0, "gamma": 0.1}]))
    (tmp_path / "results/ResearchGate/runs").mkdir(parents=True)
    (tmp_path / "results/ResearchGate/runs/keep.json").write_text("{}")
    before = _tree(tmp_path)

    assert main(["--local", str(tmp_path), "--limit", "2", "--workers", "1"]) == 0

    after = _tree(tmp_path)
    assert {k: v for k, v in after.items() if k in before} == before   # nothing changed
    new = set(after) - set(before)
    assert new and all(k.startswith("results/ResearchGateEOD/runs/pso_lssvm_v1/midrun/")
                       for k in new)
    out = json.loads((tmp_path / "results/ResearchGateEOD/runs/pso_lssvm_v1/midrun/latest/"
                      "next_session.json").read_text())
    assert [r["ticker"] for r in out] == ["AAL", "AAPL"]
    assert {r["bar"] for r in out} == {"midrun"} and out[0]["C"] == 2.0
