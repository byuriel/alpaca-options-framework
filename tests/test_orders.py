"""OrderManager pure helpers — the None-vs-0.0 distinction is what keeps a
malformed order object from booking a fabricated 100% loss into the CSV."""

from types import SimpleNamespace

from orders import OrderManager


class TestGetFillPrice:
    def test_valid_price(self):
        o = SimpleNamespace(filled_avg_price="0.55")
        assert OrderManager.get_fill_price(o) == 0.55

    def test_none_order(self):
        assert OrderManager.get_fill_price(None) is None

    def test_missing_price_is_none_not_zero(self):
        # The old implementation returned 0.0 here — which call sites booked
        # as a full-loss exit. None forces callers to handle it.
        o = SimpleNamespace(filled_avg_price=None)
        assert OrderManager.get_fill_price(o) is None

    def test_zero_price_is_none(self):
        o = SimpleNamespace(filled_avg_price="0")
        assert OrderManager.get_fill_price(o) is None

    def test_garbage_is_none(self):
        o = SimpleNamespace(filled_avg_price="abc")
        assert OrderManager.get_fill_price(o) is None


class TestGetFilledQty:
    def test_valid(self):
        assert OrderManager.get_filled_qty(SimpleNamespace(filled_qty="3")) == 3

    def test_partial_float_string(self):
        assert OrderManager.get_filled_qty(SimpleNamespace(filled_qty="2.0")) == 2

    def test_none_order(self):
        assert OrderManager.get_filled_qty(None) == 0

    def test_missing(self):
        assert OrderManager.get_filled_qty(SimpleNamespace(filled_qty=None)) == 0
