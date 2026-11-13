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
