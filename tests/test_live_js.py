"""The live page's routing / manual-walk logic runs in Node against a payload built from a real stored run."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from database import repositories as repo
from simulation.experiments import run_scenario
from visualization import live_view as lv
from visualization import scene3d as sc

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="Node.js not installed (optional JS unit test)")


def test_live_core_in_node(seeded, tmp_path):
    conn, sid = seeded
    cfg = repo.load_scenario_config(conn, sid)
    net = repo.load_network(conn, cfg.network_id)
    nd = net.nodes[sorted(net.nodes)[35]]
    cfg.name = "Bomb drill js"
    cfg.agent_count = 60
    cfg.params.hazards = [{"name": "Bomb A", "node": sorted(net.nodes)[35], "lat": nd.lat, "lon": nd.lon, "blast_radius_m": 150.0,
                           "alert_radius_m": 520.0, "strike_time_s": 420.0, "alert_time_s": 180.0}]
    new_id = repo.save_scenario(conn, cfg, net)
    repo.ensure_population(conn, new_id, net, cfg)
    rid = run_scenario(conn, new_id, "CONGESTION_AWARE")
    cfg2 = repo.load_scenario_config(conn, new_id)
    payload = lv.build_live_html(net, repo.load_replay_bundle(conn, rid), cfg2.destinations, cfg2.origins, [], cfg2.closures,
                                 [], hazards=cfg2.params.hazards, payload_only=True)
    f = tmp_path / "payload.json"
    f.write_text(json.dumps(payload), encoding="utf-8")
    script = Path(__file__).with_name("live_core_test.js")
    out = subprocess.run([NODE, str(script), str(f)], capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    r = json.loads(out.stdout.strip().splitlines()[-1])
    assert r["unrouted"] == 0 and r["routed"] == r["checked"]          # every sampled node finds a safe stop
    assert r["maxErr"] < 1e-6                                          # same distances as an independent Dijkstra
    assert r["autoStatus"] == "safe" and r["trail"] > 1                # auto-guide walks the avatar to a safe stop
    assert r["keyMoved"] == 1                                          # steering by direction moves along edges
    assert r["blastStatus"] == "caught"                                # standing in the blast area at strike
    assert r["closedBefore"] == 0 and r["closedAfterStrike"] > 0       # blast closes roads only after the strike
    assert r["compass"] == ["north", "east", "south-west", "north-west"]   # directions use plain compass words
    assert r["steps"] >= 1 and r["stepTotal"] > 0


def test_dashboard_template_js_syntax(tmp_path):
    t = (Path(lv.__file__).with_name("live_template.html")).read_text(encoding="utf-8")
    scripts = t.split("<script>")
    main = scripts[-1].split("</script>")[0].replace("__DATA__", "{}").replace("__HEIGHT__", "760")
    f = tmp_path / "dash.js"
    f.write_text(main, encoding="utf-8")
    out = subprocess.run([NODE, "--check", str(f)], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
