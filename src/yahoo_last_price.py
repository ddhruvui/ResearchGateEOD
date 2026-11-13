"""Every ticker's latest price from Yahoo Finance, into MongoDB LastPrice.LastPrice.

Same document src.ib_today --upload updates ({_id: "latest", uploadedAt, values:
[{ticker, price}]}, see publish_mongo.publish_last_price), but needs no IB session,
so the .github/workflows/last-price.yml schedule can run it every 10 minutes from
Sunday 20:00 to Friday 20:00 New York time.

Price is the latest trade Yahoo's keyless chart endpoint knows of
(query1.finance.yahoo.com/v8/finance/chart/<T>, 1-minute bars with pre/post market):
the newest bar's close when it is later than regularMarketPrice, so pre-market
(04:00-09:30 ET) and after-hours (16:00-20:00 ET) trades count. Yahoo has no
overnight session, so from 20:00 to 04:00 the price stays at the 20:00 one.

A ticker Yahoo fails on keeps its previous price in the document. Requests are
capped at 5 per second (RATE, retries included), so all 151 tickers take about
30 s. If Yahoo returns nothing at all, Mongo is left untouched and the run fails.

  python -m src.yahoo_last_price              # every ticker in tickers.json -> Mongo
  python -m src.yahoo_last_price QCOM NVDA    # just these
  python -m src.yahoo_last_price --dry-run    # print prices, no Mongo
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
from datetime import datetime, timezone

from .config import load_config, load_dotenv, load_tickers

URL = "https://query1.finance.yahoo.com/v8/finance/chart/{}?interval=1m&range=1d&includePrePost=true"
# Yahoo refuses the default Python-urllib agent.
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; last-price/1.0)"}
WORKERS = 5
TRIES = 3
RATE = 5.0          # requests per second, hard limit, retries included


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


def fetch_quote(ticker: str, timeout: float = 15) -> dict:
    """{price, time} for one ticker; raises on any failure after TRIES attempts."""
    url = URL.format(urllib.parse.quote(ticker))
    err: Exception | None = None
    for attempt in range(TRIES):
        try:
            LIMIT.wait()
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=timeout, context=SSL) as resp:
                return parse_chart(json.load(resp))
        except Exception as exc:             # 429s and timeouts are worth a retry
            err = exc
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"{type(err).__name__}: {err}")


def parse_chart(payload: dict) -> dict:
    result = (payload.get("chart") or {}).get("result") or []
    if not result:
        raise ValueError(f"no result: {(payload.get('chart') or {}).get('error')}")
    r = result[0]
    meta = r["meta"]
    price, ts = meta.get("regularMarketPrice"), meta.get("regularMarketTime") or 0
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
    return {"price": round(float(price), 4),
            "time": datetime.fromtimestamp(ts, timezone.utc)}


def fetch_all(tickers: list[str]) -> tuple[dict[str, dict], dict[str, str]]:
    """(quotes by ticker, error by ticker)."""
    def one(t):
        try:
            return t, fetch_quote(t), None
        except Exception as exc:
            return t, None, str(exc)
    quotes, errors = {}, {}
    with ThreadPoolExecutor(WORKERS) as pool:
        for t, q, e in pool.map(one, tickers):
            if q:
                quotes[t] = q
            else:
                errors[t] = e
    return quotes, errors


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("tickers", nargs="*", help="default: every ticker in tickers.json")
    ap.add_argument("--dry-run", action="store_true", help="print prices, skip Mongo")
    args = ap.parse_args(argv)

    tickers = [t.upper() for t in args.tickers] or load_tickers(load_config())
    t0 = time.monotonic()
    quotes, errors = fetch_all(tickers)
    print(f"Yahoo: {len(quotes)}/{len(tickers)} prices in {time.monotonic() - t0:.1f}s",
          file=sys.stderr)
    for t, e in sorted(errors.items()):
        print(f"  {t}: {e}", file=sys.stderr)

    if args.dry_run:
        for t in sorted(quotes):
            q = quotes[t]
            print(f"{t:6} {q['price']:>10.2f}  {q['time']:%Y-%m-%d %H:%M:%SZ}")
        return 0 if quotes else 1
    if not quotes:
        print("no prices at all — leaving Mongo untouched", file=sys.stderr)
        return 1

    load_dotenv()
    from . import publish_mongo
    uri = publish_mongo.mongo_uri()
    if not uri:
        print("MONGO_URI not set", file=sys.stderr)
        return 1
    n = publish_mongo.publish_last_price({t: q["price"] for t, q in quotes.items()}, uri)
    print(f"updated Mongo LastPrice: {len(quotes)} prices, {n} tickers in the doc", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
