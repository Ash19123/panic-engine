"""Tiny LAN web server that lets a REAL phone open the evacuation phone view and stay in sync with the dashboard.

* ``GET  /phone?run=ID``  mobile page for one stored run (built once, cached)
* ``GET  /state``         shared playback state (clock, selected person, manual avatar)
* ``POST /state``         update it (the dashboard and every phone page push when the user acts, and poll otherwise)

The state is in memory only; the stored run itself stays in SQLite. The server listens on all interfaces
(``0.0.0.0``) so a phone on the same Wi-Fi can reach ``http://<this PC's LAN address>:<port>/phone``.
Nothing is served except the phone page and the state; the PC firewall may ask to allow Python the first time.
"""
from __future__ import annotations

import json
import logging
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, Optional
from urllib.parse import parse_qs, urlparse

from database import repositories as repo
from database.connection import connect

log = logging.getLogger(__name__)
DEFAULT_PORT = 8765

_LOCK = threading.Lock()
_STATE: Dict = {"run": None, "owner": None, "t": 0.0, "playing": False, "speed": 60, "sel": None, "names": {},
                "manual": {}, "recv": time.time()}
_PAGES: Dict[int, str] = {}
_SERVER: Optional[ThreadingHTTPServer] = None
_PORT: Optional[int] = None


def lan_ip() -> str:
    """Address of this PC on the local network (no packets are sent)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def phone_page(run_id: Optional[int]) -> str:
    from visualization import live_view as lv
    conn = connect()
    try:
        if run_id is None:
            runs = repo.list_runs(conn)
            if not runs:
                return "<h3>No run yet. Run a simulation on the dashboard first.</h3>"
            run_id = _STATE["run"] or runs[0]["run_id"]
        if run_id not in _PAGES:
            run = repo.get_run(conn, run_id)
            cfg = repo.load_scenario_config(conn, run["scenario_id"])
            net = repo.load_network(conn, cfg.network_id)
            bundle = repo.load_replay_bundle(conn, run_id)
            _PAGES[run_id] = lv.build_phone_html(net, bundle, cfg.destinations, cfg.params.hazards, cfg.closures, run_id)
        return _PAGES[run_id]
    finally:
        conn.close()


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):          # keep the console quiet
        pass

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Private-Network", "true")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Private-Network", "true")
        self.end_headers()

    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/state":
            with _LOCK:
                out = dict(_STATE)
                out["age"] = int((time.time() - out.pop("recv")) * 1000)
            self._send(200, json.dumps(out).encode(), "application/json")
        elif url.path in ("/", "/phone"):
            run = parse_qs(url.query).get("run", [None])[0]
            try:
                html = phone_page(int(run) if run else None)
                self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")
            except Exception as exc:      # report to the phone instead of dropping the connection
                log.exception("phone page failed")
                self._send(500, f"<h3>Could not build the phone page</h3><pre>{exc}</pre>".encode(), "text/html; charset=utf-8")
        else:
            self._send(404, b"not found", "text/plain")

    def do_POST(self):
        if urlparse(self.path).path != "/state":
            return self._send(404, b"not found", "text/plain")
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        except ValueError:
            return self._send(400, b"bad json", "text/plain")
        with _LOCK:
            if "run" in body and body["run"] != _STATE["run"]:
                _STATE["manual"] = {}
            for k in ("run", "owner", "t", "playing", "speed", "sel", "names", "manual"):
                if k in body:
                    _STATE[k] = body[k]
            _STATE["recv"] = time.time()
        self._send(200, b"{}", "application/json")


def ensure_started(port: Optional[int] = None) -> int:
    """Start the server once per process; returns the port actually used."""
    global _SERVER, _PORT
    if _SERVER is not None:
        return _PORT
    start = port or DEFAULT_PORT
    for candidate in range(start, start + 12):
        try:
            _SERVER = ThreadingHTTPServer(("0.0.0.0", candidate), _Handler)
            break
        except OSError:
            continue
    if _SERVER is None:
        raise OSError(f"no free port in {start}..{start + 11} for the phone server")
    _PORT = _SERVER.server_address[1]
    threading.Thread(target=_SERVER.serve_forever, name="panic-phone-server", daemon=True).start()
    log.info("Phone server listening on port %s", _PORT)
    return _PORT


def phone_url(port: int, run_id: Optional[int] = None) -> str:
    return f"http://{lan_ip()}:{port}/phone" + (f"?run={run_id}" if run_id else "")


def reset_state() -> None:
    with _LOCK:
        _STATE.update({"run": None, "owner": None, "t": 0.0, "playing": False, "speed": 60, "sel": None, "names": {},
                       "manual": {}, "recv": time.time()})
    _PAGES.clear()
