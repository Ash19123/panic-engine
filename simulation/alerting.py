"""Siren placement: where would a few sirens warn the most people that phones do not reach?

Greedy maximum-coverage: repeatedly pick the road node whose siren radius covers the largest number of still
unwarned people, ignoring nodes inside a blast radius (a siren there would be destroyed). Greedy is within a
constant factor of the best possible placement for this problem and is fast enough for thousands of people.
"""
from __future__ import annotations

import math
from typing import Dict, Iterable, List, Sequence, Tuple

import numpy as np

from simulation.models import RoadNetwork


def recommend_sirens(net: RoadNetwork, people_nodes: Sequence[int], radius_m: float, k: int, delay_s: float = 10.0,
                     avoid: Iterable[Tuple[float, float, float]] = (), max_candidates: int = 1500) -> List[Dict]:
    """Return up to ``k`` siren sites ``{node, lat, lon, radius_m, delay_s, covers}`` (``covers`` = people newly warned)."""
    people = list(people_nodes)
    if not people or k <= 0:
        return []
    lat0 = sum(net.nodes[n].lat for n in people) / len(people)
    kx, ky = math.cos(math.radians(lat0)) * 111_320.0, 110_540.0
    pts = np.array([[net.nodes[n].lon * kx, net.nodes[n].lat * ky] for n in people])
    ids = sorted(net.nodes)
    ids = ids[:: max(1, len(ids) // max_candidates)]                   # thin the candidate sites on big networks
    avoid = list(avoid)
    ids = [i for i in ids if not any(
        math.hypot((net.nodes[i].lon - lon) * kx, (net.nodes[i].lat - lat) * ky) <= rad for lat, lon, rad in avoid)]
    if not ids:
        return []
    cand = np.array([[net.nodes[i].lon * kx, net.nodes[i].lat * ky] for i in ids])
    cover = ((cand[:, None, :] - pts[None, :, :]) ** 2).sum(axis=2) <= radius_m ** 2     # candidates x people
    covered = np.zeros(len(people), dtype=bool)
    sites: List[Dict] = []
    for _ in range(k):
        gain = (cover & ~covered).sum(axis=1)
        best = int(gain.argmax())
        if gain[best] == 0:
            break
        covered |= cover[best]
        nd = net.nodes[ids[best]]
        sites.append({"node": ids[best], "lat": nd.lat, "lon": nd.lon, "radius_m": float(radius_m),
                      "delay_s": float(delay_s), "covers": int(gain[best])})
    return sites
