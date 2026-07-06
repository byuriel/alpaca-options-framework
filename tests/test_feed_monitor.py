"""Feed monitor — dark symbols, quote gaps, and held-symbol coverage must be
measured correctly; these numbers gate the free-tier → OPRA decision."""

import csv
import datetime
import gzip
import json

import pytest

import config
from feed_monitor import analyze, COVERAGE_FAIL_BELOW

ET = config.ET


def _w(hh, mm, ss=0):
    return datetime.datetime(2026, 7, 6, hh, mm, ss, tzinfo=ET).timestamp()


def _write(path, events, meta=None):
    meta = meta or {"session_date": "2026-07-06",
                    "stock_feed": "iex", "option_feed": "indicative"}
    with gzip.open(path, "wt", encoding="utf-8") as f:
        f.write(json.dumps(["m", events[0][1], meta]) + "\n")
        for ev in events:
            f.write(json.dumps(ev) + "\n")


@pytest.fixture
def recording(tmp_path):
    """3 subscribed symbols: LIVE ticks steadily, GAPPY has a 60s hole,
    DARK never delivers a single quote."""
    LIVE, GAPPY, DARK = "SPY260706C00604000", "SPY260706P00600000", "SPY260706C00606000"
    events = [
        ["s", _w(9, 30, 5), [LIVE, GAPPY, DARK]],
    ]
    t = _w(9, 31, 0)
    while t < _w(9, 40, 0):
        events.append(["q", t, LIVE, 0.48, 0.52, "", 10, 12])
        t += 2.0
    events += [
        ["q", _w(9, 31, 0), GAPPY, 0.30, 0.40, "", 5, 5],    # wide spread
        ["q", _w(9, 32, 0), GAPPY, 0.31, 0.41, "", 5, 5],
        ["q", _w(9, 33, 0), GAPPY, 0.0,  0.40, "", 0, 5],    # one-sided
        ["q", _w(9, 34, 0), GAPPY, 0.31, 0.39, "", 5, 5],
    ]
    events.sort(key=lambda e: e[1])
    path = str(tmp_path / "session_2026-07-06.jsonl.gz")
    _write(path, events)
    return path, LIVE, GAPPY, DARK


class TestCoverage:
    def test_dark_symbol_detected(self, recording, tmp_path):
        path, live, gappy, dark = recording
        r = analyze(path, trades_dir=str(tmp_path))
        assert r["n_subscribed"] == 3
        assert r["n_delivering"] == 2
        assert r["dark_symbols"] == [dark]
        assert r["coverage"] == pytest.approx(2 / 3, abs=1e-4)
        assert r["coverage"] < COVERAGE_FAIL_BELOW    # would exit non-zero

    def test_quote_without_subscription_flagged(self, tmp_path):
        events = [
            ["s", _w(9, 30, 5), ["SPY260706C00604000"]],
            ["q", _w(9, 31, 0), "SPY260706C00604000", 0.48, 0.52, "", 1, 1],
            ["q", _w(9, 31, 5), "SPY260706C00999000", 0.10, 0.14, "", 1, 1],
        ]
        path = str(tmp_path / "session_2026-07-06.jsonl.gz")
        _write(path, events)
        r = analyze(path, trades_dir=str(tmp_path))
        assert r["unexpected_symbols"] == ["SPY260706C00999000"]


class TestQuality:
    def test_gap_and_spread_stats(self, recording, tmp_path):
        path, live, gappy, dark = recording
        r = analyze(path, trades_dir=str(tmp_path))
        assert r["gap_median_s"] == pytest.approx(2.0, abs=0.5)   # LIVE cadence
        assert r["gap_max_s"] >= 60.0                             # GAPPY's hole
        # LIVE spread = .04/.50 = 8%; GAPPY ≈ 28%; median lands on LIVE's 8%
        assert r["spread_median_pct"] == pytest.approx(8.0, abs=0.5)
        assert r["one_sided_pct"] > 0

    def test_quotes_per_sec(self, recording, tmp_path):
        path, *_ = recording
        r = analyze(path, trades_dir=str(tmp_path))
        assert r["n_quotes"] > 200
        assert r["quotes_per_sec"] > 0


class TestHeldSymbolGaps:
    def test_max_gap_during_hold_measured(self, recording, tmp_path):
        path, live, gappy, dark = recording
        # A trade held in GAPPY across its 60s quote hole (09:31 → 09:34)
        cols = ["date", "symbol", "entry_time", "exit_time", "realized_pnl",
                "entry_price", "exit_price", "qty", "reason", "side", "strike"]
        with open(tmp_path / "trades_2026-07-06.csv", "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=cols)
            w.writeheader()
            w.writerow({
                "date": "2026-07-06", "symbol": gappy,
                "entry_time": datetime.datetime(2026, 7, 6, 9, 31, tzinfo=ET).isoformat(),
                "exit_time":  datetime.datetime(2026, 7, 6, 9, 34, tzinfo=ET).isoformat(),
                "realized_pnl": "10", "entry_price": "0.35", "exit_price": "0.36",
                "qty": "1", "reason": "tp", "side": "put", "strike": "600",
            })
        r = analyze(path, trades_dir=str(tmp_path))
        assert len(r["held_symbol_gaps"]) == 1
        h = r["held_symbol_gaps"][0]
        assert h["symbol"] == gappy
        assert h["max_gap_s"] == pytest.approx(60.0, abs=1.0)
        # 60s > STALE_QUOTE_FLATTEN_SEC — live, this hold would have flattened
        assert h["max_gap_s"] > config.STALE_QUOTE_FLATTEN_SEC
