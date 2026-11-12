"""Today's bar so far, from Interactive Brokers, in the vendor's data/ohlcv/<T>.json format.

Adapted from ~/Development/Stocks/InteractiveBroker/get_ohlcv.py. At 14:00 ET it gives,
per ticker, the session's open / high / low, the latest trade as close, and volume so
far — the same keys the acquisition pipeline publishes once the day is over:

    [{"date": "2026-10-27", "open": .., "high": .., "low": .., "close": ..,
      "adjusted_close": .., "volume": ..}]

adjusted_close == close: the newest bar always carries adjustment factor 1 (the vendor
file shows the same on its last row), and splits/dividends only rescale older bars.

open/high/low come from IB's 1-day historical bar (09:30-16:00 ET): checked against the
vendor's 2026-10-26 row, O/H/L matched to a median 0.001%. close is the live feed's last
trade (tick 4/68) and volume its day volume — see live_quotes() and apply_live(). The
last trade can fall outside the 09:30-16:00 range (pre-market, after hours, IB's overnight
session from 20:00 ET), so high/low widen to include it. A ticker with no last trade keeps
the bar's close, and _run.json's "close_from_historical_bar" lists it. A past --date has
no live feed: close is that day's bar close (matched the vendor exactly on 149/149) and
volume undercounts by about half.

  python -m src.ib_today                    # every ticker in tickers.json
  python -m src.ib_today NVDA AAPL          # just these
  python -m src.ib_today --date 2026-10-26  # a past day (to check against the vendor)
  python -m src.ib_today --stdout           # print the JSON instead of writing files

  python -m src.ib_today --upload           # also upload to s3://crimtr8kbf/data/midrun/

Writes cache/ib_today/<DATE>_<HHMMSS>/<TICKER>.json (a one-bar array, same shape as
data/ohlcv/<T>.json) plus _run.json (as-of time, tickers with no bar). --upload puts
the same files at data/midrun/<TICKER>.json and data/midrun/_run.json on the volume,
overwriting the previous run's, through storage.MidrunStore — which cannot write any
other key, so data/ohlcv/ is never touched. A ticker with no bar this run keeps its old
midrun file; its "date" and _run.json's "missing" show it is stale. Credentials come
from the environment or this repo's .env.

--upload also rewrites one MongoDB document, LastPrice.LastPrice {_id: "latest"}:
{uploadedAt, values: [{ticker, price}, ...]}, price being each bar's close (the last
trade). Skipped when MONGO_URI is unset; see publish_mongo.publish_last_price().

Needs Trader Workstation (or IB Gateway) running on this machine with the API enabled,
and `pip install ib_async`. Connects read-only. Local tool — the pods never run this.
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import ROOT, Env, load_config, load_dotenv, load_tickers
from .storage import MidrunStore

ET = ZoneInfo("America/New_York")
OUT_ROOT = ROOT / "cache" / "ib_today"

# 7496 = TWS live, 7497 = TWS paper, 4001/4002 = IB Gateway live/paper
DEFAULT_PORT = 7496
# IB allows ~50 historical data requests open at once
CONCURRENCY = 40
# TWS allows ~100 concurrent market data lines by default
BATCH_SIZE = 50
# Seconds to wait for a batch's live quotes; every ticker arrived within 8 s in testing
LIVE_WAIT_SEC = 10.0
LIVE_PASSES = 3


def to_ib_symbol(symbol: str) -> str:
    # IB uses a space for share classes: BRK-B -> BRK B
    return symbol.upper().replace("-", " ").replace(".", " ")


def to_bar(day: str, b) -> dict:
    """One IB daily BarData -> one row of data/ohlcv/<T>.json."""
    return {
        "date": day,
        "open": float(b.open),
        "high": float(b.high),
        "low": float(b.low),
        "close": float(b.close),
        "adjusted_close": float(b.close),
        "volume": int(round(float(b.volume))),
    }


async def _fetch(ib, contract, end, all_hours, sem):
    async with sem:
        bars = await ib.reqHistoricalDataAsync(
            contract, endDateTime=end, durationStr="1 D", barSizeSetting="1 day",
            whatToShow="TRADES", useRTH=not all_hours, formatDate=1,
        )
    return contract, (bars[-1] if bars else None)


def _positive(x) -> float | None:
    """x if it is a real price/size > 0; IB leaves an unset field NaN (or -1)."""
    import math

    return float(x) if x is not None and not math.isnan(x) and x > 0 else None


async def live_quotes(ib, contracts, wait: float = LIVE_WAIT_SEC) -> dict[str, dict]:
    """{IB symbol: {"last": price, "volume": shares}} from the live feed — last trade
    (tick 4, or 68 delayed) and day volume (tick 8/74). A field that never arrived is
    left out.

    IB's historical TRADES bars leave out a large share of the day's prints: on
    2026-10-26 the 09:30-16:00 bar's volume was 10-69% (median 49%) below the vendor's
    for all 149 stocks, while open/high/low/close matched. The live feed's day volume is
    the full consolidated figure, about 2x the bar — the vendor's basis."""
    ib.reqMarketDataType(3)   # live where subscribed, else 15-min delayed
    out: dict[str, dict] = {}
    # The first batch after connecting is slow to fill (14 of 50 still empty after 10 s
    # in testing), so retry whatever is missing.
    for _ in range(LIVE_PASSES):
        todo = [c for c in contracts if len(out.get(c.symbol, {})) < 2]
        if not todo:
            break
        for sym, q in (await _live_pass(ib, todo, wait)).items():
            out.setdefault(sym, {}).update(q)
    return out


async def _live_pass(ib, contracts, wait: float) -> dict[str, dict]:
    out = {}
    for i in range(0, len(contracts), BATCH_SIZE):
        batch = contracts[i:i + BATCH_SIZE]
        tickers = [ib.reqMktData(c, "", False, False) for c in batch]
        for _ in range(int(wait / 0.25)):
            await asyncio.sleep(0.25)
            if all(_positive(t.volume) and _positive(t.last) for t in tickers):
                break
        for c, t in zip(batch, tickers):
            ib.cancelMktData(c)
            q = {"last": _positive(t.last), "volume": _positive(t.volume)}
            q = {k: v for k, v in q.items() if v is not None}
            if q:
                out[c.symbol] = q
    return out


def apply_live(bar: dict, quote: dict | None) -> tuple[dict, str, str]:
    """(bar, close source, volume source): today's historical bar with the live feed's
    last trade as close/adjusted_close and its day volume, where they are usable.

    The last trade can sit outside the 09:30-16:00 bar (after hours, overnight), so
    high/low widen to keep low <= close <= high."""
    bar, quote = dict(bar), quote or {}
    close_src = vol_src = "historical_bar"
    last = quote.get("last")
    if last:
        bar["close"] = bar["adjusted_close"] = float(last)
        bar["high"] = max(bar["high"], bar["close"])
        bar["low"] = min(bar["low"], bar["close"])
        close_src = "live_last"
    # The live day volume is a superset of the bar's, so a smaller one is not today's:
    # after 20:00 ET it resets to the overnight session (NVDA read 163,444 at 23:21
    # against a 46.9M bar).
    vol = int(round(quote.get("volume") or 0))
    if vol >= bar["volume"] and vol > 0:
        bar["volume"] = vol
        vol_src = "live"
    return bar, close_src, vol_src


async def fetch_bars(ib, symbols: list[str], day: dt.date,
                     all_hours: bool) -> tuple[dict, dict, dict]:
    """({our ticker: bar}, {ticker: close source}, {ticker: volume source}) for `day`.
    A ticker whose newest bar is not `day` (pre-open, holiday, halted) is left out
    rather than passed off as that day's bar."""
    from ib_async import Stock

    ours = {to_ib_symbol(s): s for s in symbols}
    contracts = [Stock(ib_sym, "SMART", "USD") for ib_sym in ours]
    contracts = [c for c in await ib.qualifyContractsAsync(*contracts) if c and c.conId]
    today = day == dt.datetime.now(ET).date()
    # "" = up to now; a past day ends at its own midnight so IB returns that day's bar.
    end = "" if today else f"{day:%Y%m%d} 23:59:59 US/Eastern"
    sem = asyncio.Semaphore(CONCURRENCY)
    got = await asyncio.gather(*(_fetch(ib, c, end, all_hours, sem) for c in contracts))
    got = [(c, b) for c, b in got if b is not None and str(b.date)[:10] == day.isoformat()]
    # The live feed only knows today; a past day keeps the bar's close and (low) volume.
    live = await live_quotes(ib, [c for c, _ in got]) if today else {}
    out, close_src, vol_src = {}, {}, {}
    for c, b in got:
        t = ours[c.symbol]
        out[t], close_src[t], vol_src[t] = apply_live(to_bar(day.isoformat(), b),
                                                      live.get(c.symbol))
    return out, close_src, vol_src


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("tickers", nargs="*", help="default: every ticker in tickers.json")
    ap.add_argument("--date", help="YYYY-MM-DD, default today (ET)")
    ap.add_argument("--all-hours", action="store_true", help="include 04:00-09:30 and 16:00-20:00 ET")
    ap.add_argument("--stdout", action="store_true", help="print {ticker: [bar]} instead of writing files")
    ap.add_argument("--upload", action="store_true", help="also upload to data/midrun/ on the volume")
    ap.add_argument("--out", type=Path, help=f"output dir, default {OUT_ROOT.relative_to(ROOT)}/<DATE>_<HHMMSS>")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--client-id", type=int, default=31)
    args = ap.parse_args(argv)

    from ib_async import IB

    if args.upload:
        # Fail on credentials before spending ~20 s on IB, not after.
        load_dotenv()
        store = MidrunStore(Env.load())
        from . import publish_mongo
        mongo = publish_mongo.mongo_uri()

    symbols = [s.upper() for s in args.tickers] or load_tickers(load_config())
    now = dt.datetime.now(ET)
    day = dt.date.fromisoformat(args.date) if args.date else now.date()

    ib = IB()
    ib.connect(args.host, args.port, clientId=args.client_id, readonly=True)
    try:
        bars, close_src, vol_src = ib.run(fetch_bars(ib, symbols, day, args.all_hours))
    finally:
        ib.disconnect()
    missing = sorted(set(symbols) - set(bars))
    hist_close = sorted(t for t, s in close_src.items() if s != "live_last")
    hist_vol = sorted(t for t, s in vol_src.items() if s != "live")
    manifest = {
        "date": day.isoformat(), "as_of": now.isoformat(timespec="seconds"),
        "session": "all_hours" if args.all_hours else "regular",
        "close": "live last trade",
        "count": len(bars), "missing": missing,
        "close_from_historical_bar": hist_close,
        "volume_from_historical_bar": hist_vol,
    }

    if args.stdout:
        json.dump({t: [bars[t]] for t in sorted(bars)}, sys.stdout, indent=1)
        print()
    else:
        out = args.out or OUT_ROOT / f"{day}_{now:%H%M%S}"
        out.mkdir(parents=True, exist_ok=True)
        for t, b in bars.items():
            (out / f"{t}.json").write_text(json.dumps([b]))
        (out / "_run.json").write_text(json.dumps(manifest, indent=1))
        print(f"wrote {len(bars)} bars for {day} to {out} (as of {now:%H:%M} ET)", file=sys.stderr)
    if args.upload and bars:
        with ThreadPoolExecutor(max_workers=16) as pool:
            list(pool.map(lambda t: store.put_json(f"{t}.json", [bars[t]]), bars))
        store.put_json("_run.json", manifest)     # last: its as_of marks a complete upload
        print(f"uploaded {len(bars)} bars to {store.url()}", file=sys.stderr)
        if not mongo:
            print("MONGO_URI not set — skipping the LastPrice upload", file=sys.stderr)
        else:
            try:    # the volume already has the bars; a Mongo failure must not fail the run
                n = publish_mongo.publish_last_price({t: b["close"] for t, b in bars.items()}, mongo)
                print(f"updated Mongo LastPrice: {len(bars)} prices, {n} tickers in the doc",
                      file=sys.stderr)
            except Exception as exc:
                print(f"LastPrice upload FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
    if hist_close:
        print(f"no live last trade, close from IB's historical bar for {len(hist_close)}: "
              f"{', '.join(hist_close[:10])}{' ...' if len(hist_close) > 10 else ''}", file=sys.stderr)
    if hist_vol:
        print(f"volume from IB's historical bar (undercounts ~2x) for {len(hist_vol)}: "
              f"{', '.join(hist_vol[:10])}{' ...' if len(hist_vol) > 10 else ''}", file=sys.stderr)
    if missing:
        print(f"no {day} bar for: {', '.join(missing)}", file=sys.stderr)
    return 0 if bars else 1


if __name__ == "__main__":
    sys.exit(main())
