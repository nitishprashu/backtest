"""Local HTTP server (standard library only). Binds to 127.0.0.1; JSON API + static web UI."""
from __future__ import annotations

import json
import mimetypes
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from .serial import clean
from .service import ApiError, Service, _date

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
MAX_BODY = 8 * 1024 * 1024


class App:
    """Routes -> Service calls. Kept separate from the handler so it can be tested without sockets."""

    def __init__(self, service: Service):
        self.s = service
        r = self._routes = []   # (method, regex, fn)

        def route(method: str, pattern: str):
            def deco(fn):
                r.append((method, re.compile("^" + pattern + "$"), fn))
                return fn
            return deco

        @route("GET", r"/api/config")
        def _(q, body, **k): return self.s.config()

        @route("GET", r"/api/data")
        def _(q, body, **k):
            return self.s.data_status(_date(q["start"]), _date(q["end"]), int(q.get("step", 50)))

        @route("POST", r"/api/preview")
        def _(q, body, **k): return self.s.preview(body)

        @route("POST", r"/api/runs")
        def _(q, body, **k): return self.s.start_run(body)

        @route("POST", r"/api/replay")
        def _(q, body, **k): return self.s.start_replay(body)

        @route("GET", r"/api/runs")
        def _(q, body, **k): return {"runs": self.s.list_runs()}

        @route("GET", r"/api/runs/(?P<rid>[a-f0-9]+)")
        def _(q, body, rid, **k): return self.s.run_detail(rid)

        @route("PATCH", r"/api/runs/(?P<rid>[a-f0-9]+)")
        def _(q, body, rid, **k): return self.s.rename_run(rid, str(body.get("name", "")))

        @route("DELETE", r"/api/runs/(?P<rid>[a-f0-9]+)")
        def _(q, body, rid, **k): return self.s.delete_run(rid)

        @route("GET", r"/api/runs/(?P<rid>[a-f0-9]+)/trades")
        def _(q, body, rid, **k):
            return self.s.run_trades(rid, int(q.get("offset", 0)), int(q.get("limit", 100)), q.get("sort", "date"),
                                     q.get("order", "asc"), q.get("reason", ""), q.get("result", "all"),
                                     q.get("date"))

        @route("GET", r"/api/runs/(?P<rid>[a-f0-9]+)/explorer")
        def _(q, body, rid, **k): return self.s.explorer(rid, _date(q["date"]))

        @route("GET", r"/api/runs/(?P<rid>[a-f0-9]+)/nifty-trades")
        def _(q, body, rid, **k): return self.s.nifty_trade_chart(rid, _date(q["date"]))

        @route("GET", r"/api/runs/(?P<rid>[a-f0-9]+)/nifty-backtest-chart")
        def _(q, body, rid, **k): return self.s.nifty_backtest_chart(rid)

        @route("GET", r"/api/runs/(?P<rid>[a-f0-9]+)/chart-contracts")
        def _(q, body, rid, **k): return self.s.chart_contracts(rid, _date(q["date"]))

        @route("GET", r"/api/chart-contracts")
        def _(q, body, **k):
            return self.s.chart_contracts_global(q.get("year", ""), q.get("expiry", ""))

        @route("GET", r"/api/market-chart")
        def _(q, body, **k):
            return self.s.market_chart_history(
                q.get("instrument", "spot"), q.get("timeframe", "1m"),
                q.get("expiry", ""), q.get("strike", ""), q.get("option_type", ""),
                q.get("before", ""), q.get("limit", 100), q.get("run_id", ""))

        @route("GET", r"/api/runs/(?P<rid>[a-f0-9]+)/market-chart")
        def _(q, body, rid, **k):
            return self.s.market_chart(
                rid, _date(q["date"]), q.get("instrument", "spot"), q.get("expiry", ""),
                q.get("strike", ""), q.get("option_type", ""), q.get("timeframe", "1m"))

        @route("GET", r"/api/runs/(?P<rid>[a-f0-9]+)/report\.pdf")
        def _(q, body, rid, **k): return self.s.backtest_pdf(rid)

        @route("GET", r"/api/runs/(?P<rid>[a-f0-9]+)/trades\.csv")
        def _(q, body, rid, **k): return ("text/csv; charset=utf-8", self.s.trades_csv(rid), f"trades_{rid}.csv")

        @route("POST", r"/api/compare")
        def _(q, body, **k): return self.s.compare([str(x) for x in body.get("ids", [])])

        @route("POST", r"/api/optimize")
        def _(q, body, **k): return self.s.start_optimize(body)

        @route("POST", r"/api/repair")
        def _(q, body, **k): return self.s.start_repair(body)

        @route("GET", r"/api/jobs/(?P<jid>[a-f0-9]+)")
        def _(q, body, jid, **k):
            job = self.s.jobs.get(jid)
            if job is None:
                raise ApiError("job not found", 404)
            return clean(job.snapshot())

        @route("POST", r"/api/jobs/(?P<jid>[a-f0-9]+)/cancel")
        def _(q, body, jid, **k):
            job = self.s.jobs.get(jid)
            if job is None:
                raise ApiError("job not found", 404)
            job.cancel_requested = True
            return {"ok": True}

    def dispatch(self, method: str, path: str, query: Dict[str, str], body: Dict[str, Any]):
        for m, rx, fn in self._routes:
            mt = rx.match(path)
            if mt and m == method:
                return fn(query, body, **mt.groupdict())
        if any(rx.match(path) for _, rx, _ in self._routes):
            raise ApiError("method not allowed", 405)
        raise ApiError("not found", 404)


def make_handler(app: App, port_getter: Callable[[], int]):
    class Handler(BaseHTTPRequestHandler):
        server_version = "NiftyLab/5"
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):      # quiet; errors are returned to the client
            pass

        # -- helpers
        def _send(self, status: int, body: bytes, ctype: str, extra: Optional[Dict[str, str]] = None):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, obj: Any):
            self._send(status, json.dumps(clean(obj), separators=(",", ":")).encode("utf-8"),
                       "application/json; charset=utf-8")

        def _host_ok(self) -> bool:
            host = (self.headers.get("Host") or "").lower()
            p = port_getter()
            return host in (f"127.0.0.1:{p}", f"localhost:{p}")

        def _handle(self, method: str):
            u = urlparse(self.path)
            if not self._host_ok():
                return self._json(403, {"error": "forbidden host"})
            if u.path.startswith("/api/"):
                if method != "GET" and self.headers.get("X-Lab-Client") != "1":
                    return self._json(403, {"error": "missing client header"})
                body: Dict[str, Any] = {}
                try:
                    n = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    n = -1
                if n < 0:
                    self.close_connection = True
                    return self._json(400, {"error": "invalid Content-Length"})
                if n > MAX_BODY:
                    self.close_connection = True      # the unread body would corrupt the next keep-alive request
                    return self._json(413, {"error": "request too large"})
                if n:
                    try:
                        body = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
                    except ValueError:
                        return self._json(400, {"error": "invalid JSON"})
                q = {k: v[0] for k, v in parse_qs(u.query).items()}
                try:
                    out = app.dispatch(method, u.path, q, body)
                except ApiError as exc:
                    return self._json(exc.status, {"error": str(exc)})
                except KeyError as exc:
                    return self._json(400, {"error": f"missing parameter {exc}"})
                except (ValueError, TypeError) as exc:
                    return self._json(400, {"error": f"{type(exc).__name__}: {exc}"})
                except Exception as exc:   # unexpected: tell the UI what happened
                    return self._json(500, {"error": f"{type(exc).__name__}: {exc}"})
                if isinstance(out, tuple):                 # file download
                    ctype, data, fname = out
                    return self._send(200, data, ctype, {"Content-Disposition": f'attachment; filename="{fname}"'})
                return self._json(200, out)
            if method != "GET":
                return self._json(405, {"error": "method not allowed"})
            return self._static(u.path)

        def _static(self, path: str):
            rel = "index.html" if path in ("", "/") else path.lstrip("/")
            target = (WEB_DIR / rel).resolve()
            if WEB_DIR.resolve() not in target.parents and target != WEB_DIR.resolve():
                return self._json(403, {"error": "forbidden"})
            if not target.is_file():
                return self._json(404, {"error": "not found"})
            ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
            if target.suffix == ".js":
                ctype = "text/javascript; charset=utf-8"
            elif ctype.startswith("text/") or target.suffix in (".css", ".html", ".svg"):
                ctype = ctype.split(";")[0] + "; charset=utf-8"
            self._send(200, target.read_bytes(), ctype)

        def do_GET(self): self._handle("GET")
        def do_POST(self): self._handle("POST")
        def do_PATCH(self): self._handle("PATCH")
        def do_DELETE(self): self._handle("DELETE")

    return Handler


def create_server(service: Service, port: int = 8765, host: str = "127.0.0.1") -> ThreadingHTTPServer:
    app = App(service)
    holder: Dict[str, Any] = {}
    srv = ThreadingHTTPServer((host, port), make_handler(app, lambda: holder["srv"].server_address[1]))
    srv.daemon_threads = True
    holder["srv"] = srv
    return srv


def serve_in_thread(srv: ThreadingHTTPServer) -> threading.Thread:
    t = threading.Thread(target=srv.serve_forever, daemon=True, name="lab-http")
    t.start()
    return t
