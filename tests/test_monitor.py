"""Web monitor — must be read-only by construction, never 500 on snapshot
errors, and never take the bot down over a busy port."""

import json
import urllib.error
import urllib.request

import pytest

from monitor import MonitorServer


def _get(port, path):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as r:
        return r.status, r.headers.get("Content-Type", ""), r.read()


@pytest.fixture
def server():
    started = []

    def make(snapshot_fn):
        s = MonitorServer(snapshot_fn, host="127.0.0.1", port=0)
        # port=0 normally means disabled; force ephemeral for tests
        s.port = 0

        # start() treats 0 as disabled — bind ephemeral by asking the OS
        class _S(MonitorServer):
            pass
        s2 = MonitorServer(snapshot_fn, host="127.0.0.1", port=_free_port())
        assert s2.start() is True
        started.append(s2)
        return s2

    yield make
    for s in started:
        s.stop()


def _free_port():
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class TestEndpoints:
    def test_state_returns_snapshot_json(self, server):
        s = server(lambda: {"spy": {"price": 601.25}, "paper": True})
        code, ctype, body = _get(s.port, "/api/state")
        assert code == 200 and "json" in ctype
        data = json.loads(body)
        assert data["spy"]["price"] == 601.25

    def test_index_serves_page(self, server):
        s = server(lambda: {})
        code, ctype, body = _get(s.port, "/")
        assert code == 200 and "html" in ctype
        text = body.decode()
        assert "0DTE Bot" in text
        assert "Read-only monitor" in text
        assert "cdn" not in text.lower()          # self-contained, no CDN

    def test_unknown_path_404(self, server):
        s = server(lambda: {})
        with pytest.raises(urllib.error.HTTPError) as e:
            _get(s.port, "/admin")
        assert e.value.code == 404

    def test_write_methods_rejected(self, server):
        # READ-ONLY BY CONSTRUCTION: no POST/PUT/DELETE handler exists
        s = server(lambda: {})
        req = urllib.request.Request(
            f"http://127.0.0.1:{s.port}/api/state", data=b"x=1", method="POST")
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(req, timeout=5)
        assert e.value.code in (405, 501)

    def test_snapshot_exception_returns_error_payload_not_500(self, server):
        def broken():
            raise RuntimeError("state torn mid-read")
        s = server(broken)
        code, _, body = _get(s.port, "/api/state")
        assert code == 200                          # the page keeps polling
        assert "snapshot failed" in json.loads(body)["error"]


class TestLifecycle:
    def test_port_zero_is_disabled(self):
        s = MonitorServer(lambda: {}, port=0)
        assert s.start() is False

    def test_busy_port_disables_instead_of_raising(self, server):
        s1 = server(lambda: {})
        s2 = MonitorServer(lambda: {}, host="127.0.0.1", port=s1.port)
        assert s2.start() is False                  # logs a warning, bot lives

    def test_stop_releases_port(self):
        port = _free_port()
        s = MonitorServer(lambda: {}, host="127.0.0.1", port=port)
        assert s.start() is True
        s.stop()
        s2 = MonitorServer(lambda: {}, host="127.0.0.1", port=port)
        assert s2.start() is True
        s2.stop()
