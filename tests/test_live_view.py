"""The live (client-side) view is built from stored snapshots only; check its payload and its self-containment."""
import json  # parse the embedded data
import re  # find the data in the page

from database import repositories as repo  # save and load helpers
from simulation.experiments import run_scenario  # run and store a scenario
from visualization import live_view as lv  # builds the live page
from visualization import scene3d as sc  # building decoration helpers


def test_live_html_is_self_contained_and_matches_stored_frames(seeded):  # the page carries everything it needs
    conn, sid = seeded  # database with the demo scenario
    rid = run_scenario(conn, sid, "CONGESTION_AWARE")  # run it
    run = repo.get_run(conn, rid)  # the run row
    cfg = repo.load_scenario_config(conn, run["scenario_id"])  # its scenario
    net = repo.load_network(conn, cfg.network_id)  # its network
    bundle = repo.load_replay_bundle(conn, rid)  # its stored frames
    html = lv.build_live_html(net, bundle, cfg.destinations, cfg.origins, [], cfg.closures,  # build the page (part 1)
                              sc.synthetic_buildings(net))  # with decorative buildings (part 2)
    assert "requestAnimationFrame" in html and "deck.Deck" in html          # animates in the browser
    data = json.loads(re.search(r"const D=(\{.*?\});\nconst \$=", html, re.S).group(1))  # the embedded data
    assert data["people"] and data["graph"]["lon"] and data["safe"] and data["bombs"] == []  # people, graph and safe stops are present; no bomb
    assert data["times"] == bundle["times"] and data["n"] == len(bundle["agent_ids"]) == cfg.agent_count  # same frames and agents as stored
    assert len(data["states"]) == len(data["lat"]) == len(data["lon"]) == len(data["times"])  # one entry per frame everywhere
    assert data["people"] and data["graph"]["lon"] and data["safe"] and data["bombs"] == []  # repeated sanity check
    assert data["synthetic"] is True                                          # synthetic net => no basemap offered
    assert data["total"] == cfg.agent_count and sum(data["stats"][-1]) == cfg.agent_count  # every agent is counted in the last frame
    assert "cartocdn" not in lv.TEMPLATE.read_text(encoding="utf-8") and "tile.openstreetmap.org" in html   # Carto tiles need an API key now


def test_replay_bundle_counts_match_sql(seeded):  # replay numbers equal SQL numbers
    conn, sid = seeded  # database with the demo scenario
    rid = run_scenario(conn, sid, "SHORTEST_PATH")  # run it
    b = repo.load_replay_bundle(conn, rid)  # its stored frames
    last = b["stats"][-1]  # counts in the final frame
    run = repo.get_run(conn, rid)  # the run row
    assert last[3] == run["evacuated_agents"] and last[4] == run["failed_agents"]  # evacuated and failed counts agree


def test_view_frames_the_points():  # the camera
    from geo.synthetic_network import build_synthetic_network  # the fake grid
    net = build_synthetic_network()  # build it
    v = lv.view_for(net, [(net.center[0], net.center[1])])  # frame its centre
    assert 11 <= v["zoom"] <= 17.5 and abs(v["lat"] - net.center[0]) < 1e-9  # sensible zoom and centred
