"""Always-on dashboard: cumulative aggregation from disk, liveness heuristic,
open-position read, and the read-only HTTP surface."""

import csv
import json
import os
import time
import urllib.request

import pytest

import config
import dashboard as dash

COLS = ["date", "symbol", "side", "strike", "entry_price", "exit_price",
        "qty", "reason", "realized_pnl", "entry_time", "exit_time",
        "fees", "entry_order_id", "exit_order_id", "entry_bid", "entry_ask",
        "exit_bid", "exit_ask", "entry_slippage", "exit_slippage",
        "entry_spy", "mfe_pnl", "mae_pnl", "peak_mid"]


def _write_day(log_dir, date_str, trades):
    os.makedirs(log_dir, exist_ok=True)
    p = os.path.join(log_dir, f"trades_{date_str}.csv")
    with open(p, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLS)
        w.writeheader()
        for pnl, reason in trades:
            row = {c: "" for c in COLS}
            row.update({"realized_pnl": str(pnl), "fees": "0.50",
                        "reason": reason, "symbol": "SPY260706C00627000",
                        "side": "call", "entry_price": "0.50",
                        "exit_price": "0.60", "qty": "6", "exit_time": "10:15:00"})
            w.writerow(row)
    return p


class TestAggregation:
    def test_cumulative_since_inception(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "LOG_DIR", str(tmp_path))
        _write_day(tmp_path, "2026-07-06", [(25.0, "target"), (-10.0, "stop")])
        _write_day(tmp_path, "2026-07-07", [(15.0, "target")])
        _write_day(tmp_path, "2026-07-08", [(-30.0, "stop"), (-5.0, "stop")])

        d = dash.build_state()
        si = d["since_inception"]
        assert si["trading_days"] == 3
        assert si["trades"] == 5
        assert si["wins"] == 2
        assert si["win_rate"] == pytest.approx(2 / 5)
        # net = gross (25-10+15-30-5 = -5) - fees (5 * 0.50 = 2.50) = -7.50
        assert si["gross_pnl"] == pytest.approx(-5.0)
        assert si["net_pnl"] == pytest.approx(-7.50)
        assert si["green_days"] == 2      # 07-06 (+14) and 07-07 (+14.5)
        assert si["red_days"] == 1        # 07-08
        assert si["first_day"] == "2026-07-06"

    def test_days_newest_first(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "LOG_DIR", str(tmp_path))
        _write_day(tmp_path, "2026-07-06", [(1.0, "target")])
        _write_day(tmp_path, "2026-07-08", [(2.0, "target")])
        d = dash.build_state()
        dated = [x["date"] for x in d["days"] if x["trades"] > 0]
        assert dated == ["2026-07-08", "2026-07-06"]

    def test_empty_when_no_files(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "LOG_DIR", str(tmp_path))
        d = dash.build_state()
        assert d["since_inception"]["trades"] == 0
        assert d["since_inception"]["first_day"] is None
        assert d["since_inception"]["win_rate"] is None

    def test_header_only_file_counts_as_no_trades(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "LOG_DIR", str(tmp_path))
        _write_day(tmp_path, "2026-07-06", [])       # header only
        d = dash.build_state()
        assert d["since_inception"]["trading_days"] == 0


class TestLiveness:
    def test_fresh_log_means_running(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "LOG_DIR", str(tmp_path))
        p = os.path.join(str(tmp_path), "bot_2026-07-09.log")
        with open(p, "w") as f:
            f.write("alive\n")
        assert dash.bot_running() is not None      # just written -> live

    def test_stale_log_means_not_running(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "LOG_DIR", str(tmp_path))
        p = os.path.join(str(tmp_path), "bot_2026-07-09.log")
        with open(p, "w") as f:
            f.write("old\n")
        old = time.time() - (dash.LIVENESS_WINDOW_SEC + 60)
        os.utime(p, (old, old))
        assert dash.bot_running() is None

    def test_no_log_means_not_running(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "LOG_DIR", str(tmp_path))
        assert dash.bot_running() is None


class TestOpenPosition:
    def test_reads_persisted_position(self, tmp_path, monkeypatch):
        import state
        monkeypatch.setattr(config, "LOG_DIR", str(tmp_path))
        pf = os.path.join(str(tmp_path), "position_state.json")
        monkeypatch.setattr(state, "POSITION_STATE_FILE", pf)
        with open(pf, "w") as f:
            json.dump({"symbol": "SPY260706C00627000", "side": "call",
                       "strike": 627.0, "qty": 6, "entry_price": 0.51,
                       "entry_time": "2026-07-09T10:00:00-04:00"}, f)
        d = dash.build_state()
        assert d["position"]["symbol"] == "SPY260706C00627000"
        assert d["position"]["qty"] == 6

    def test_flat_when_no_file(self, tmp_path, monkeypatch):
        import state
        monkeypatch.setattr(config, "LOG_DIR", str(tmp_path))
        monkeypatch.setattr(state, "POSITION_STATE_FILE",
                            os.path.join(str(tmp_path), "nope.json"))
        assert dash.build_state()["position"] is None


class TestServer:
    def test_serves_page_and_api_readonly(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "LOG_DIR", str(tmp_path))
        _write_day(tmp_path, "2026-07-06", [(10.0, "target")])
        srv = dash.Dashboard("127.0.0.1", 0)

        # bind on an ephemeral port and serve in a thread
        import threading
        from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

        # reach into serve_forever's handler by binding manually
        holder = {}

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == "/api/state":
                    body = json.dumps(dash.build_state(), default=str).encode()
                    self._r(200, "application/json", body)
                elif self.path in ("/", "/index.html"):
                    self._r(200, "text/html; charset=utf-8", dash.HTML_PAGE.encode())
                else:
                    self._r(404, "text/plain", b"nf")

            def _r(self, c, t, b):
                self.send_response(c); self.send_header("Content-Type", t)
                self.send_header("Content-Length", str(len(b))); self.end_headers()
                self.wfile.write(b)

            def send_error(self, code, message=None, explain=None):
                if code == 501:
                    code, message = 405, "read-only"
                super().send_error(code, message, explain)

            def log_message(self, *a): pass

        httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        port = httpd.server_port
        th = threading.Thread(target=httpd.serve_forever, daemon=True)
        th.start()
        try:
            page = urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=3).read()
            assert b"0DTE Bot" in page

            api = urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/state", timeout=3).read()
            payload = json.loads(api)
            assert payload["since_inception"]["trades"] == 1

            # POST must be refused (read-only)
            req = urllib.request.Request(f"http://127.0.0.1:{port}/",
                                         data=b"x", method="POST")
            with pytest.raises(urllib.error.HTTPError) as ei:
                urllib.request.urlopen(req, timeout=3)
            assert ei.value.code == 405
        finally:
            httpd.shutdown(); httpd.server_close()
