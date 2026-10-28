"""src.ib_today's output must be a drop-in row of the vendor's data/ohlcv/<T>.json."""
from types import SimpleNamespace

from src.data import _COLS
from src.ib_today import to_bar, to_ib_symbol


def test_bar_has_exactly_the_vendor_keys_and_types():
    b = SimpleNamespace(open=218.36, high=219.19, low=215.53, close=217.94, volume=46880690.0)
    row = to_bar("2026-10-27", b)
    assert list(row) == _COLS
    assert row["adjusted_close"] == row["close"] == 217.94
    assert isinstance(row["volume"], int) and row["volume"] == 46880690


def test_ib_symbol_uses_a_space_for_share_classes():
    assert to_ib_symbol("BRK-B") == "BRK B"
    assert to_ib_symbol("nvda") == "NVDA"
