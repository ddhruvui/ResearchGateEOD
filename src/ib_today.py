"""Today's bar so far, from Interactive Brokers, in the vendor's data/ohlcv/<T>.json format.

Adapted from ~/Development/Stocks/InteractiveBroker/get_ohlcv.py. At 14:00 ET it gives,
per ticker, the session's open / high / low, the latest trade as close, and volume so
far — the same keys the acquisition pipeline publishes once the day is over:

    [{"date": "2026-10-27", "open": .., "high": .., "low": .., "close": ..,
      "adjusted_close": .., "volume": ..}]

adjusted_close == close: the newest bar always carries adjustment factor 1 (the vendor
file shows the same on its last row), and splits/dividends only rescale older bars.

open/high/low/close come from IB's 1-day historical bar (09:30-16:00 ET): checked against
the vendor's 2026-10-26 row, close matched exactly on 149/149 stocks and O/H/L to a median
0.001%. volume comes from the live feed instead — see live_volumes(). A past --date has
no live feed, so its volume undercounts by about half.

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

Needs Trader Workstation (or IB Gateway) running on this machine with the API enabled,
and `pip install ib_async`. Connects read-only. Local tool — the pods never run this.
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import ROOT, Env, load_config, load_tickers
from .storage import MidrunStore

ET = ZoneInfo("America/New_York")
OUT_ROOT = ROOT / "cache" / "ib_today"

# 7496 = TWS live, 7497 = TWS paper, 4001/4002 = IB Gateway live/paper
DEFAULT_PORT = 7496
# IB allows ~50 historical data requests open at once
CONCURRENCY = 40
# TWS allows ~100 concurrent market data lines by default
BATCH_SIZE = 50
# Seconds to wait for a batch's live volumes; every ticker arrived within 8 s in testing
LIVE_WAIT_SEC = 10.0
LIVE_PASSES = 3


def _load_dotenv(path: Path = ROOT / ".env") -> None:
    """KEY=VALUE lines from .env into os.environ, never overriding what is already set
    (same rule as scripts/_common.sh)."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.removeprefix("export ").strip(), v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        os.environ.setdefault(k, v)


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


async def live_volumes(ib, contracts, wait: float = LIVE_WAIT_SEC) -> dict[str, int]:
    """{IB symbol: today's volume} from the live feed (tick 8).

    IB's historical TRADES bars leave out a large share of the day's prints: on
    2026-10-26 the 09:30-16:00 bar's volume was 10-69% (median 49%) below the vendor's
    for all 149 stocks, while open/high/low/close matched. The live feed's day volume is
    the full consolidated figure, about 2x the bar — the vendor's basis."""
    ib.reqMarketDataType(3)   # live where subscribed, else 15-min delayed
    out = {}
    # The first batch after connecting is slow to fill (14 of 50 still empty after 10 s
    # in testing), so retry whatever is missing.
    for _ in range(LIVE_PASSES):
        todo = [c for c in contracts if c.symbol not in out]
        if not todo:
            break
        out.update(await _live_pass(ib, todo, wait))
    return out


async def _live_pass(ib, contracts, wait: float) -> dict[str, int]:
    import math

    out = {}
    for i in range(0, len(contracts), BATCH_SIZE):
        batch = contracts[i:i + BATCH_SIZE]
        tickers = [ib.reqMktData(c, "", False, False) for c in batch]
        for _ in range(int(wait / 0.25)):
            await asyncio.sleep(0.25)
            if all(t.volume == t.volume and t.volume > 0 for t in tickers):
                break
        for c, t in zip(batch, tickers):
            ib.cancelMktData(c)
            if t.volume is not None and not math.isnan(t.volume) and t.volume > 0:
                out[c.symbol] = int(round(t.volume))
    return out


async def fetch_bars(ib, symbols: list[str], day: dt.date, all_hours: bool) -> tuple[dict, dict]:
    """({our ticker: bar}, {our ticker: volume source}) for `day`. A ticker whose newest
    bar is not `day` (pre-open, holiday, halted) is left out rather than passed off as
    that day's bar."""
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
    # The live feed only knows today; a past day keeps the (low) historical volume.
    live = await live_volumes(ib, [c for c, _ in got]) if today else {}
    out, vol_src = {}, {}
    for c, b in got:
        bar = to_bar(day.isoformat(), b)
        # The live day volume is a superset of the bar's, so a smaller one is not today's:
        # after 20:00 ET it resets to the overnight session (NVDA read 163,444 at 23:21
        # against a 46.9M bar).
        use_live = live.get(c.symbol, 0) >= bar["volume"]
        if use_live:
            bar["volume"] = live[c.symbol]
        out[ours[c.symbol]] = bar
        vol_src[ours[c.symbol]] = "live" if use_live else "historical_bar"
    return out, vol_src


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
        _load_dotenv()
        store = MidrunStore(Env.load())

    symbols = [s.upper() for s in args.tickers] or load_tickers(load_config())
    now = dt.datetime.now(ET)
    day = dt.date.fromisoformat(args.date) if args.date else now.date()

    ib = IB()
    ib.connect(args.host, args.port, clientId=args.client_id, readonly=True)
    try:
        bars, vol_src = ib.run(fetch_bars(ib, symbols, day, args.all_hours))
    finally:
        ib.disconnect()
    missing = sorted(set(symbols) - set(bars))
    hist_vol = sorted(t for t, s in vol_src.items() if s != "live")
    manifest = {
        "date": day.isoformat(), "as_of": now.isoformat(timespec="seconds"),
        "session": "all_hours" if args.all_hours else "regular",
        "count": len(bars), "missing": missing,
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
    if hist_vol:
        print(f"volume from IB's historical bar (undercounts ~2x) for {len(hist_vol)}: "
              f"{', '.join(hist_vol[:10])}{' ...' if len(hist_vol) > 10 else ''}", file=sys.stderr)
    if missing:
        print(f"no {day} bar for: {', '.join(missing)}", file=sys.stderr)
    return 0 if bars else 1


if __name__ == "__main__":
    sys.exit(main())
