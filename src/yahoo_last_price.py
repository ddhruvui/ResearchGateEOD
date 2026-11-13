"""Every ticker's latest price from Yahoo Finance, into MongoDB LastPrice.LastPrice —
and with --midrun, today's bar so far into data/midrun/, as src.ib_today --upload does.

LastPrice: the same document src.ib_today --upload updates ({_id: "latest",
uploadedAt, values: [{ticker, price}]}, see publish_mongo.publish_last_price), but
needs no IB session, so the .github/workflows/last-price.yml schedule can run it
every 10 minutes from Sunday 20:00 to Friday 20:00 New York time.

Price is the latest trade Yahoo's keyless chart endpoint knows of
(query1.finance.yahoo.com/v8/finance/chart/<T>, 1-minute bars with pre/post market):
the newest bar's close when it is later than regularMarketPrice, so pre-market
(04:00-09:30 ET) and after-hours (16:00-20:00 ET) trades count. Yahoo has no
overnight session, so from 20:00 to 04:00 the price stays at the 20:00 one.
A ticker Yahoo fails on keeps its previous price in the document.

--midrun: per ticker, a one-bar array in the vendor's data/ohlcv/<T>.json row format,
the same file src.ib_today --upload writes, uploaded through storage.MidrunStore:

    [{"date", "open", "high", "low", "close", "adjusted_close", "volume"}]

open/high/low/volume are Yahoo's regular-session (09:30-16:00 ET) day bar
(chart/<T>?interval=1d). For 2026-11-10, 11 and 12 they matched the vendor's rows on
all 151 tickers: open/high/low/close to a median 0.000% (147-151 of 151 within 0.01%,
worst 0.6%) and volume to a median 0.01-0.02%. IB's historical bar undercounts volume
by about half. close (= adjusted_close) is the price above, with high/low widened to
include it. The day bar is a second request per ticker because the 1-minute bars'
first open missed the official open on 16 of 151 tickers (up to 0.8%) on 2026-11-13.

Before the 09:30 open (from 04:00 ET) the bar is the pre-market so far, from the same
1-minute bars: first trade's open, their high/low, latest trade as close. Its volume
is 0: Yahoo's pre-market minute bars carry prices but no volume (checked 2026-11-13).
The model uses volume only in MFI, so the bar's price is what counts.

A ticker with no trade today (after midnight, weekend, holiday) is left out and keeps
its old file. When no ticker has one, data/midrun is not touched, like src.ib_today.
_run.json says "source": "yahoo" and which session the bars are from.

An IB upload wins. data/midrun is left alone while its _run.json is src.ib_today's
from today and either under IB_HOLD old (time to run scripts/midrun.sh on it) or
newer than Yahoo's newest trade (IB's overnight session, which Yahoo has no data for).
A Yahoo upload with no new trade since the last one is skipped too, so _run.json's
as_of stays the time of the newest data. --force (scripts/midrun.sh YAHOO=1) replaces
even a fresh IB upload. --need-bars (also midrun.sh's) makes the exit status say
whether data/midrun now holds today's bars, which midrun.sh predicts from next:
0 yes (this run's, or an IB upload left in place), 3 no bar for today.

Requests are capped at 5 per second (RATE, retries included). The prices take about
30 s, and --midrun's day bars about 30 s more while a session has traded today.
If Yahoo returns no prices at all, Mongo and data/midrun are left untouched and
the run fails.

  python -m src.yahoo_last_price              # every ticker in tickers.json -> Mongo
  python -m src.yahoo_last_price --midrun     # ... and today's bars -> data/midrun/
  python -m src.yahoo_last_price --midrun --force   # ... even over a fresh IB upload
  python -m src.yahoo_last_price QCOM NVDA    # just these
  python -m src.yahoo_last_price --dry-run    # print prices (and bars), write nothing
"""
from __future__ import annotations

import argparse
import json
import ssl
import sys
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .config import Env, load_config, load_dotenv, load_tickers
from .storage import MIDRUN_PREFIX, MidrunStore, SourceStore

URL = "https://query1.finance.yahoo.com/v8/finance/chart/{}?interval=1m&range=1d&includePrePost=true"
DAY_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{}?interval=1d&range=1d"
# Yahoo refuses the default Python-urllib agent.
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; last-price/1.0)"}
WORKERS = 5
TRIES = 3
RATE = 5.0          # requests per second, hard limit, retries included
ET = ZoneInfo("America/New_York")
IB_HOLD = timedelta(minutes=30)


class RateLimiter:
    """At most `rate` calls per second across all threads: wait() hands out start
    slots spaced 1/rate apart, so no rolling one-second window ever sees more than
    `rate` requests."""

    def __init__(self, rate: float):
        self.interval = 1.0 / rate
        self.next = 0.0
        self.lock = threading.Lock()

    def wait(self) -> None:
        with self.lock:
            now = time.monotonic()
            slot = max(now, self.next)
            self.next = slot + self.interval
        time.sleep(max(0.0, slot - now))


LIMIT = RateLimiter(RATE)


def _ssl_context() -> ssl.SSLContext:
    try:                       # python.org builds on macOS ship without a CA bundle
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


SSL = _ssl_context()


def _fetch(url: str, parse, timeout: float = 15):
    """parse(chart payload); raises on any failure after TRIES attempts."""
    err: Exception | None = None
    for attempt in range(TRIES):
        try:
            LIMIT.wait()
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=timeout, context=SSL) as resp:
                return parse(json.load(resp))
        except Exception as exc:             # 429s and timeouts are worth a retry
            err = exc
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"{type(err).__name__}: {err}")


def fetch_quote(ticker: str, day: str | None = None, timeout: float = 15) -> dict:
    """{price, time, regular_day[, premarket]} for one ticker (see parse_chart)."""
    return _fetch(URL.format(urllib.parse.quote(ticker)), lambda p: parse_chart(p, day), timeout)


def fetch_day_bar(ticker: str, timeout: float = 15) -> dict:
    """The newest regular-session day bar for one ticker (see parse_day_bar)."""
    return _fetch(DAY_URL.format(urllib.parse.quote(ticker)), parse_day_bar, timeout)


def _result(payload: dict) -> dict:
    result = (payload.get("chart") or {}).get("result") or []
    if not result:
        raise ValueError(f"no result: {(payload.get('chart') or {}).get('error')}")
    return result[0]


def parse_chart(payload: dict, day: str | None = None) -> dict:
    """{price, time, regular_day}: the latest trade, when it happened, and the New York
    date of the regular-session price (yesterday's until today's 09:30 open). When
    `day` has no regular-session trade yet, also "premarket": its bar so far from the
    1-minute bars (see minute_bar), or None."""
    r = _result(payload)
    meta = r["meta"]
    price, ts = meta.get("regularMarketPrice"), meta.get("regularMarketTime") or 0
    regular_day = datetime.fromtimestamp(ts, ET).date().isoformat()
    # The newest minute bar with a trade: pre-market or after-hours when it is
    # later than the regular-session price.
    closes = ((r.get("indicators") or {}).get("quote") or [{}])[0].get("close") or []
    for t, c in reversed(list(zip(r.get("timestamp") or [], closes))):
        if isinstance(c, (int, float)) and c > 0:
            if t > ts:
                price, ts = c, t
            break
    if not isinstance(price, (int, float)) or price <= 0:
        raise ValueError(f"bad price {price!r}")
    out = {"price": round(float(price), 4),
           "time": datetime.fromtimestamp(ts, timezone.utc),
           "regular_day": regular_day}
    if day and regular_day != day:
        out["premarket"] = minute_bar(r, day)
    return out


def _positive(x) -> bool:
    return isinstance(x, (int, float)) and x > 0


def minute_bar(r: dict, day: str) -> dict | None:
    """{date, open, high, low, volume} over `day`'s 1-minute bars (New York date) — the
    pre-market so far before the open — or None when none has traded."""
    q = ((r.get("indicators") or {}).get("quote") or [{}])[0]
    cols = [q.get(k) or [] for k in ("open", "high", "low", "volume")]
    rows = []
    for i, t in enumerate(r.get("timestamp") or []):
        if datetime.fromtimestamp(t, ET).date().isoformat() != day:
            continue
        o, h, lo, v = (c[i] if i < len(c) else None for c in cols)
        if _positive(o) and _positive(h) and _positive(lo):
            rows.append((o, h, lo, v if _positive(v) else 0))
    if not rows:
        return None
    return {"date": day, "open": round(float(rows[0][0]), 4),
            "high": round(float(max(x[1] for x in rows)), 4),
            "low": round(float(min(x[2] for x in rows)), 4),
            "volume": int(round(sum(x[3] for x in rows)))}


def parse_day_bar(payload: dict) -> dict:
    """{date, open, high, low, volume}: the newest day bar with a real open/high/low,
    dated in New York. Yahoo's values are float32s (357.9649963378906), so they are
    rounded to 4 places."""
    r = _result(payload)
    q = ((r.get("indicators") or {}).get("quote") or [{}])[0]
    ts = r.get("timestamp") or []

    def at(key, i):
        col = q.get(key) or []
        return col[i] if i < len(col) else None

    for i in reversed(range(len(ts))):
        o, h, lo = at("open", i), at("high", i), at("low", i)
        if _positive(o) and _positive(h) and _positive(lo):
            v = at("volume", i)
            return {"date": datetime.fromtimestamp(ts[i], ET).date().isoformat(),
                    "open": round(float(o), 4), "high": round(float(h), 4),
                    "low": round(float(lo), 4),
                    "volume": int(round(v)) if _positive(v) else 0}
    raise ValueError("no day bar")


def to_bar(day: dict, price: float) -> dict:
    """One data/ohlcv/<T>.json row: the day bar's open/high/low/volume with the latest
    trade as close. A pre-market or after-hours trade can sit outside the 09:30-16:00
    range, so high/low widen to keep low <= close <= high (as ib_today.apply_live)."""
    return {"date": day["date"], "open": day["open"],
            "high": max(day["high"], price), "low": min(day["low"], price),
            "close": price, "adjusted_close": price, "volume": day["volume"]}


def fetch_all(tickers: list[str], day: str | None = None
              ) -> tuple[dict[str, dict], dict[str, dict], dict[str, str]]:
    """(quotes, bars, errors) by ticker. With `day` (YYYY-MM-DD), also that day's bar:
    the regular-session day bar once the session has traded, else the pre-market so
    far. The day-bar request is skipped before the open: it would only return
    yesterday's bar. Each quote's "bar_session" says which ("regular"/"pre-market")."""
    def one(t):
        try:
            q = fetch_quote(t, day)
        except Exception as exc:
            return t, None, None, str(exc)
        if not day:
            return t, q, None, None
        if q["regular_day"] != day:
            pm = q.pop("premarket", None)
            if not pm:
                return t, q, None, None
            q["bar_session"] = "pre-market"
            return t, q, to_bar(pm, q["price"]), None
        q["bar_session"] = "regular"
        try:
            d = fetch_day_bar(t)
        except Exception as exc:
            return t, q, None, f"day bar: {exc}"
        return t, q, (to_bar(d, q["price"]) if d["date"] == day else None), None

    quotes, bars, errors = {}, {}, {}
    with ThreadPoolExecutor(WORKERS) as pool:
        for t, q, b, e in pool.map(one, tickers):
            if q:
                quotes[t] = q
            if b:
                bars[t] = b
            if e:
                errors[t] = e
    return quotes, bars, errors


def keep_reason(prev: dict | None, day: str, now: datetime, newest: datetime,
                count: int, force: bool = False) -> str | None:
    """Why data/midrun's current upload (`prev`, its _run.json) must stay, or None to
    replace it with this run's `count` bars whose newest trade is `newest`.

    An IB upload (any _run.json not marked "source": "yahoo"; those written before
    the field existed are all IB's) from `day` stays while it is under IB_HOLD old or
    newer than Yahoo's newest trade, unless `force`. A Yahoo one stays when nothing has
    traded since: it already holds these bars."""
    if not prev or prev.get("date") != day:
        return None
    if prev.get("source", "ib") == "yahoo":
        if prev.get("newest_trade") == _et(newest) and prev.get("count") == count:
            return f"no trade since the last Yahoo upload (newest {_et(newest)[11:16]} ET)"
        return None
    if force:
        return None
    try:
        at = datetime.fromisoformat(prev["as_of"])
    except (KeyError, TypeError, ValueError):
        return None
    if at.tzinfo is None:
        at = at.replace(tzinfo=ET)
    if now - at < IB_HOLD:
        return (f"IB uploaded it {int((now - at).total_seconds() // 60)} min ago "
                f"(it stays {int(IB_HOLD.total_seconds() // 60)} min)")
    if at >= newest:
        return (f"IB's upload ({at.astimezone(ET):%H:%M} ET) is newer than Yahoo's "
                f"newest trade ({newest.astimezone(ET):%H:%M} ET)")
    return None


def _et(t: datetime) -> str:
    return t.astimezone(ET).isoformat(timespec="seconds")


def manifest(bars: dict[str, dict], tickers: list[str], day: str, now: datetime,
             newest: datetime, session: str = "regular") -> dict:
    """data/midrun/_run.json. Same keys src.midrun reads from IB's (date, as_of).
    session: "regular", "pre-market", or "pre-market+regular" around the open."""
    return {
        "date": day, "as_of": now.isoformat(timespec="seconds"), "source": "yahoo",
        "session": session, "close": "latest trade incl. pre-market and after-hours",
        "newest_trade": _et(newest),
        "count": len(bars), "missing": sorted(set(tickers) - set(bars)),
    }


def upload_midrun(store: MidrunStore | None, src: SourceStore | None, bars: dict,
                  quotes: dict, tickers: list[str], day: str, now: datetime,
                  force: bool = False) -> str:
    """Put this run's bars in data/midrun/ unless keep_reason() says the ones there
    stay. With no store (a dry run), only reports what it would do. Returns what
    happened: "uploaded", "current" (data/midrun already holds these Yahoo bars),
    "kept" (an IB upload stays), "no_bars" or "dry_run"."""
    if not bars:
        print(f"midrun: no {day} session bar yet (before the 09:30 ET open, weekend or "
              f"holiday) — data/midrun untouched", file=sys.stderr)
        return "no_bars"
    newest = max(quotes[t]["time"] for t in bars)
    missing = sorted(set(tickers) - set(bars))
    prev = src.try_get_json(f"{MIDRUN_PREFIX}/_run.json") if src else None
    why = keep_reason(prev, day, now, newest, len(bars), force)
    if why:
        print(f"midrun: keeping the upload in data/midrun — {why}", file=sys.stderr)
        return "current" if prev.get("source") == "yahoo" else "kept"
    if store is None:
        print(f"midrun: dry run — would upload {len(bars)} bars to data/midrun/",
              file=sys.stderr)
        return "dry_run"
    session = "+".join(sorted({quotes[t].get("bar_session", "regular") for t in bars}))
    store.put_run(bars, manifest(bars, tickers, day, now, newest, session))
    print(f"midrun: uploaded {len(bars)} {day} {session} bars to {store.url()} "
          f"(newest trade {_et(newest)[11:16]} ET)", file=sys.stderr)
    if missing:
        print(f"midrun: no {day} bar for {len(missing)}, their old files stay: "
              f"{', '.join(missing[:10])}{' ...' if len(missing) > 10 else ''}", file=sys.stderr)
    return "uploaded"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("tickers", nargs="*", help="default: every ticker in tickers.json")
    ap.add_argument("--midrun", action="store_true",
                    help="also upload today's bar so far to data/midrun/ (like src.ib_today --upload)")
    ap.add_argument("--force", action="store_true",
                    help="with --midrun: replace even a fresh IB upload (scripts/midrun.sh YAHOO=1)")
    ap.add_argument("--need-bars", action="store_true",
                    help="with --midrun: exit 0 when data/midrun then holds today's bars, 3 when "
                         "there is no bar for today; LastPrice problems only warn (scripts/midrun.sh)")
    ap.add_argument("--dry-run", action="store_true", help="print prices (and bars), write nothing")
    args = ap.parse_args(argv)
    if (args.force or args.need_bars) and not args.midrun:
        ap.error("--force and --need-bars only apply with --midrun")

    tickers = [t.upper() for t in args.tickers] or load_tickers(load_config())
    now = datetime.now(ET)
    day = now.date().isoformat()

    load_dotenv()
    store = src = None
    if args.midrun:
        try:    # fail on volume credentials before spending a minute on Yahoo
            env = Env.load()
            src = SourceStore(env)
            store = None if args.dry_run else MidrunStore(env)
        except Exception as exc:
            if not args.dry_run:
                raise
            print(f"midrun: no volume access ({exc}) — not reading data/midrun/_run.json",
                  file=sys.stderr)

    t0 = time.monotonic()
    quotes, bars, errors = fetch_all(tickers, day if args.midrun else None)
    print(f"Yahoo: {len(quotes)}/{len(tickers)} prices"
          + (f", {len(bars)} {day} bars" if args.midrun else "")
          + f" in {time.monotonic() - t0:.1f}s", file=sys.stderr)
    for t, e in sorted(errors.items()):
        print(f"  {t}: {e}", file=sys.stderr)

    if args.dry_run:
        for t in sorted(quotes):
            q, b = quotes[t], bars.get(t)
            line = f"{t:6} {q['price']:>10.2f}  {q['time']:%Y-%m-%d %H:%M:%SZ}"
            if b:
                line += (f"  bar {b['date']} O {b['open']:.2f} H {b['high']:.2f} "
                         f"L {b['low']:.2f} V {b['volume']:,}")
            print(line)
        if args.midrun and quotes:
            upload_midrun(None, src, bars, quotes, tickers, day, now, args.force)
        return 0 if quotes else 1
    if not quotes:
        print("no prices at all — leaving Mongo and data/midrun untouched", file=sys.stderr)
        return 1

    rc = 0
    from . import publish_mongo
    uri = publish_mongo.mongo_uri()
    if not uri:
        print("MONGO_URI not set — skipping LastPrice", file=sys.stderr)
        rc = 1
    else:
        try:    # a Mongo failure must not stop the data/midrun upload below
            n = publish_mongo.publish_last_price({t: q["price"] for t, q in quotes.items()}, uri)
            print(f"updated Mongo LastPrice: {len(quotes)} prices, {n} tickers in the doc",
                  file=sys.stderr)
        except Exception as exc:
            print(f"LastPrice upload FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
            rc = 1
    if args.midrun:
        status = upload_midrun(store, src, bars, quotes, tickers, day, now, args.force)
        if args.need_bars:
            # midrun.sh predicts from data/midrun next, so only its bars decide the exit;
            # LastPrice is a side effect here, as in src.ib_today --upload. "kept" is an
            # IB upload from today, as good a bar to predict from.
            return 0 if status in ("uploaded", "current", "kept") else 3
    return rc


if __name__ == "__main__":
    sys.exit(main())
