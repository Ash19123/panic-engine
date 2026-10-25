"""Phone page for a real device: shared-state server, mobile page payload, and its JS logic (Node)."""
import json
import re
import shutil
import subprocess
import threading
import urllib.request
from pathlib import Path

import pytest

from database import repositories as repo
from simulation.experiments import run_scenario
from visualization import live_view as lv
from visualization import phone_server as ps


def _bomb_run(conn, sid):
    cfg = repo.load_scenario_config(conn, sid)
    net = repo.load_network(conn, cfg.network_id)
    nd = net.nodes[sorted(net.nodes)[35]]
    cfg.name = "Bomb drill phone"
    cfg.agent_count = 60
    cfg.params.hazards = [{"name": "Bomb A", "node": sorted(net.nodes)[35], "lat": nd.lat, "lon": nd.lon, "blast_radius_m": 150.0,
                           "alert_radius_m": 520.0, "strike_time_s": 420.0, "alert_time_s": 180.0}]
    nid = repo.save_scenario(conn, cfg, net)
    repo.ensure_population(conn, nid, net, cfg)
    return run_scenario(conn, nid, "CONGESTION_AWARE")


def _get(url, data=None):
    req = urllib.request.Request(url, data=json.dumps(data).encode() if data is not None else None,
                                 method="POST" if data is not None else "GET")
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read().decode("utf-8")


def test_phone_page_is_small_and_self_contained(seeded):
    conn, sid = seeded
    rid = _bomb_run(conn, sid)
    run = repo.get_run(conn, rid)
    cfg = repo.load_scenario_config(conn, run["scenario_id"])
    net = repo.load_network(conn, cfg.network_id)
    html = lv.build_phone_html(net, repo.load_replay_bundle(conn, rid), cfg.destinations, cfg.params.hazards, cfg.closures, rid)
    assert "deck.Deck" not in html and "requestAnimationFrame" in html          # no 3-D engine on the phone
    assert len(html) < 600_000
    data = json.loads(re.search(r"const D=(\{.*?\}),\$=id", html, re.S).group(1))
    assert data["run"] == rid and data["sync"] == "/state" and data["bombs"] and data["people"]
    assert all(len(p["lon"]) == len(data["times"]) for p in data["people"])


def test_phone_server_state_roundtrip_and_page(seeded, monkeypatch, tmp_path):
    conn, sid = seeded
    rid = _bomb_run(conn, sid)
    from config import settings
    db_file = conn.execute("PRAGMA database_list").fetchone()[2]            # the phone server must read THIS test database
    monkeypatch.setattr(settings, "DB_PATH", Path(db_file))
    monkeypatch.setattr(ps, "_SERVER", None)
    monkeypatch.setattr(ps, "_PORT", None)
    ps.reset_state()
    port = ps.ensure_started(18765)
    try:
        base = f"http://127.0.0.1:{port}"
        s0 = json.loads(_get(base + "/state"))
        assert s0["playing"] is False
        _get(base + "/state", {"owner": "web1", "run": rid, "t": 123.5, "playing": True, "speed": 60, "sel": 25,
                               "names": {"25": "Asha"}, "manual": {}})
        s1 = json.loads(_get(base + "/state"))
        assert s1["t"] == 123.5 and s1["owner"] == "web1" and s1["names"]["25"] == "Asha" and s1["age"] >= 0
        page = _get(f"{base}/phone?run={rid}")
        assert "EMERGENCY ALERT" in page and f'"run":{rid}' in page
    finally:
        ps._SERVER.shutdown()


NODE = shutil.which("node")


@pytest.mark.skipif(NODE is None, reason="Node.js not installed")
def test_phone_js_syntax_and_logic(seeded, tmp_path):
    conn, sid = seeded
    rid = _bomb_run(conn, sid)
    run = repo.get_run(conn, rid)
    cfg = repo.load_scenario_config(conn, run["scenario_id"])
    net = repo.load_network(conn, cfg.network_id)
    html = lv.build_phone_html(net, repo.load_replay_bundle(conn, rid), cfg.destinations, cfg.params.hazards, cfg.closures, rid)
    scripts = re.findall(r"<script>(.*?)</script>", html, re.S)
    main = tmp_path / "phone_main.js"
    main.write_text(scripts[-1], encoding="utf-8")
    out = subprocess.run([NODE, "--check", str(main)], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
