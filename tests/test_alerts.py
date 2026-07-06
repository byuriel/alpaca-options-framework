"""Alerting — must fire on CRITICAL, never spam, never block, never crash."""

import logging
import time

import pytest

from alerts import AlertHandler, AlertSender, COOLDOWN_SEC, DAILY_CAP


class CapturingSender(AlertSender):
    """Sender with delivery replaced by capture — tests the rate-limit and
    handler logic without network. The delivery loop is neutralized so the
    queue holds exactly what would have been sent (including suppression
    suffixes), and we drain it synchronously."""
    def __init__(self):
        self.delivered = []
        super().__init__()          # enabled=True (property) starts the thread,
                                    # but _sender_loop below is a no-op

    @property
    def enabled(self):
        return True                 # force the producer path on

    def _sender_loop(self):
        return                      # no consumption — the test drains the queue

    def alert(self, message):
        super().alert(message)
        while not self._q.empty():
            self.delivered.append(self._q.get_nowait())


class TestRateLimiting:
    def test_first_alert_delivers(self):
        s = CapturingSender()
        s.alert("EXIT FAILED: something bad")
        assert len(s.delivered) == 1

    def test_repeat_within_cooldown_suppressed(self):
        s = CapturingSender()
        for _ in range(50):
            s.alert("EXIT FAILED: something bad")
        assert len(s.delivered) == 1     # one delivery, 49 suppressed

    def test_different_messages_both_deliver(self):
        s = CapturingSender()
        s.alert("EXIT FAILED: alpha")
        s.alert("STALENESS: bravo — completely different key")
        assert len(s.delivered) == 2

    def test_cooldown_expiry_redelivers_with_suppression_count(self):
        s = CapturingSender()
        s.alert("EXIT FAILED: x")
        s.alert("EXIT FAILED: x")            # suppressed
        s._last_sent = {k: v - (COOLDOWN_SEC + 1) for k, v in s._last_sent.items()}
        s.alert("EXIT FAILED: x")
        assert len(s.delivered) == 2
        assert "+1 similar suppressed" in s.delivered[1]

    def test_daily_cap(self):
        s = CapturingSender()
        for i in range(DAILY_CAP + 15):
            s.alert(f"unique message number {i} with distinct key material")
        assert len(s.delivered) == DAILY_CAP


class TestHandler:
    def test_critical_forwards_and_lower_levels_do_not(self):
        s = CapturingSender()
        h = AlertHandler(s)
        log = logging.getLogger("test-alerts")
        log.addHandler(h)
        try:
            log.warning("warning — should NOT alert")
            log.error("error — should NOT alert")
            log.critical("CRITICAL — should alert")
        finally:
            log.removeHandler(h)
        assert len(s.delivered) == 1
        assert "CRITICAL — should alert" in s.delivered[0]

    def test_handler_never_raises(self):
        class Exploding(AlertSender):
            @property
            def enabled(self):
                return True
            def alert(self, message):
                raise RuntimeError("boom")
        h = AlertHandler(Exploding())
        rec = logging.LogRecord("x", logging.CRITICAL, "f", 1, "msg", None, None)
        h.emit(rec)   # must not raise


class TestDisabledDefault:
    def test_no_channels_is_noop(self):
        s = AlertSender()             # nothing configured
        assert s.enabled is False
        s.alert("goes nowhere")       # must not raise, must not enqueue
        assert s._q.empty()

    def test_flush_on_disabled_returns_fast(self):
        s = AlertSender()
        t0 = time.monotonic()
        s.flush(3.0)
        assert time.monotonic() - t0 < 0.5


class TestWebhookDelivery:
    def test_payload_is_slack_compatible_json(self, monkeypatch):
        captured = {}

        def fake_urlopen(req, timeout=0):
            captured["url"]  = req.full_url
            captured["body"] = req.data.decode()
            captured["ct"]   = req.get_header("Content-type")
            class R:
                def read(self):
                    return b"ok"
            return R()

        import alerts as alerts_mod
        monkeypatch.setattr(alerts_mod.urllib.request, "urlopen", fake_urlopen)
        s = AlertSender(webhook_url="https://hooks.example.com/T/B/x")
        s._send_webhook("hello world")
        assert captured["url"].startswith("https://hooks.example.com")
        assert captured["ct"] == "application/json"
        import json
        assert json.loads(captured["body"])["text"] == "hello world"
