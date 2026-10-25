"""Phone page for a real device: shared-state server, mobile page payload, and its JS logic (Node)."""
import json  # read and write JSON
import re  # find data and scripts in the page
import shutil  # find the Node program
import subprocess  # run Node
import threading  # (kept for tests that start servers)
import urllib.request  # call the phone server
from pathlib import Path  # file paths

import pytest  # the test framework

from database import repositories as repo  # save and load helpers
from simulation.experiments import run_scenario  # run and store a scenario
from visualization import live_view as lv  # builds the phone page
from visualization import phone_server as ps  # the phone server


def _bomb_run(conn, sid):  # create and run a bomb scenario
    cfg = repo.load_scenario_config(conn, sid)  # the demo scenario
    net = repo.load_network(conn, cfg.network_id)  # its network
    nd = net.nodes[sorted(net.nodes)[35]]  # a node near the middle
    cfg.name = "Bomb drill phone"  # rename it
    cfg.agent_count = 60  # 60 people
    cfg.params.hazards = [{"name": "Bomb A", "node": sorted(net.nodes)[35], "lat": nd.lat, "lon": nd.lon, "blast_radius_m": 150.0,  # one bomb (part 1)
                           "alert_radius_m": 520.0, "strike_time_s": 420.0, "alert_time_s": 180.0}]  # (part 2)
    nid = repo.save_scenario(conn, cfg, net)  # store it
    repo.ensure_population(conn, nid, net, cfg)  # generate the crowd
    return run_scenario(conn, nid, "CONGESTION_AWARE")  # run it and return the run id


def _get(url, data=None):  # call a URL (GET, or POST when data is given)
    req = urllib.request.Request(url, data=json.dumps(data).encode() if data is not None else None,  # JSON body for POST
                                 method="POST" if data is not None else "GET")  # the method
    with urllib.request.urlopen(req, timeout=20) as r:  # send it
        return r.read().decode("utf-8")  # the response text


def test_phone_page_is_small_and_self_contained(seeded):  # the mobile page is light
    conn, sid = seeded  # database with the demo scenario
    rid = _bomb_run(conn, sid)  # a bomb run
    run = repo.get_run(conn, rid)  # the run row
    cfg = repo.load_scenario_config(conn, run["scenario_id"])  # its scenario
    net = repo.load_network(conn, cfg.network_id)  # its network
    html = lv.build_phone_html(net, repo.load_replay_bundle(conn, rid), cfg.destinations, cfg.params.hazards, cfg.closures, rid)  # build the page
    assert "deck.Deck" not in html and "requestAnimationFrame" in html          # no 3-D engine on the phone
    assert len(html) < 600_000  # small enough for a phone
    data = json.loads(re.search(r"const D=(\{.*?\}),\$=id", html, re.S).group(1))  # the embedded data
    assert data["run"] == rid and data["sync"] == "/state" and data["bombs"] and data["people"]  # run id, sync address, bomb and people
    assert all(len(p["lon"]) == len(data["times"]) for p in data["people"])  # each person has a position for every frame


def test_phone_server_state_roundtrip_and_page(seeded, monkeypatch, tmp_path):  # the shared-state server
    conn, sid = seeded  # database with the demo scenario
    rid = _bomb_run(conn, sid)  # a bomb run
    from config import settings  # settings to patch
    db_file = conn.execute("PRAGMA database_list").fetchone()[2]            # the phone server must read THIS test database
    monkeypatch.setattr(settings, "DB_PATH", Path(db_file))  # point the server at it
    monkeypatch.setattr(ps, "_SERVER", None)  # forget any running server
    monkeypatch.setattr(ps, "_PORT", None)  # forget its port
    ps.reset_state()  # clear the shared state
    port = ps.ensure_started(18765)  # start the server
    try:  # always stop it
        base = f"http://127.0.0.1:{port}"  # its address
        s0 = json.loads(_get(base + "/state"))  # the initial state
        assert s0["playing"] is False  # not playing yet
        _get(base + "/state", {"owner": "web1", "run": rid, "t": 123.5, "playing": True, "speed": 60, "sel": 25,  # push a new state (part 1)
                               "names": {"25": "Asha"}, "manual": {}})  # (part 2)
        s1 = json.loads(_get(base + "/state"))  # read it back
        assert s1["t"] == 123.5 and s1["owner"] == "web1" and s1["names"]["25"] == "Asha" and s1["age"] >= 0  # the update is stored
        page = _get(f"{base}/phone?run={rid}")  # fetch the phone page
        assert "EMERGENCY ALERT" in page and f'"run":{rid}' in page  # it shows the alert screen and the run id
    finally:  # cleanup
        ps._SERVER.shutdown()  # stop the server


NODE = shutil.which("node")  # path to Node.js, or None


@pytest.mark.skipif(NODE is None, reason="Node.js not installed")
def test_phone_js_syntax_and_logic(seeded, tmp_path):  # the phone page's script must parse
    conn, sid = seeded  # database with the demo scenario
    rid = _bomb_run(conn, sid)  # a bomb run
    run = repo.get_run(conn, rid)  # the run row
    cfg = repo.load_scenario_config(conn, run["scenario_id"])  # its scenario
    net = repo.load_network(conn, cfg.network_id)  # its network
    html = lv.build_phone_html(net, repo.load_replay_bundle(conn, rid), cfg.destinations, cfg.params.hazards, cfg.closures, rid)  # build the page
    scripts = re.findall(r"<script>(.*?)</script>", html, re.S)  # all scripts in it
    main = tmp_path / "phone_main.js"  # temporary file
    main.write_text(scripts[-1], encoding="utf-8")  # save the last (main) script
    out = subprocess.run([NODE, "--check", str(main)], capture_output=True, text=True)  # ask Node to check the syntax
    assert out.returncode == 0, out.stderr  # no syntax errors
