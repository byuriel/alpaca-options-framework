"""Reconciliation — the matching core must catch every way the local record
and the broker can disagree, and must aggregate partial exit legs correctly."""

import csv
import os

import pytest

import config
import reconcile
from reconcile import load_local_orders, pending_failure_flag


def _order(oid, symbol="SPY260706C00604000", side="buy", qty=6, price=0.51):
    return {"id": oid, "symbol": symbol, "side": side, "qty": qty, "price": price}


def _entries(**kw):
    d = {"symbol": "SPY260706C00604000", "qty": 6, "price": 0.51}
    d.update(kw)
    return {"e1": d}


def _exits(**kw):
    d = {"symbol": "SPY260706C00604000", "qty": 6, "price": 0.76}
    d.update(kw)
    return {"x1": d}


class TestMatching:
    def test_clean_pass(self):
        r = reconcile.reconcile(_entries(), _exits(), [
            _order("e1", side="buy", qty=6, price=0.51),
            _order("x1", side="sell", qty=6, price=0.76),
        ])
        assert r["ok"] is True
        assert r["problems"] == []
        assert r["n_matched"] == 2

    def test_missing_broker_order(self):
        r = reconcile.reconcile(_entries(), {}, [])
        assert r["ok"] is False
        assert any("NOT FOUND" in p for p in r["problems"])

    def test_qty_mismatch(self):
        r = reconcile.reconcile(_entries(qty=6), {}, [_order("e1", qty=5)])
        assert any("qty mismatch" in p for p in r["problems"])

    def test_price_mismatch_beyond_tolerance(self):
        r = reconcile.reconcile(_entries(price=0.51), {}, [_order("e1", price=0.53)])
        assert any("price mismatch" in p for p in r["problems"])

    def test_price_within_tolerance_passes(self):
        r = reconcile.reconcile(_entries(price=0.51), {},
                                [_order("e1", price=0.512)])
        assert r["ok"] is True

    def test_side_mismatch(self):
        r = reconcile.reconcile(_entries(), {}, [_order("e1", side="sell")])
        assert any("side mismatch" in p for p in r["problems"])

    def test_unmatched_broker_order_surfaced(self):
        r = reconcile.reconcile({}, {}, [_order("ghost-1", side="sell", qty=2)])
        assert r["ok"] is False
        assert any("UNMATCHED broker order ghost-1" in p for p in r["problems"])

    def test_degraded_rows_are_notes_not_failures(self):
        r = reconcile.reconcile({}, {}, [], degraded=[{"symbol": "X", "qty": "3"}])
        assert r["ok"] is True
        assert len(r["notes"]) == 1


class TestCsvLoading:
    def _write(self, path, rows):
        cols = ["date", "symbol", "side", "strike", "entry_price", "exit_price",
                "qty", "reason", "realized_pnl", "entry_time", "exit_time",
                "fees", "entry_order_id", "exit_order_id"]
        with open(path, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=cols)
            w.writeheader()
            w.writerows(rows)

    def test_partial_legs_aggregate_per_entry_order(self, tmp_path):
        # One 6-lot entry closed in two legs (4 + 2, different exit orders):
        # the ENTRY order must be expected once with qty 6, not twice.
        self._write(tmp_path / "trades_2026-07-06.csv", [
            {"date": "2026-07-06", "symbol": "S", "entry_price": "0.51",
             "exit_price": "0.76", "qty": "4", "realized_pnl": "99",
             "entry_order_id": "e1", "exit_order_id": "xa"},
            {"date": "2026-07-06", "symbol": "S", "entry_price": "0.51",
             "exit_price": "0.70", "qty": "2", "realized_pnl": "37",
             "entry_order_id": "e1", "exit_order_id": "xb"},
        ])
        entries, exits, degraded = load_local_orders("2026-07-06", str(tmp_path))
        assert entries["e1"]["qty"] == 6
        assert exits["xa"]["qty"] == 4 and exits["xb"]["qty"] == 2
        assert degraded == []

    def test_recovered_sentinel_goes_to_degraded(self, tmp_path):
        self._write(tmp_path / "trades_2026-07-06.csv", [
            {"date": "2026-07-06", "symbol": "S", "entry_price": "0.40",
             "exit_price": "0.55", "qty": "3", "realized_pnl": "44",
             "entry_order_id": "recovered", "exit_order_id": "x9"},
        ])
        entries, exits, degraded = load_local_orders("2026-07-06", str(tmp_path))
        assert entries == {}
        assert "x9" in exits          # the exit is still verifiable
        assert len(degraded) == 1

    def test_missing_csv_is_empty_not_error(self, tmp_path):
        entries, exits, degraded = load_local_orders("2026-01-01", str(tmp_path))
        assert (entries, exits, degraded) == ({}, {}, [])


class TestFailureFlag:
    def test_flag_detection_and_absence(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "LOG_DIR", str(tmp_path))
        assert pending_failure_flag() is None
        flag = tmp_path / "reconcile_FAIL_2026-07-06.flag"
        flag.write_text("qty mismatch\n")
        found = pending_failure_flag()
        assert found is not None and found.endswith("reconcile_FAIL_2026-07-06.flag")
