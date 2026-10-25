"""The live page's routing / manual-walk logic runs in Node against a payload built from a real stored run."""
import json  # read and write JSON
import shutil  # find the Node program
import subprocess  # run Node
from pathlib import Path  # file paths

import pytest  # the test framework

from database import repositories as repo  # save and load helpers
from simulation.experiments import run_scenario  # run and store a scenario
from visualization import live_view as lv  # builds the live page data
from visualization import scene3d as sc  # building decoration helpers

NODE = shutil.which("node")  # path to Node.js, or None if it is not installed
pytestmark = pytest.mark.skipif(NODE is None, reason="Node.js not installed (optional JS unit test)")  # skip every test here without Node


def test_live_core_in_node(seeded, tmp_path):  # run the browser logic in Node against real data
    conn, sid = seeded  # database with the demo scenario
    cfg = repo.load_scenario_config(conn, sid)  # the scenario
    net = repo.load_network(conn, cfg.network_id)  # its network
    nd = net.nodes[sorted(net.nodes)[35]]  # a node near the middle
    cfg.name = "Bomb drill js"  # rename it
    cfg.agent_count = 60  # 60 people
    cfg.params.hazards = [{"name": "Bomb A", "node": sorted(net.nodes)[35], "lat": nd.lat, "lon": nd.lon, "blast_radius_m": 150.0,  # one bomb (part 1)
                           "alert_radius_m": 520.0, "strike_time_s": 420.0, "alert_time_s": 180.0}]  # (part 2)
    new_id = repo.save_scenario(conn, cfg, net)  # store it
    repo.ensure_population(conn, new_id, net, cfg)  # generate the crowd
    rid = run_scenario(conn, new_id, "CONGESTION_AWARE")  # run it
    cfg2 = repo.load_scenario_config(conn, new_id)  # reload the scenario
    payload = lv.build_live_html(net, repo.load_replay_bundle(conn, rid), cfg2.destinations, cfg2.origins, [], cfg2.closures,  # build only the data (part 1)
                                 [], hazards=cfg2.params.hazards, payload_only=True)  # (part 2)
    f = tmp_path / "payload.json"  # temporary file
    f.write_text(json.dumps(payload), encoding="utf-8")  # save the data for Node
    script = Path(__file__).with_name("live_core_test.js")  # the Node test script
    out = subprocess.run([NODE, str(script), str(f)], capture_output=True, text=True, timeout=120)  # run it
    assert out.returncode == 0, out.stderr  # it must succeed
    r = json.loads(out.stdout.strip().splitlines()[-1])  # its last output line is JSON results
    assert r["unrouted"] == 0 and r["routed"] == r["checked"]          # every sampled node finds a safe stop
    assert r["maxErr"] < 1e-6                                          # same distances as an independent Dijkstra
    assert r["autoStatus"] == "safe" and r["trail"] > 1                # auto-guide walks the avatar to a safe stop
    assert r["keyMoved"] == 1                                          # steering by direction moves along edges
    assert r["blastStatus"] == "caught"                                # standing in the blast area at strike
    assert r["closedBefore"] == 0 and r["closedAfterStrike"] > 0       # blast closes roads only after the strike
    assert r["compass"] == ["north", "east", "south-west", "north-west"]   # directions use plain compass words
    assert r["steps"] >= 1 and r["stepTotal"] > 0  # step-by-step directions exist


def test_dashboard_template_js_syntax(tmp_path):  # the dashboard page's script must parse
    t = (Path(lv.__file__).with_name("live_template.html")).read_text(encoding="utf-8")  # the template text
    scripts = t.split("<script>")  # split at script tags
    main = scripts[-1].split("</script>")[0].replace("__DATA__", "{}").replace("__HEIGHT__", "760")  # the last script with placeholders filled
    f = tmp_path / "dash.js"  # temporary file
    f.write_text(main, encoding="utf-8")  # save it
    out = subprocess.run([NODE, "--check", str(f)], capture_output=True, text=True)  # ask Node to check the syntax
    assert out.returncode == 0, out.stderr  # no syntax errors
