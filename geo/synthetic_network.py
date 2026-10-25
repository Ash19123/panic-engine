"""Deterministic synthetic road network (fallback + unit-test fixture).

THE SYNTHETIC NETWORK IS NOT A MAP OF ANY REAL PLACE. It is a regular grid with
a few removed links, a two-link "bridge" choke between its west and east halves
and different road classes, placed near the study-area centre only so that the
map component has somewhere to draw it. It is always labelled SYNTHETIC.
"""
from __future__ import annotations  # modern type hints on older Python

import random  # seeded random numbers
from typing import List, Optional, Tuple  # type hints

from config import settings  # centre coordinates and walking speed
from geo import geographic_utils as geo  # distance and offset helpers
from simulation.models import DestinationSpec, Edge, Node, RoadNetwork  # network building blocks

# (width factor, label) -> capacity = max(2, round(length_m * PERSONS_PER_METER * width factor))
ROAD_CLASSES = {"arterial": 2.0, "local": 1.0, "narrow": 0.5, "bridge": 0.25}  # width factor per road class
PERSONS_PER_METER = 0.25  # how many people fit per metre of a standard road


def edge_capacity(length_m: float, road_class: str) -> int:  # capacity of a road from its length and class
    return max(2, round(length_m * PERSONS_PER_METER * ROAD_CLASSES.get(road_class, 1.0)))  # never fewer than 2 people


def build_synthetic_network(rows: int = 8, cols: int = 10, spacing_m: float = 120.0,
                            seed: int = 7, center: Optional[Tuple[float, float]] = None,
                            speed_mps: float = settings.WALKING_SPEED_MPS) -> RoadNetwork:  # build the fake grid
    """Grid network with a river-like choke between the west and east halves.

    Columns [0, cols//2) are "west", the rest "east"; only three links cross the
    middle (rows 1, rows//2, rows-2), of small capacity. Every
    link is bidirectional. A handful of other links are removed (seeded) so the
    graph is irregular, but strong connectivity is verified.
    """
    rng = random.Random(seed)  # seeded: the same grid every time
    clat, clon = center or (settings.CENTER_LAT, settings.CENTER_LON)  # place the grid at the study-area centre by default
    net = RoadNetwork("Synthetic grid (NOT real roads)", "SYNTHETIC", center=(clat, clon),  # name, source label, centre
                      radius_m=max(rows, cols) * spacing_m,  # rough size of the grid
                      metadata={"rows": rows, "cols": cols, "spacing_m": spacing_m, "seed": seed,  # remember how it was built
                                "warning": "Synthetic fallback - not a map of any real location."})  # visible warning
    nid = {}  # (row, column) -> node id
    nxt = 1  # next node id
    for r in range(rows):  # each row
        for c in range(cols):  # each column
            north = (rows / 2 - r) * spacing_m  # metres north of the centre
            east = (c - cols / 2) * spacing_m  # metres east of the centre
            lat, lon = geo.offset_latlon(clat, clon, north, east)  # coordinates of this grid point
            net.add_node(Node(nxt, lat, lon))  # add the node
            nid[(r, c)] = nxt  # remember its id
            nxt += 1  # next id

    mid = cols // 2  # first column of the east half
    bridges = {1, rows // 2, rows - 2}  # the only rows that cross between west and east
    links: List[Tuple[Tuple[int, int], Tuple[int, int], str]] = []  # (from cell, to cell, road class)
    for r in range(rows):  # each row
        for c in range(cols):  # each column
            if c + 1 < cols:  # a link to the east neighbour
                crossing = (c == mid - 1)  # does it cross the middle?
                if crossing:  # a crossing link
                    if r not in bridges:  # only three rows have a crossing
                        continue  # skip the others
                    cls = "narrow" if r == rows // 2 else "bridge"  # the middle one is narrow, the other two are bridges
                else:  # an ordinary east-west link
                    cls = "arterial" if r == rows // 2 else ("narrow" if rng.random() < 0.15 else "local")  # middle row is an arterial, some random narrow roads
                links.append(((r, c), (r, c + 1), cls))  # record the link
            if r + 1 < rows:  # a link to the south neighbour
                cls = "arterial" if c == mid + 1 else ("narrow" if rng.random() < 0.15 else "local")  # one arterial column, some random narrow roads
                links.append(((r, c), (r + 1, c), cls))  # record the link

    # Remove a few non-structural links to break regularity.
    removable = [l for l in links if l[2] in ("local", "narrow")]  # only ordinary roads may be removed
    rng.shuffle(removable)  # random order
    removed = set()  # links to drop
    for l in removable[: max(1, len(links) // 14)]:  # about 1 in 14 links
        removed.add((l[0], l[1]))  # mark it removed
    eid = 1  # next road id
    for a, b, cls in links:  # every link
        if (a, b) in removed:  # dropped
            continue  # skip it
        na, nb = net.nodes[nid[a]], net.nodes[nid[b]]  # its two end nodes
        length = geo.haversine_m(na.lat, na.lon, nb.lat, nb.lon)  # length in metres
        for s, t in ((na, nb), (nb, na)):  # one road in each direction
            net.add_edge(Edge(eid, s.node_id, t.node_id, length, length / speed_mps,  # id, ends, length, walking time
                              edge_capacity(length, cls), road_class=cls))  # capacity and class
            eid += 1  # next id
    if net.strongly_connected_fraction() < 1.0:  # some node cannot reach another
        # Extremely unlikely for the default seed; fail loudly rather than ship a broken fallback.
        raise RuntimeError("synthetic network is not strongly connected; choose another seed")  # stop
    net.network_id = "synthetic-" + net.fingerprint()  # id derived from the content
    return net  # the finished network


def default_origins_destinations(net: RoadNetwork) -> Tuple[List[Tuple[int, str]], List[DestinationSpec]]:  # demo start points and safe stops
    """Hypothetical demo origins (west side) and destinations (east/boundary).

    Positions are arbitrary demonstration choices on the synthetic grid.
    """
    meta = net.metadata  # how the grid was built
    rows, cols = meta["rows"], meta["cols"]  # grid size
    ids = {(i // cols, i % cols): i + 1 for i in range(rows * cols)}  # (row, column) -> node id
    origins = [(ids[(rows // 2, 1)], "Demo origin A (west)"), (ids[(2, 2)], "Demo origin B (north-west)"),  # two start points
               (ids[(rows - 3, 1)], "Demo origin C (south-west)")]  # and a third
    dests = [  # three hypothetical safe stops on the east side
        DestinationSpec(None, ids[(0, cols - 1)], "Hypothetical safe area NE", 150, 6,  # north-east corner
                        source_type="SYNTHETIC_DEMO", hypothetical=True),  # flagged as not real
        DestinationSpec(None, ids[(rows - 1, cols - 1)], "Hypothetical safe area SE", 150, 6,  # south-east corner
                        source_type="SYNTHETIC_DEMO", hypothetical=True),  # flagged as not real
        DestinationSpec(None, ids[(rows // 2, cols - 1)], "Hypothetical safe area E", 150, 6,  # middle of the east edge
                        source_type="SYNTHETIC_DEMO", hypothetical=True),  # flagged as not real
    ]
    return origins, dests  # start points and safe stops
