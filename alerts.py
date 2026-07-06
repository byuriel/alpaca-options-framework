"""
Critical-event alerting — the bot must never fail silently.

Every CRITICAL log line (staleness flatten, EXIT FAILED + gate lock, exit
desync, reconciliation mismatch, watchdog restart) is forwarded to the
operator's phone/inbox via a webhook and/or email. A machine that flattens a
position at 11:00 and says nothing until someone reads the terminal is not
an unattended system.

Design constraints, in order:
  1. NEVER block the event loop. Delivery runs on a daemon thread behind a
     queue; emit() is enqueue-only. Known hard-exit paths call flush() with
     a short timeout so the final alert escapes before os._exit().
  2. NEVER crash or recurse. Delivery errors are swallowed (logged at DEBUG
     — logging them higher could re-enter the handler). The handler is a
     no-op unless a channel is configured.
  3. NEVER spam. Per-message-key cooldown (default 5 min) and a daily cap:
     a CRITICAL that repeats every 5 seconds reaches the operator once,
     with a suppression count, not 400 times.

Channels (all stdlib):
  - Webhook: ALERT_WEBHOOK_URL — JSON POST {"text": ...} (Slack-compatible;
    for Discord append /slack to the webhook URL).
  - Email:   ALERT_EMAIL_TO + ALERT_SMTP_HOST [ALERT_SMTP_PORT=587,
    ALERT_SMTP_USER, ALERT_SMTP_PASS] — STARTTLS when credentials given.

Wiring: main() attaches AlertHandler to the root logger at startup (live
only — replay never attaches it), so every current and future
logger.critical() call is covered with zero call-site changes.
"""

import json
import logging
import queue
import smtplib
import socket
import threading
import time
import urllib.request
from email.message import EmailMessage

import config

logger = logging.getLogger(__name__)

COOLDOWN_SEC   = 300    # per message-key
DAILY_CAP      = 20     # absolute sends per day — a storm reaches you once
SEND_TIMEOUT   = 5.0    # per-channel network timeout (seconds)
_KEY_LEN       = 60     # message prefix used as the dedup key


class AlertSender:
    def __init__(self, webhook_url: str = "", email_to: str = "",
                 smtp_host: str = "", smtp_port: int = 587,
                 smtp_user: str = "", smtp_pass: str = ""):
        self.webhook_url = webhook_url
        self.email_to    = email_to
        self.smtp_host   = smtp_host
        self.smtp_port   = smtp_port
        self.smtp_user   = smtp_user
        self.smtp_pass   = smtp_pass

        self._q          = queue.Queue(maxsize=100)
        self._last_sent  = {}       # key -> monotonic time of last delivery
        self._suppressed = {}       # key -> count suppressed since last send
        self._sent_today = 0
        self._lock       = threading.Lock()
        self._thread     = None
        if self.enabled:
            self._thread = threading.Thread(
                target=self._sender_loop, daemon=True, name="alert-sender")
            self._thread.start()

    @property
    def enabled(self) -> bool:
        return bool(self.webhook_url or (self.email_to and self.smtp_host))

    # ── Producer side (event-loop safe: enqueue only) ─────────────────────────

    def alert(self, message: str):
        """Rate-limited, non-blocking. Safe to call from anywhere."""
        if not self.enabled:
            return
        key = message[:_KEY_LEN]
        now = time.monotonic()
        with self._lock:
            if self._sent_today >= DAILY_CAP:
                return
            last = self._last_sent.get(key, 0.0)
            if now - last < COOLDOWN_SEC:
                self._suppressed[key] = self._suppressed.get(key, 0) + 1
                return
            self._last_sent[key] = now
            self._sent_today += 1
            suppressed = self._suppressed.pop(key, 0)
        if suppressed:
            message = f"{message}\n(+{suppressed} similar suppressed in the last {COOLDOWN_SEC}s)"
        try:
            self._q.put_nowait(message)
        except queue.Full:
            pass   # cap already bounds this; never block

    def flush(self, timeout: float = 3.0):
        """Best-effort drain before a hard exit — the last alert is usually
        the one that matters most."""
        deadline = time.monotonic() + timeout
        while not self._q.empty() and time.monotonic() < deadline:
            time.sleep(0.05)

    # ── Delivery thread ───────────────────────────────────────────────────────

    def _sender_loop(self):
        while True:
            msg = self._q.get()
            body = (f"[{config.UNDERLYING} bot | "
                    f"{'PAPER' if config.PAPER else 'LIVE'} | "
                    f"{socket.gethostname()}]\n{msg}")
            if self.webhook_url:
                self._send_webhook(body)
            if self.email_to and self.smtp_host:
                self._send_email(body)

    def _send_webhook(self, body: str):
        try:
            req = urllib.request.Request(
                self.webhook_url,
                data=json.dumps({"text": body}).encode(),
                headers={"Content-Type": "application/json"},
            )
            urllib.request.urlopen(req, timeout=SEND_TIMEOUT).read()
        except Exception as e:
            logger.debug("Alert webhook failed: %s", e)

    def _send_email(self, body: str):
        try:
            em = EmailMessage()
            em["Subject"] = body.splitlines()[-1][:100]
            em["From"]    = self.smtp_user or f"bot@{socket.gethostname()}"
            em["To"]      = self.email_to
            em.set_content(body)
            with smtplib.SMTP(self.smtp_host, self.smtp_port,
                              timeout=SEND_TIMEOUT) as s:
                if self.smtp_user:
                    s.starttls()
                    s.login(self.smtp_user, self.smtp_pass)
                s.send_message(em)
        except Exception as e:
            logger.debug("Alert email failed: %s", e)


class AlertHandler(logging.Handler):
    """Forwards CRITICAL log records to the sender. Attached once by main()
    — every logger.critical() anywhere in the codebase becomes an alert."""

    def __init__(self, sender: AlertSender):
        super().__init__(level=logging.CRITICAL)
        self._sender = sender

    def emit(self, record: logging.LogRecord):
        try:
            self._sender.alert(f"{record.levelname} {record.name}: {record.getMessage()}")
        except Exception:
            pass   # an alert failure must never take down logging


# ── Module singleton (configured from env by main(); no-op otherwise) ─────────

_sender: AlertSender = AlertSender()   # disabled default (no channels)


def configure_from_env() -> bool:
    """Build the singleton from environment config. Returns True if any
    channel is active."""
    import os
    global _sender
    _sender = AlertSender(
        webhook_url=os.environ.get("ALERT_WEBHOOK_URL", "").strip(),
        email_to=os.environ.get("ALERT_EMAIL_TO", "").strip(),
        smtp_host=os.environ.get("ALERT_SMTP_HOST", "").strip(),
        smtp_port=int(os.environ.get("ALERT_SMTP_PORT", "587") or 587),
        smtp_user=os.environ.get("ALERT_SMTP_USER", "").strip(),
        smtp_pass=os.environ.get("ALERT_SMTP_PASS", "").strip(),
    )
    return _sender.enabled


def attach_to_root_logger() -> bool:
    """Install the CRITICAL forwarder. Idempotent."""
    root = logging.getLogger()
    if not any(isinstance(h, AlertHandler) for h in root.handlers):
        root.addHandler(AlertHandler(_sender))
    return _sender.enabled


def alert(message: str):
    _sender.alert(message)


def flush(timeout: float = 3.0):
    _sender.flush(timeout)
