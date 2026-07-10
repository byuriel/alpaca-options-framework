"""Daily results webhook: summary math, message formatting, webhook routing
and fallback, and the "0 trades still sends" no-silent-failure rule."""

import csv
import json
import os

import pytest

import config
import daily_summary as ds


def _write_trades(tmp_path, date_str, rows):
    os.makedirs(tmp_path, exist_ok=True)
    path = os.path.join(str(tmp_path), f"trades_{date_str}.csv")
    cols = ["date", "symbol", "side", "strike", "entry_price", "exit_price",
            "qty", "reason", "realized_pnl", "entry_time", "exit_time",
            "fees", "entry_order_id", "exit_order_id", "entry_bid",
            "entry_ask", "exit_bid", "exit_ask", "entry_slippage",
            "exit_slippage", "entry_spy", "mfe_pnl", "mae_pnl", "peak_mid"]
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in rows:
            row = {c: "" for c in cols}
            row.update(r)
            w.writerow(row)
    return path


def _row(pnl, fees="0.50", reason="target"):
    return {"realized_pnl": str(pnl), "fees": fees, "reason": reason}


class TestLoadTrades:
    def test_missing_file_returns_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "LOG_DIR", str(tmp_path))
        assert ds.load_trades("2026-07-09") == []

    def test_loads_existing_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "LOG_DIR", str(tmp_path))
        _write_trades(tmp_path, "2026-07-08", [_row(10.0), _row(-5.0)])
        rows = ds.load_trades("2026-07-08")
        assert len(rows) == 2


class TestSummarize:
    def test_zero_trades(self):
        s = ds.summarize([])
        assert s["trades"] == 0
        assert s["win_rate"] is None
        assert s["gross_pnl"] == 0.0 and s["net_pnl"] == 0.0

    def test_mixed_wins_losses_scratch(self):
        rows = [_row(25.0, reason="target"), _row(-10.0, reason="stop"),
                _row(0.0, reason="time_stop")]
        s = ds.summarize(rows)
        assert s["trades"] == 3
        assert s["wins"] == 1 and s["losses"] == 1 and s["scratches"] == 1
        assert s["win_rate"] == pytest.approx(1 / 3)
        assert s["gross_pnl"] == pytest.approx(15.0)
        assert s["fees"] == pytest.approx(1.5)
        assert s["net_pnl"] == pytest.approx(13.5)
        assert s["best"] == 25.0 and s["worst"] == -10.0
        assert s["reasons"] == {"target": 1, "stop": 1, "time_stop": 1}

    def test_missing_or_blank_numeric_fields_treated_as_zero(self):
        s = ds.summarize([{"realized_pnl": "", "fees": "", "reason": ""}])
        assert s["gross_pnl"] == 0.0
        assert s["reasons"] == {"?": 1}


class TestFormatMessage:
    def test_zero_trades_message_still_sent(self):
        s = ds.summarize([])
        msg = ds.format_message("2026-07-09", s)
        assert "2026-07-09" in msg
        assert "0 trades" in msg

    def test_normal_day_message_contains_key_figures(self):
        rows = [_row(25.0, reason="target"), _row(-10.0, reason="stop")]
        s = ds.summarize(rows)
        msg = ds.format_message("2026-07-08", s)
        assert "Trades: 2" in msg
        assert "W 1 / L 1" in msg
        assert "target=1" in msg and "stop=1" in msg


class TestWebhookRouting:
    def test_daily_summary_url_takes_priority(self, monkeypatch):
        monkeypatch.setenv("DAILY_SUMMARY_WEBHOOK_URL", "https://daily.example/hook")
        monkeypatch.setenv("ALERT_WEBHOOK_URL", "https://alert.example/hook")
        assert ds.webhook_url() == "https://daily.example/hook"

    def test_falls_back_to_alert_webhook(self, monkeypatch):
        monkeypatch.delenv("DAILY_SUMMARY_WEBHOOK_URL", raising=False)
        monkeypatch.setenv("ALERT_WEBHOOK_URL", "https://alert.example/hook")
        assert ds.webhook_url() == "https://alert.example/hook"

    def test_no_webhook_configured(self, monkeypatch):
        monkeypatch.delenv("DAILY_SUMMARY_WEBHOOK_URL", raising=False)
        monkeypatch.delenv("ALERT_WEBHOOK_URL", raising=False)
        assert ds.webhook_url() == ""


class TestSendWebhook:
    def test_posts_slack_compatible_json(self, monkeypatch):
        captured = {}

        class FakeResp:
            def read(self): return b"ok"
            def __enter__(self): return self
            def __exit__(self, *a): return False

        def fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            captured["body"] = json.loads(req.data.decode())
            captured["headers"] = dict(req.header_items())
            return FakeResp()

        monkeypatch.setattr(ds.urllib.request, "urlopen", fake_urlopen)
        ok = ds.send_webhook("https://hooks.example/x", "hello world")
        assert ok is True
        assert captured["url"] == "https://hooks.example/x"
        assert captured["body"] == {"text": "hello world"}

    def test_network_failure_returns_false_never_raises(self, monkeypatch):
        def boom(req, timeout=None):
            raise OSError("no route to host")
        monkeypatch.setattr(ds.urllib.request, "urlopen", boom)
        assert ds.send_webhook("https://hooks.example/x", "x") is False


class TestRun:
    def test_run_sends_and_returns_summary(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "LOG_DIR", str(tmp_path))
        monkeypatch.setenv("DAILY_SUMMARY_WEBHOOK_URL", "https://hooks.example/x")
        _write_trades(tmp_path, "2026-07-08", [_row(12.5)])
        sent = {}
        def _fake_send(url, text):
            sent["text"] = text
            return True
        monkeypatch.setattr(ds, "send_webhook", _fake_send)
        s = ds.run("2026-07-08")
        assert s["trades"] == 1
        assert s["webhook_sent"] is True
        assert "2026-07-08" in sent["text"]

    def test_run_without_webhook_still_returns_summary(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "LOG_DIR", str(tmp_path))
        monkeypatch.delenv("DAILY_SUMMARY_WEBHOOK_URL", raising=False)
        monkeypatch.delenv("ALERT_WEBHOOK_URL", raising=False)
        s = ds.run("2026-07-09")
        assert s["trades"] == 0
        assert s["webhook_sent"] is False

    def test_run_defaults_to_today_et(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "LOG_DIR", str(tmp_path))
        monkeypatch.delenv("DAILY_SUMMARY_WEBHOOK_URL", raising=False)
        monkeypatch.delenv("ALERT_WEBHOOK_URL", raising=False)
        s = ds.run()
        assert s["trades"] == 0   # no file for today in the temp dir
