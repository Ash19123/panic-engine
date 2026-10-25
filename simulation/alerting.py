"""Siren placement: where would a few sirens warn the most people that phones do not reach?

Greedy maximum-coverage: repeatedly pick the road node whose siren radius covers the largest number of still
unwarned people, ignoring nodes inside a blast radius (a siren there would be destroyed). Greedy is within a
constant factor of the best possible placement for this problem and is fast enough for thousands of people.
"""
from __future__ import annotations  # modern type hints on older Python

import math  # trigonometry and hypot
from typing import Dict, Iterable, List, Sequence, Tuple  # type hints

import numpy as np  # fast array maths for the coverage matrix

from simulation.models import RoadNetwork  # the road graph class


def recommend_sirens(net: RoadNetwork, people_nodes: Sequence[int], radius_m: float, k: int, delay_s: float = 10.0,
                     avoid: Iterable[Tuple[float, float, float]] = (), max_candidates: int = 1500) -> List[Dict]:
    """Return up to ``k`` siren sites ``{node, lat, lon, radius_m, delay_s, covers}`` (``covers`` = people newly warned)."""
    people = list(people_nodes)  # node id of every person to warn
    if not people or k <= 0:  # nobody to warn or no sirens requested
        return []  # nothing to recommend
    lat0 = sum(net.nodes[n].lat for n in people) / len(people)  # mean latitude, used to convert degrees to metres
    kx, ky = math.cos(math.radians(lat0)) * 111_320.0, 110_540.0  # metres per degree of longitude and latitude
    pts = np.array([[net.nodes[n].lon * kx, net.nodes[n].lat * ky] for n in people])  # people positions in metres
    ids = sorted(net.nodes)  # all candidate node ids in a fixed order
    ids = ids[:: max(1, len(ids) // max_candidates)]  # thin the candidate sites on big networks
    avoid = list(avoid)  # circles (lat, lon, radius) where a siren would be destroyed
    ids = [i for i in ids if not any(  # keep only candidates outside every avoid circle
        math.hypot((net.nodes[i].lon - lon) * kx, (net.nodes[i].lat - lat) * ky) <= rad for lat, lon, rad in avoid)]  # distance test in metres
    if not ids:  # every candidate was inside an avoid circle
        return []  # nothing to recommend
    cand = np.array([[net.nodes[i].lon * kx, net.nodes[i].lat * ky] for i in ids])  # candidate positions in metres
    cover = ((cand[:, None, :] - pts[None, :, :]) ** 2).sum(axis=2) <= radius_m ** 2  # candidates x people
    covered = np.zeros(len(people), dtype=bool)  # who is already covered by a chosen siren
    sites: List[Dict] = []  # the chosen siren sites
    for _ in range(k):  # choose k sirens one at a time
        gain = (cover & ~covered).sum(axis=1)  # how many NEW people each candidate would cover
        best = int(gain.argmax())  # the candidate with the biggest gain
        if gain[best] == 0:  # no candidate covers anybody new
            break  # stop early
        covered |= cover[best]  # mark those people as covered
        nd = net.nodes[ids[best]]  # the chosen node
        sites.append({"node": ids[best], "lat": nd.lat, "lon": nd.lon, "radius_m": float(radius_m),  # record the site
                      "delay_s": float(delay_s), "covers": int(gain[best])})  # delay before sounding, people newly warned
    return sites  # the recommended sites
