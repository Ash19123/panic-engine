"""Deterministic synthetic road network (fallback + unit-test fixture).

THE SYNTHETIC NETWORK IS NOT A MAP OF ANY REAL PLACE. It is a regular grid with
a few removed links, a two-link "bridge" choke between its west and east halves
and different road classes, placed near the study-area centre only so that the
map component has somewhere to draw it. It is always labelled SYNTHETIC.
"""
from __future__ import annotations

import random
from typing import List, Optional, Tuple

from config import settings
from geo import geographic_utils as geo
from simulation.models import DestinationSpec, Edge, Node, RoadNetwork

# (width factor, label) -> capacity = max(2, round(length_m * PERSONS_PER_METER * width factor))
ROAD_CLASSES = {"arterial": 2.0, "local": 1.0, "narrow": 0.5, "bridge": 0.25}
PERSONS_PER_METER = 0.25


def edge_capacity(length_m: float, road_class: str) -> int:
    return max(2, round(length_m * PERSONS_PER_METER * ROAD_CLASSES.get(road_class, 1.0)))


def build_synthetic_network(rows: int = 8, cols: int = 10, spacing_m: float = 120.0,
                            seed: int = 7, center: Optional[Tuple[float, float]] = None,
                            speed_mps: float = settings.WALKING_SPEED_MPS) -> RoadNetwork:
    """Grid network with a river-like choke between the west and east halves.

    Columns [0, cols//2) are "west", the rest "east"; only three links cross the
    middle (rows 1, rows//2, rows-2), of small capacity. Every
    link is bidirectional. A handful of other links are removed (seeded) so the
    graph is irregular, but strong connectivity is verified.
    """
    rng = random.Random(seed)
    clat, clon = center or (settings.CENTER_LAT, settings.CENTER_LON)
    net = RoadNetwork("Synthetic grid (NOT real roads)", "SYNTHETIC", center=(clat, clon),
                      radius_m=max(rows, cols) * spacing_m,
                      metadata={"rows": rows, "cols": cols, "spacing_m": spacing_m, "seed": seed,
                                "warning": "Synthetic fallback - not a map of any real location."})
    nid = {}
    nxt = 1
    for r in range(rows):
        for c in range(cols):
            north = (rows / 2 - r) * spacing_m
            east = (c - cols / 2) * spacing_m
            lat, lon = geo.offset_latlon(clat, clon, north, east)
            net.add_node(Node(nxt, lat, lon))
            nid[(r, c)] = nxt
            nxt += 1

    mid = cols // 2
    bridges = {1, rows // 2, rows - 2}
    links: List[Tuple[Tuple[int, int], Tuple[int, int], str]] = []
    for r in range(rows):
        for c in range(cols):
            if c + 1 < cols:
                crossing = (c == mid - 1)
                if crossing:
                    if r not in bridges:
                        continue
                    cls = "narrow" if r == rows // 2 else "bridge"
                else:
                    cls = "arterial" if r == rows // 2 else ("narrow" if rng.random() < 0.15 else "local")
                links.append(((r, c), (r, c + 1), cls))
            if r + 1 < rows:
                cls = "arterial" if c == mid + 1 else ("narrow" if rng.random() < 0.15 else "local")
                links.append(((r, c), (r + 1, c), cls))

    # Remove a few non-structural links to break regularity.
    removable = [l for l in links if l[2] in ("local", "narrow")]
    rng.shuffle(removable)
    removed = set()
    for l in removable[: max(1, len(links) // 14)]:
        removed.add((l[0], l[1]))
    eid = 1
    for a, b, cls in links:
        if (a, b) in removed:
            continue
        na, nb = net.nodes[nid[a]], net.nodes[nid[b]]
        length = geo.haversine_m(na.lat, na.lon, nb.lat, nb.lon)
        for s, t in ((na, nb), (nb, na)):
            net.add_edge(Edge(eid, s.node_id, t.node_id, length, length / speed_mps,
                              edge_capacity(length, cls), road_class=cls))
            eid += 1
    if net.strongly_connected_fraction() < 1.0:
        # Extremely unlikely for the default seed; fail loudly rather than ship a broken fallback.
        raise RuntimeError("synthetic network is not strongly connected; choose another seed")
    net.network_id = "synthetic-" + net.fingerprint()
    return net


def default_origins_destinations(net: RoadNetwork) -> Tuple[List[Tuple[int, str]], List[DestinationSpec]]:
    """Hypothetical demo origins (west side) and destinations (east/boundary).

    Positions are arbitrary demonstration choices on the synthetic grid.
    """
    meta = net.metadata
    rows, cols = meta["rows"], meta["cols"]
    ids = {(i // cols, i % cols): i + 1 for i in range(rows * cols)}
    origins = [(ids[(rows // 2, 1)], "Demo origin A (west)"), (ids[(2, 2)], "Demo origin B (north-west)"),
               (ids[(rows - 3, 1)], "Demo origin C (south-west)")]
    dests = [
        DestinationSpec(None, ids[(0, cols - 1)], "Hypothetical safe area NE", 150, 6,
                        source_type="SYNTHETIC_DEMO", hypothetical=True),
        DestinationSpec(None, ids[(rows - 1, cols - 1)], "Hypothetical safe area SE", 150, 6,
                        source_type="SYNTHETIC_DEMO", hypothetical=True),
        DestinationSpec(None, ids[(rows // 2, cols - 1)], "Hypothetical safe area E", 150, 6,
                        source_type="SYNTHETIC_DEMO", hypothetical=True),
    ]
    return origins, dests
