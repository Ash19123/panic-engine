"""The live (client-side) view is built from stored snapshots only; check its payload and its self-containment."""
import json
import re

from database import repositories as repo
from simulation.experiments import run_scenario
from visualization import live_view as lv
from visualization import scene3d as sc


def test_live_html_is_self_contained_and_matches_stored_frames(seeded):
    conn, sid = seeded
    rid = run_scenario(conn, sid, "CONGESTION_AWARE")
    run = repo.get_run(conn, rid)
    cfg = repo.load_scenario_config(conn, run["scenario_id"])
    net = repo.load_network(conn, cfg.network_id)
    bundle = repo.load_replay_bundle(conn, rid)
    html = lv.build_live_html(net, bundle, cfg.destinations, cfg.origins, [], cfg.closures,
                              sc.synthetic_buildings(net))
    assert "requestAnimationFrame" in html and "deck.Deck" in html          # animates in the browser
    data = json.loads(re.search(r"const D=(\{.*?\});\nconst \$=", html, re.S).group(1))
    assert data["people"] and data["graph"]["lon"] and data["safe"] and data["bombs"] == []
    assert data["times"] == bundle["times"] and data["n"] == len(bundle["agent_ids"]) == cfg.agent_count
    assert len(data["states"]) == len(data["lat"]) == len(data["lon"]) == len(data["times"])
    assert data["people"] and data["graph"]["lon"] and data["safe"] and data["bombs"] == []
    assert data["synthetic"] is True                                          # synthetic net => no basemap offered
    assert data["total"] == cfg.agent_count and sum(data["stats"][-1]) == cfg.agent_count
    assert "cartocdn" not in lv.TEMPLATE.read_text(encoding="utf-8") and "tile.openstreetmap.org" in html   # Carto tiles need an API key now


def test_replay_bundle_counts_match_sql(seeded):
    conn, sid = seeded
    rid = run_scenario(conn, sid, "SHORTEST_PATH")
    b = repo.load_replay_bundle(conn, rid)
    last = b["stats"][-1]
    run = repo.get_run(conn, rid)
    assert last[3] == run["evacuated_agents"] and last[4] == run["failed_agents"]


def test_view_frames_the_points():
    from geo.synthetic_network import build_synthetic_network
    net = build_synthetic_network()
    v = lv.view_for(net, [(net.center[0], net.center[1])])
    assert 11 <= v["zoom"] <= 17.5 and abs(v["lat"] - net.center[0]) < 1e-9
