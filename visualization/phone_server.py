"""Tiny LAN web server that lets a REAL phone open the evacuation phone view and stay in sync with the dashboard.

* ``GET  /phone?run=ID``  mobile page for one stored run (built once, cached)
* ``GET  /state``         shared playback state (clock, selected person, manual avatar)
* ``POST /state``         update it (the dashboard and every phone page push when the user acts, and poll otherwise)

The state is in memory only; the stored run itself stays in SQLite. The server listens on all interfaces
(``0.0.0.0``) so a phone on the same Wi-Fi can reach ``http://<this PC's LAN address>:<port>/phone``.
Nothing is served except the phone page and the state; the PC firewall may ask to allow Python the first time.
"""
from __future__ import annotations  # modern type hints on older Python

import json  # state as JSON
import logging  # log problems
import socket  # find this PC's address
import threading  # lock and background server thread
import time  # timestamps
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer  # standard-library web server
from typing import Dict, Optional  # type hints
from urllib.parse import parse_qs, urlparse  # read the URL and its query string

from database import repositories as repo  # load runs and networks
from database.connection import connect  # open the database

log = logging.getLogger(__name__)  # logger for this module
DEFAULT_PORT = 8765  # the port tried first

_LOCK = threading.Lock()  # protects the shared state from simultaneous access
_STATE: Dict = {"run": None, "owner": None, "t": 0.0, "playing": False, "speed": 60, "sel": None, "names": {},  # shared playback state (part 1)
                "manual": {}, "recv": time.time()}  # (part 2): manual avatars and when it was last updated
_PAGES: Dict[int, str] = {}  # built phone pages by run id
_SERVER: Optional[ThreadingHTTPServer] = None  # the running server, if any
_PORT: Optional[int] = None  # the port it uses


def lan_ip() -> str:  # this PC's address on the local network
    """Address of this PC on the local network (no packets are sent)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)  # a UDP socket
    try:  # connecting may fail without a network
        s.connect(("10.255.255.255", 1))  # choose the outgoing interface; nothing is sent for UDP
        return s.getsockname()[0]  # the address of that interface
    except OSError:  # no network
        return "127.0.0.1"  # this machine only
    finally:  # always
        s.close()  # close the socket


def phone_page(run_id: Optional[int]) -> str:  # build (or fetch) the phone page for a run
    from visualization import live_view as lv  # imported here to avoid a circular import
    conn = connect()  # open the database
    try:  # always close it
        if run_id is None:  # no run asked for
            runs = repo.list_runs(conn)  # all completed runs
            if not runs:  # none
                return "<h3>No run yet. Run a simulation on the dashboard first.</h3>"  # tell the user
            run_id = _STATE["run"] or runs[0]["run_id"]  # the dashboard's current run, or the newest
        if run_id not in _PAGES:  # not built yet
            run = repo.get_run(conn, run_id)  # the run row
            cfg = repo.load_scenario_config(conn, run["scenario_id"])  # its scenario
            net = repo.load_network(conn, cfg.network_id)  # its road network
            bundle = repo.load_replay_bundle(conn, run_id)  # its stored frames
            _PAGES[run_id] = lv.build_phone_html(net, bundle, cfg.destinations, cfg.params.hazards, cfg.closures, run_id)  # build and cache the page
        return _PAGES[run_id]  # the page
    finally:  # always
        conn.close()  # close the database


class _Handler(BaseHTTPRequestHandler):  # answers web requests
    def log_message(self, *args):          # keep the console quiet
        pass  # print nothing

    def _send(self, code: int, body: bytes, ctype: str) -> None:  # send a response
        self.send_response(code)  # status code
        self.send_header("Content-Type", ctype)  # kind of content
        self.send_header("Content-Length", str(len(body)))  # size
        self.send_header("Access-Control-Allow-Origin", "*")  # allow any page to call us (the dashboard runs on another port)
        self.send_header("Access-Control-Allow-Private-Network", "true")  # allow local-network access from secure pages
        self.send_header("Cache-Control", "no-store")  # never cache
        self.end_headers()  # finish the headers
        self.wfile.write(body)  # send the body

    def do_OPTIONS(self):  # browser pre-flight check
        self.send_response(204)  # no content
        self.send_header("Access-Control-Allow-Origin", "*")  # allowed origins
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")  # allowed methods
        self.send_header("Access-Control-Allow-Headers", "Content-Type")  # allowed headers
        self.send_header("Access-Control-Allow-Private-Network", "true")  # allow local-network access
        self.end_headers()  # finish the headers

    def do_GET(self):  # handle GET requests
        url = urlparse(self.path)  # split the URL
        if url.path == "/state":  # shared state requested
            with _LOCK:  # one at a time
                out = dict(_STATE)  # copy it
                out["age"] = int((time.time() - out.pop("recv")) * 1000)  # replace the timestamp by its age in milliseconds
            self._send(200, json.dumps(out).encode(), "application/json")  # reply with JSON
        elif url.path in ("/", "/phone"):  # the phone page requested
            run = parse_qs(url.query).get("run", [None])[0]  # optional run id in the query
            try:  # building can fail
                html = phone_page(int(run) if run else None)  # build or fetch the page
                self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")  # reply with the page
            except Exception as exc:      # report to the phone instead of dropping the connection
                log.exception("phone page failed")  # log the traceback
                self._send(500, f"<h3>Could not build the phone page</h3><pre>{exc}</pre>".encode(), "text/html; charset=utf-8")  # show the error on the phone
        else:  # anything else
            self._send(404, b"not found", "text/plain")  # not found

    def do_POST(self):  # handle POST requests
        if urlparse(self.path).path != "/state":  # only /state accepts updates
            return self._send(404, b"not found", "text/plain")  # not found
        try:  # the body may be invalid JSON
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")  # read and parse it
        except ValueError:  # not valid JSON
            return self._send(400, b"bad json", "text/plain")  # bad request
        with _LOCK:  # one at a time
            if "run" in body and body["run"] != _STATE["run"]:  # a different run was selected
                _STATE["manual"] = {}  # drop manual avatars of the old run
            for k in ("run", "owner", "t", "playing", "speed", "sel", "names", "manual"):  # the keys we accept
                if k in body:  # present in the update
                    _STATE[k] = body[k]  # store it
            _STATE["recv"] = time.time()  # remember when it was updated
        self._send(200, b"{}", "application/json")  # acknowledge


def ensure_started(port: Optional[int] = None) -> int:  # start the server once
    """Start the server once per process; returns the port actually used."""
    global _SERVER, _PORT  # module-level variables are changed
    if _SERVER is not None:  # already running
        return _PORT  # nothing to do
    start = port or DEFAULT_PORT  # first port to try
    for candidate in range(start, start + 12):  # try 12 ports
        try:  # the port may be busy
            _SERVER = ThreadingHTTPServer(("0.0.0.0", candidate), _Handler)  # listen on all interfaces
            break  # success
        except OSError:  # port in use
            continue  # try the next one
    if _SERVER is None:  # none was free
        raise OSError(f"no free port in {start}..{start + 11} for the phone server")  # give up
    _PORT = _SERVER.server_address[1]  # the port actually used
    threading.Thread(target=_SERVER.serve_forever, name="panic-phone-server", daemon=True).start()  # serve in the background
    log.info("Phone server listening on port %s", _PORT)  # log it
    return _PORT  # the port


def phone_url(port: int, run_id: Optional[int] = None) -> str:  # the address to open on a phone
    return f"http://{lan_ip()}:{port}/phone" + (f"?run={run_id}" if run_id else "")  # this PC's address, the port, the path and the run


def reset_state() -> None:  # clear the shared state (used by tests)
    with _LOCK:  # one at a time
        _STATE.update({"run": None, "owner": None, "t": 0.0, "playing": False, "speed": 60, "sel": None, "names": {},  # reset the values (part 1)
                       "manual": {}, "recv": time.time()})  # (part 2)
    _PAGES.clear()  # forget built pages
