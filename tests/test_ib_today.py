"""src.ib_today's output must be a drop-in row of the vendor's data/ohlcv/<T>.json."""
from types import SimpleNamespace

from src.data import _COLS
from src.ib_today import apply_live, to_bar, to_ib_symbol

BAR = {"date": "2026-11-11", "open": 178.49, "high": 182.17, "low": 172.94,
       "close": 176.57, "adjusted_close": 176.57, "volume": 4_000_000}


def test_bar_has_exactly_the_vendor_keys_and_types():
    b = SimpleNamespace(open=218.36, high=219.19, low=215.53, close=217.94, volume=46880690.0)
    row = to_bar("2026-10-27", b)
    assert list(row) == _COLS
    assert row["adjusted_close"] == row["close"] == 217.94
    assert isinstance(row["volume"], int) and row["volume"] == 46880690


def test_ib_symbol_uses_a_space_for_share_classes():
    assert to_ib_symbol("BRK-B") == "BRK B"
    assert to_ib_symbol("nvda") == "NVDA"


def test_live_last_trade_is_the_close():
    bar, close_src, vol_src = apply_live(BAR, {"last": 175.10, "volume": 8_500_000})
    assert bar["close"] == bar["adjusted_close"] == 175.10
    assert (bar["open"], bar["high"], bar["low"]) == (178.49, 182.17, 172.94)
    assert bar["volume"] == 8_500_000
    assert (close_src, vol_src) == ("live_last", "live")
    assert list(bar) == _COLS and BAR["close"] == 176.57    # input not mutated


def test_last_outside_the_session_range_widens_high_low():
    # 2026-11-11 22:51 ET: QCOM's overnight trade sat below the 09:30-16:00 low
    bar, _, _ = apply_live(BAR, {"last": 172.60})
    assert bar["low"] == bar["close"] == 172.60 and bar["high"] == 182.17
    bar, _, _ = apply_live(BAR, {"last": 183.00})
    assert bar["high"] == bar["close"] == 183.00 and bar["low"] == 172.94


def test_no_live_quote_keeps_the_historical_bar():
    for quote in (None, {}, {"volume": 9_512}):
        bar, close_src, vol_src = apply_live(BAR, quote)
        assert bar == BAR
        assert (close_src, vol_src) == ("historical_bar", "historical_bar")


def test_overnight_volume_reset_keeps_bar_volume_but_still_takes_last():
    bar, close_src, vol_src = apply_live(BAR, {"last": 172.60, "volume": 9_512})
    assert bar["close"] == 172.60 and bar["volume"] == 4_000_000
    assert (close_src, vol_src) == ("live_last", "historical_bar")
