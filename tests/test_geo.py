"""Geographic helpers, synthetic fallback, OSM graph conversion (offline: uses a hand-made graph) and playback."""
import networkx as nx
import pytest

from geo import geographic_utils as geo
from geo import network_cache as cache
from geo.network_loader import graph_to_network, osm_capacity
from geo.synthetic_network import build_synthetic_network
from visualization import playback as pb


def test_synthetic_network_is_deterministic_labelled_and_connected():
    a, b = build_synthetic_network(), build_synthetic_network()
    assert a.fingerprint() == b.fingerprint() and a.network_id == b.network_id
    assert a.is_synthetic and "NOT real" in a.name
    assert a.strongly_connected_fraction() == 1.0
    assert all(e.capacity >= 2 and e.length_m > 0 for e in a.edges.values())


def test_haversine_and_polyline_interpolation():
    d = geo.haversine_m(16.0, 80.0, 16.0, 80.01)
    assert 1050 < d < 1100
    pts = [(16.0, 80.0), (16.0, 80.01), (16.01, 80.01)]
    start, end = geo.interpolate_polyline(pts, 0), geo.interpolate_polyline(pts, 1)
    assert start == pts[0] and end == pts[-1]
    mid = geo.interpolate_polyline(pts, 0.5)
    assert geo.haversine_m(*start, *mid) == pytest.approx(geo.polyline_length_m(pts) / 2, rel=0.02)


def test_osm_graph_conversion_keeps_largest_component_and_parallel_edges():
    g = nx.MultiDiGraph()
    for n, (x, y) in {1: (80.5300, 16.45), 2: (80.5310, 16.45), 3: (80.5320, 16.45), 9: (80.60, 16.50), 10: (80.601, 16.50)}.items():
        g.add_node(n, x=x, y=y)
    for u, v, hw in ((1, 2, "residential"), (2, 1, "residential"), (2, 3, "footway"), (3, 2, "footway"),
                     (1, 2, "primary"), (9, 10, "residential"), (10, 9, "residential")):
        g.add_edge(u, v, length=100.0, highway=hw, osmid=5)
    net = graph_to_network(g, "t", (16.45, 80.53), 500)
    assert len(net.nodes) == 3 and not net.is_synthetic            # nodes 9/10 form a disconnected island
    ids = {n.osm_id: n.node_id for n in net.nodes.values()}
    assert len(net.parallel_edges(ids[1], ids[2])) == 2           # parallel segments preserved
    assert net.metadata["dropped_disconnected_nodes"] == 2


def test_osm_capacity_is_heuristic_and_positive():
    assert osm_capacity("primary", 100) > osm_capacity("footway", 100) >= 2


def test_cache_key_is_stable_and_distinguishes_requests():
    k = cache.cache_key(16.45605, 80.53816, 1500, "walk")
    assert k == cache.cache_key(16.456050001, 80.53816, 1500, "walk")
    assert k != cache.cache_key(16.45605, 80.53816, 2000, "walk")


def test_playback_stepping_clamps_and_stops():
    assert pb.step(0, 10, -1) == 0 and pb.step(9, 10, 1) == 9 and pb.step(3, 10, 2) == 5
    s = pb.PlaybackState(1, index=8, playing=True)
    pb.advance_playing(s, 10)
    assert s.index == 9 and s.playing is False


def test_osm_conversion_keeps_one_way_pedestrian_branches_in_weak_component():
    g = nx.MultiDiGraph()
    for n, (x, y) in {1: (80.5300, 16.45), 2: (80.5310, 16.45), 3: (80.5320, 16.45),
                       4: (80.5330, 16.45), 9: (80.60, 16.50), 10: (80.601, 16.50)}.items():
        g.add_node(n, x=x, y=y)
    for u, v in ((1, 2), (2, 1), (2, 3), (3, 2), (3, 4), (9, 10), (10, 9)):
        g.add_edge(u, v, length=100.0, highway="footway", osmid=5)
    net = graph_to_network(g, "weak-component-test", (16.45, 80.53), 500)
    assert len(net.nodes) == 4
    assert net.metadata["component_policy"] == "largest weakly connected component"
    # The one-way branch is retained, but its direction remains explicit.
    assert any(e.source != e.target for e in net.edges.values())
