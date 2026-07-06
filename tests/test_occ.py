"""OCC symbol construction/parsing — a wrong strike in the routing table
sends orders to the wrong contract."""

import datetime

from occ import build_occ_symbol, parse_occ_expiry, parse_occ_symbol


class TestBuild:
    def test_call(self):
        sym = build_occ_symbol("SPY", datetime.date(2026, 5, 13), "CALL", 560.0)
        assert sym == "SPY260513C00560000"

    def test_put_half_dollar_strike(self):
        sym = build_occ_symbol("SPY", datetime.date(2026, 7, 6), "PUT", 623.5)
        assert sym == "SPY260706P00623500"

    def test_no_float_drift(self):
        # 0.1 + 0.2 style float error must not corrupt the strike encoding
        sym = build_occ_symbol("SPY", datetime.date(2026, 1, 2), "CALL", 689.5000000001)
        assert sym.endswith("00689500")


class TestParse:
    def test_roundtrip(self):
        for side, strike in [("CALL", 560.0), ("PUT", 623.5), ("CALL", 1000.0)]:
            sym = build_occ_symbol("SPY", datetime.date(2026, 5, 20), side, strike)
            parsed_side, parsed_strike = parse_occ_symbol(sym)
            assert parsed_side == side.lower()
            assert parsed_strike == strike

    def test_garbage_returns_none(self):
        assert parse_occ_symbol("not-a-symbol") == (None, None)
        assert parse_occ_symbol("SPY260520X00740000") == (None, None)
        assert parse_occ_symbol("") == (None, None)


class TestParseExpiry:
    def test_expiry_roundtrip(self):
        d   = datetime.date(2026, 7, 6)
        sym = build_occ_symbol("SPY", d, "CALL", 625.0)
        assert parse_occ_expiry(sym) == d

    def test_garbage_returns_none(self):
        assert parse_occ_expiry("nope") is None
        # the ghost sweeper uses this to scope 0DTE-only reaping
        assert parse_occ_expiry("SPY261399C00625000") is None   # month 13
