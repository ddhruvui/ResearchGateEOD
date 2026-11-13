import subprocess
import threading
import time
from pathlib import Path

import pytest

from src import yahoo_last_price as y

WINDOW = Path(__file__).resolve().parents[1] / "scripts" / "last_price_window.sh"


def chart(rmp, rmt, ts=(), closes=()):
    return {"chart": {"result": [{"meta": {"regularMarketPrice": rmp, "regularMarketTime": rmt},
                                  "timestamp": list(ts),
                                  "indicators": {"quote": [{"close": list(closes)}]}}]}}


def test_parse_chart_reads_regular_market_price():
    q = y.parse_chart(chart(168.53, 1794586603))
    assert q["price"] == 168.53
    assert q["time"].tzinfo is not None


def test_parse_chart_prefers_a_later_extended_hours_bar():
    # after-hours bar at 1000 is newer than the regular close at 900; trailing null skipped
    q = y.parse_chart(chart(100.0, 900, ts=[800, 1000, 1060], closes=[99.0, 101.234567, None]))
    assert q["price"] == 101.2346
    assert q["time"].timestamp() == 1000


def test_parse_chart_keeps_regular_price_when_bars_are_older():
    q = y.parse_chart(chart(100.0, 900, ts=[800, 860], closes=[99.0, 99.5]))
    assert (q["price"], q["time"].timestamp()) == (100.0, 900)


@pytest.mark.parametrize("dow,hm,inside", [
    (7, "1950", False), (7, "2000", True), (7, "2350", True),   # Sunday evening start
    (1, "0000", True), (3, "1200", True), (4, "2359", True),
    (5, "1950", True), (5, "2000", True), (5, "2009", True),    # Friday 20:00 lands, late or not
    (5, "2010", False), (5, "2100", False),
    (6, "0000", False), (6, "1200", False), (7, "1200", False),
])
def test_last_price_window(dow, hm, inside):
    r = subprocess.run(["bash", str(WINDOW), str(dow), hm])
    assert (r.returncode == 0) == inside


@pytest.mark.parametrize("payload", [
    {"chart": {"result": None, "error": {"code": "Not Found"}}},
    {"chart": {"result": [{"meta": {"regularMarketPrice": None, "regularMarketTime": 0}}]}},
    {"chart": {"result": [{"meta": {"regularMarketPrice": 0, "regularMarketTime": 0}}]}},
])
def test_parse_chart_rejects_missing_or_bad_price(payload):
    with pytest.raises(ValueError):
        y.parse_chart(payload)


def test_rate_limiter_never_exceeds_rate_in_any_second():
    lim, starts, lock = y.RateLimiter(50.0), [], threading.Lock()

    def go():
        for _ in range(10):
            lim.wait()
            with lock:
                starts.append(time.monotonic())

    threads = [threading.Thread(target=go) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    starts.sort()
    assert len(starts) == 50
    # starts spaced >= 1/rate apart => never more than `rate` in any one-second window
    gaps = [b - a for a, b in zip(starts, starts[1:])]
    assert min(gaps) >= 1 / 50 - 0.005


# ---- --midrun: today's bar so far, in the vendor's row format ----

from datetime import datetime, timedelta, timezone   # noqa: E402

from src.data import _COLS                           # noqa: E402

ET = y.ET
DAY = "2026-11-13"
OPEN_TS = int(datetime(2026, 11, 13, 9, 30, tzinfo=ET).timestamp())


def day_chart(ts, o, h, lo, v):
    return {"chart": {"result": [{"meta": {}, "timestamp": list(ts), "indicators": {"quote": [
        {"open": list(o), "high": list(h), "low": list(lo), "volume": list(v)}]}}]}}


def test_parse_chart_gives_the_regular_session_day_in_new_york():
    # 16:00 ET on 2026-11-13 is 21:00 UTC; at 23:00 ET the UTC date is already the 14th
    close = int(datetime(2026, 11, 13, 16, 0, tzinfo=ET).timestamp())
    late = int(datetime(2026, 11, 13, 23, 0, tzinfo=ET).timestamp())
    assert y.parse_chart(chart(100.0, close))["regular_day"] == DAY
    assert y.parse_chart(chart(100.0, late))["regular_day"] == DAY


def test_parse_day_bar_rounds_float32_noise_and_dates_in_new_york():
    d = y.parse_day_bar(day_chart([OPEN_TS], [357.9649963378906], [360.8299865722656],
                                  [355.7550048828125], [38999578]))
    assert d == {"date": DAY, "open": 357.965, "high": 360.83, "low": 355.755, "volume": 38999578}


def test_parse_day_bar_skips_an_empty_trailing_bar():
    d = y.parse_day_bar(day_chart([OPEN_TS - 86400, OPEN_TS], [10.0, None], [11.0, None],
                                  [9.0, None], [500, None]))
    assert d["date"] == "2026-11-12" and d["open"] == 10.0


def test_parse_day_bar_rejects_no_bar():
    with pytest.raises(ValueError):
        y.parse_day_bar(day_chart([OPEN_TS], [None], [None], [None], [None]))


def test_bar_is_a_vendor_row_with_the_latest_trade_as_close():
    day = {"date": DAY, "open": 100.0, "high": 105.0, "low": 99.0, "volume": 1234}
    b = y.to_bar(day, 103.5)
    assert list(b) == _COLS
    assert b == {"date": DAY, "open": 100.0, "high": 105.0, "low": 99.0,
                 "close": 103.5, "adjusted_close": 103.5, "volume": 1234}
    # an after-hours trade outside the session range widens it
    assert (y.to_bar(day, 98.0)["low"], y.to_bar(day, 106.0)["high"]) == (98.0, 106.0)


def _quote(day, price=50.0, t=1000):
    return {"price": price, "time": datetime.fromtimestamp(t, timezone.utc), "regular_day": day}


def test_fetch_all_asks_for_a_day_bar_only_once_today_has_traded(monkeypatch):
    asked = []
    days = {"AAA": DAY, "BBB": "2026-11-12", "CCC": DAY}       # BBB: still yesterday's price
    monkeypatch.setattr(y, "fetch_quote", lambda t, day=None: _quote(days[t]))
    bar_days = {"AAA": DAY, "CCC": "2026-11-12"}               # CCC: Yahoo has no today bar

    def day_bar(t):
        asked.append(t)
        return {"date": bar_days[t], "open": 1.0, "high": 2.0, "low": 0.5, "volume": 7}

    monkeypatch.setattr(y, "fetch_day_bar", day_bar)
    quotes, bars, errors = y.fetch_all(["AAA", "BBB", "CCC"], DAY)
    assert set(quotes) == {"AAA", "BBB", "CCC"} and set(bars) == {"AAA"} and not errors
    assert sorted(asked) == ["AAA", "CCC"]
    # without a day (no --midrun) no day bar is requested at all
    asked.clear()
    assert y.fetch_all(["AAA"])[1] == {} and asked == []


def test_fetch_all_keeps_the_price_when_only_the_day_bar_fails(monkeypatch):
    monkeypatch.setattr(y, "fetch_quote", lambda t, day=None: _quote(DAY))

    def boom(t):
        raise RuntimeError("HTTPError: 429")

    monkeypatch.setattr(y, "fetch_day_bar", boom)
    quotes, bars, errors = y.fetch_all(["AAA"], DAY)
    assert "AAA" in quotes and not bars and errors["AAA"].startswith("day bar:")


NOW = datetime(2026, 11, 13, 12, 0, tzinfo=ET)


def _ib(minutes_ago, day=DAY, **extra):
    return {"date": day, "as_of": (NOW - timedelta(minutes=minutes_ago)).isoformat(
        timespec="seconds"), **extra}


@pytest.mark.parametrize("prev,newest_ago,keep", [
    (None, 1, False),
    (_ib(5), 1, True),                              # IB just uploaded: leave it for midrun.sh
    (_ib(5, source="ib"), 1, True),
    (_ib(29), 1, True),
    (_ib(31), 1, False),                            # 30 min on, Yahoo's newer trades win
    (_ib(90), 120, True),                           # IB newer than every Yahoo trade (overnight)
    (_ib(5, day="2026-11-12"), 1, False),           # yesterday's IB upload never blocks
    ({"date": DAY, "as_of": "garbage"}, 1, False),
])
def test_keep_reason_ib(prev, newest_ago, keep):
    newest = NOW - timedelta(minutes=newest_ago)
    assert (y.keep_reason(prev, DAY, NOW, newest, 151) is not None) == keep


def test_keep_reason_yahoo_skips_only_when_nothing_traded_since():
    newest = NOW - timedelta(minutes=2)
    prev = y.manifest({"AAA": {}}, ["AAA"], DAY, NOW - timedelta(minutes=10), newest)
    assert prev["source"] == "yahoo" and prev["missing"] == []
    assert y.keep_reason(prev, DAY, NOW, newest, 1) is not None
    assert y.keep_reason(prev, DAY, NOW, newest + timedelta(seconds=60), 1) is None
    assert y.keep_reason(prev, DAY, NOW, newest, 2) is None        # a ticker came back


class _Store:
    def __init__(self):
        self.runs = []

    def put_run(self, bars, manifest):
        self.runs.append((bars, manifest))

    def url(self, name=""):
        return "s3://vol/data/midrun/"


class _Src:
    def __init__(self, prev):
        self.prev = prev

    def try_get_json(self, key):
        assert key == "data/midrun/_run.json"
        return self.prev


def test_upload_midrun_writes_bars_and_a_yahoo_manifest():
    store, bars = _Store(), {"AAA": {"date": DAY}, "BBB": {"date": DAY}}
    quotes = {"AAA": _quote(DAY, t=1000), "BBB": _quote(DAY, t=2000)}
    assert y.upload_midrun(store, _Src(None), bars, quotes, ["AAA", "BBB", "CCC"], DAY,
                           NOW) == "uploaded"
    (sent, man), = store.runs
    assert sent is bars
    assert man["source"] == "yahoo" and man["date"] == DAY and man["missing"] == ["CCC"]
    assert man["newest_trade"] == datetime.fromtimestamp(2000, timezone.utc).astimezone(ET).isoformat()
    assert datetime.fromisoformat(man["as_of"]) == NOW


def test_upload_midrun_leaves_a_fresh_ib_upload_and_an_empty_day_alone():
    store, quotes = _Store(), {"AAA": _quote(DAY, t=int(NOW.timestamp()))}
    args = ({"AAA": {}}, quotes, ["AAA"], DAY, NOW)
    assert y.upload_midrun(store, _Src(_ib(5)), *args) == "kept"
    assert y.upload_midrun(store, _Src(None), {}, quotes, ["AAA"], DAY, NOW) == "no_bars"
    assert y.upload_midrun(None, _Src(None), *args) == "dry_run"
    assert store.runs == []


def test_force_replaces_a_fresh_ib_upload_but_not_identical_yahoo_bars():
    store, quotes = _Store(), {"AAA": _quote(DAY, t=int(NOW.timestamp()))}
    args = ({"AAA": {}}, quotes, ["AAA"], DAY, NOW)
    assert y.upload_midrun(store, _Src(_ib(1)), *args, force=True) == "uploaded"
    same = y.manifest({"AAA": {}}, ["AAA"], DAY, NOW, quotes["AAA"]["time"])
    assert y.upload_midrun(store, _Src(same), *args, force=True) == "current"
    assert len(store.runs) == 1


def _main(monkeypatch, bars, prev):
    """Run main() --midrun --force on one ticker with Yahoo, the volume and Mongo stubbed."""
    import types
    from src import publish_mongo

    store = _Store()
    today = datetime.now(ET).date().isoformat()
    quotes = {"AAA": _quote(today, t=int(datetime.now(ET).timestamp()) - 30)}
    monkeypatch.setattr(y, "load_dotenv", lambda: None)
    monkeypatch.setattr(y, "Env", types.SimpleNamespace(load=lambda: object()))
    monkeypatch.setattr(y, "MidrunStore", lambda env: store)
    monkeypatch.setattr(y, "SourceStore", lambda env: _Src(prev(today)))
    monkeypatch.setattr(y, "fetch_all", lambda t, day=None: (
        quotes, {k: {**v, "date": day} for k, v in bars.items()}, {}))
    monkeypatch.setattr(publish_mongo, "mongo_uri", lambda: None)   # no LastPrice
    return store


def _fresh_ib(today):
    return {"date": today, "as_of": datetime.now(ET).isoformat(timespec="seconds")}


def test_need_bars_exit_follows_data_midrun_not_lastprice(monkeypatch):
    bar = {"AAA": {"close": 1.0}}
    store = _main(monkeypatch, bar, _fresh_ib)            # YAHOO=1: replace the IB upload
    assert y.main(["AAA", "--midrun", "--force", "--need-bars"]) == 0 and len(store.runs) == 1
    store = _main(monkeypatch, bar, _fresh_ib)            # YAHOO=1 KEEP_IB=1: IB's bars stay
    assert y.main(["AAA", "--midrun", "--need-bars"]) == 0 and store.runs == []
    store = _main(monkeypatch, bar, _fresh_ib)            # cron: IB stays; no Mongo -> 1
    assert y.main(["AAA", "--midrun"]) == 1 and store.runs == []
    store = _main(monkeypatch, {}, _fresh_ib)             # no bar for today: 3, nothing written
    assert y.main(["AAA", "--midrun", "--force", "--need-bars"]) == 3 and store.runs == []


@pytest.mark.parametrize("flag", ["--force", "--need-bars"])
def test_force_and_need_bars_need_midrun(flag):
    with pytest.raises(SystemExit):
        y.main(["AAA", flag])


def minute_chart(rmt, rows):
    """1-minute chart: rows of (ts, open, high, low, close, volume)."""
    ts, o, h, lo, c, v = (list(x) for x in zip(*rows)) if rows else ([],) * 6
    return {"chart": {"result": [{"meta": {"regularMarketPrice": 100.0, "regularMarketTime": rmt},
                                  "timestamp": ts, "indicators": {"quote": [
                                      {"open": o, "high": h, "low": lo, "close": c, "volume": v}]}}]}}


def _et_ts(h, m, day=13):
    return int(datetime(2026, 11, day, h, m, tzinfo=ET).timestamp())


def test_before_the_open_the_bar_is_the_premarket_so_far():
    yesterday_close = _et_ts(16, 0, day=12)
    payload = minute_chart(yesterday_close, [
        (_et_ts(19, 59, day=12), 99.0, 99.5, 98.0, 99.2, 0),     # yesterday's after-hours
        (_et_ts(4, 0), 101.0, 101.5, 100.5, 101.2, 0),
        (_et_ts(7, 59), 101.2, 103.0, 100.8, 102.6, None),       # Yahoo: no pre-market volume
        (_et_ts(8, 0), None, None, None, None, None),            # empty minute
    ])
    q = y.parse_chart(payload, DAY)
    assert q["regular_day"] == "2026-11-12" and q["price"] == 102.6
    assert q["premarket"] == {"date": DAY, "open": 101.0, "high": 103.0, "low": 100.5, "volume": 0}
    b = y.to_bar(q["premarket"], q["price"])
    assert list(b) == _COLS and b["close"] == b["adjusted_close"] == 102.6


def test_no_premarket_bar_after_midnight_or_once_the_session_trades():
    after_midnight = minute_chart(_et_ts(16, 0, day=12), [(_et_ts(19, 59, day=12), 99, 99, 99, 99, 0)])
    assert y.parse_chart(after_midnight, DAY)["premarket"] is None
    trading = minute_chart(_et_ts(10, 0), [(_et_ts(10, 0), 101, 101, 101, 101, 500)])
    assert "premarket" not in y.parse_chart(trading, DAY)
    assert "premarket" not in y.parse_chart(after_midnight)        # no day asked for


def test_fetch_all_uses_the_premarket_bar_before_the_open(monkeypatch):
    pm = {"date": DAY, "open": 10.0, "high": 11.0, "low": 9.5, "volume": 0}
    monkeypatch.setattr(y, "fetch_quote",
                        lambda t, day=None: {**_quote("2026-11-12", price=10.5), "premarket": pm})
    monkeypatch.setattr(y, "fetch_day_bar", lambda t: pytest.fail("no day bar before the open"))
    quotes, bars, errors = y.fetch_all(["AAA"], DAY)
    assert bars["AAA"]["close"] == 10.5 and bars["AAA"]["volume"] == 0
    assert quotes["AAA"]["bar_session"] == "pre-market" and "premarket" not in quotes["AAA"]
    store = _Store()
    y.upload_midrun(store, _Src(None), bars, quotes, ["AAA"], DAY, NOW)
    assert store.runs[0][1]["session"] == "pre-market"
