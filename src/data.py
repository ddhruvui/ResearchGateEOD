"""Load EOD bars from the read-only source volume and build an adjusted OHLCV frame.

The vendor file carries RAW open/high/low/close/volume plus a single adjusted_close.
Three of the paper's five indicators read high/low/volume, so those must be put on
the same basis as adjusted_close or every split shows up as a fake gap:

    rho          = adjusted_close / close          (split AND dividend)
    adj_o/h/l    = raw * rho
    adj_volume   = raw volume * cumulative SPLIT-ONLY factor

Volume gets the split factor alone: a dividend does not change the share count, so
applying rho to volume would corrupt money-flow. Same reasoning as the reference
implementation in InvestOpediaClaude/src/data/panel.py.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .storage import SourceStore

_COLS = ["date", "open", "high", "low", "close", "adjusted_close", "volume"]


def source_key(cfg: dict, ticker: str) -> str:
    """Key of a ticker's EOD file: <source_prefix>/<T>.json unless data.source_keys
    overrides it (e.g. the ETFs the acquisition files under data/watchlist/market/)."""
    override = (cfg["data"].get("source_keys") or {}).get(ticker)
    return override or f"{cfg['data']['source_prefix']}/{ticker}.json"


def _split_ratio(raw) -> float | None:
    """'4.000000/1.000000' -> 4.0 ; also tolerates a bare number."""
    if raw is None:
        return None
    s = str(raw).strip()
    try:
        if "/" in s:
            num, den = s.split("/", 1)
            num, den = float(num), float(den)
            return num / den if den else None
        return float(s)
    except (TypeError, ValueError):
        return None


def split_volume_factor(splits: list | None, dates: pd.DatetimeIndex) -> np.ndarray:
    """Cumulative multiplier putting historical share counts on today's basis.

    After a k:1 split with ex-date s, volume strictly BEFORE s is multiplied by k.
    """
    factor = np.ones(len(dates), dtype=float)
    if not splits:
        return factor
    for row in splits:
        if not isinstance(row, dict):
            continue
        ratio = _split_ratio(row.get("split"))
        d = row.get("date")
        if not ratio or ratio <= 0 or not d:
            continue
        factor[dates < pd.Timestamp(d)] *= ratio
    return factor


def newer_rows(rows: list, extra: list | None) -> list:
    """The rows of `extra` dated after the newest row of `rows` — the intraday bar from
    data/midrun/<T>.json once the vendor file has not caught up to its day yet."""
    if not extra:
        return []
    last = max((str(r.get("date", ""))[:10] for r in rows if isinstance(r, dict)), default="")
    return [r for r in extra if isinstance(r, dict) and str(r.get("date", ""))[:10] > last]


def load_adjusted(source: SourceStore, ticker: str, cfg: dict,
                  extra_rows: list | None = None) -> pd.DataFrame:
    """Return a date-sorted adjusted OHLCV frame for one ticker. Read-only on source.

    `extra_rows` (same row format) are appended IN MEMORY when dated after the vendor's
    last bar; the source file itself is only ever read."""
    key = source_key(cfg, ticker)
    rows = source.get_json(key)
    extra = newer_rows(rows, extra_rows)
    df = pd.DataFrame(list(rows) + extra)
    missing = [c for c in _COLS if c not in df.columns]
    if missing:
        raise ValueError(f"{ticker}: source file missing columns {missing}")

    df = df[_COLS].copy()
    df["date"] = pd.to_datetime(df["date"])
    df = df.dropna(subset=["date"]).sort_values("date").reset_index(drop=True)
    for c in _COLS[1:]:
        df[c] = pd.to_numeric(df[c], errors="coerce")

    # Drop bars with no usable close on either basis — cannot be adjusted or scored.
    df = df[(df["close"] > 0) & (df["adjusted_close"] > 0)].reset_index(drop=True)
    if df.empty:
        return df

    rho = df["adjusted_close"] / df["close"]
    out = pd.DataFrame({
        "date": df["date"],
        "adj_open": df["open"] * rho,
        "adj_high": df["high"] * rho,
        "adj_low": df["low"] * rho,
        "adj_close": df["adjusted_close"],
    })

    splits = source.try_get_json(f"{cfg['data']['splits_prefix']}/{ticker}.json")
    vf = split_volume_factor(splits if isinstance(splits, list) else None,
                             pd.DatetimeIndex(df["date"]))
    out["adj_volume"] = df["volume"].to_numpy() * vf

    # High/low must bracket the close; a handful of vendor bars violate this.
    bad = (out["adj_high"] < out["adj_low"]) | out[["adj_open", "adj_high", "adj_low",
                                                    "adj_close"]].isna().any(axis=1)
    out = out[~bad].reset_index(drop=True)
    out.attrs["appended"] = len(extra)
    return out
