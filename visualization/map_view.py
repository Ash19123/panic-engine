"""Folium map construction. Pure functions: network + data in, ``folium.Map`` out."""
from __future__ import annotations

from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from datetime import datetime, timedelta

import folium

from config import settings
from simulation.models import ClosureSpec, DestinationSpec, OriginSpec, RoadNetwork
from visualization.theme import STATE_STYLE

OSM_TILES = "OpenStreetMap"
LEGEND_HTML = """
<div style="position:fixed;bottom:24px;left:24px;z-index:9999;background:rgba(15,27,45,.92);color:#e8eef7;
 padding:10px 12px;border-radius:8px;font:12px/1.5 sans-serif;border:1px solid #2a3f5f;max-width:230px">
<b>Legend</b><br>
<span style="color:#8b97a8">&#9679;</span> waiting (not departed)<br>
<span style="color:#4da3ff">&#9654;</span> moving<br>
<span style="color:#ff8a3d">&#10074;&#10074;</span> queued / held<br>
<span style="color:#2fbf71">&#10010;</span> evacuated<br>
<span style="color:#e5484d">&#10006;</span> unable to evacuate<br>
<span style="color:#ffb000">&#9632;</span> origin (user-defined) &nbsp;
<span style="color:#b266ff">&#9650;</span> destination (hypothetical)<br>
Road colour: green &rarr; yellow &rarr; red = occupancy / capacity<br>
<span style="color:#111;background:#fff;padding:0 3px">- - -</span> blocked road &nbsp; <span style="color:#ff00aa">&#9632;</span> bottleneck<br>
<i>Marker tooltips give the state in words.</i></div>
"""


def congestion_color(ratio: float) -> str:
    r = max(0.0, min(1.0, ratio))
    if r < 0.5:                      # green -> yellow
        g, rd = 200, int(80 + 350 * r)
    else:                            # yellow -> red
        rd, g = 235, int(200 - 360 * (r - 0.5))
    return f"#{min(rd, 255):02x}{max(g, 0):02x}3c"


def base_map(network: RoadNetwork, zoom: int = 15) -> folium.Map:
    if network.center:
        center = network.center
    else:
        s, w, n, e = network.bounds()
        center = ((s + n) / 2, (w + e) / 2)
    # Synthetic roads must never be drawn on top of real streets (that would imply they exist):
    # use a blank background for them and the OpenStreetMap tiles only for real data.
    m = folium.Map(location=center, zoom_start=zoom, tiles=None if network.is_synthetic else OSM_TILES,
                   control_scale=True, max_zoom=19)
    if network.is_synthetic:
        m.get_root().html.add_child(folium.Element(
            "<style>.leaflet-container{background:#0b1626 !important}</style>"
            '<div style="position:fixed;top:10px;left:60px;z-index:9999;background:#ff8a3d;color:#111;'
            'padding:3px 10px;border-radius:4px;font:600 12px sans-serif">SYNTHETIC NETWORK - not real roads</div>'))
    s, w, n, e = network.bounds()
    m.fit_bounds([[s, w], [n, e]])
    return m


def draw_roads(m: folium.Map, network: RoadNetwork, occupancy: Optional[Dict[int, Tuple[int, int]]] = None,
               closed: Iterable[int] = (), capacity: Optional[Dict[int, int]] = None,
               bottlenecks: Iterable[int] = (), highlight: Iterable[int] = (), max_edges: int = 6000) -> None:
    occupancy = occupancy or {}
    closed, bottlenecks, highlight = set(closed), set(bottlenecks), set(highlight)
    group = folium.FeatureGroup(name="Road segments", show=True)
    drawn = 0
    seen_pairs = set()
    for eid, e in network.edges.items():
        if drawn >= max_edges:
            break
        occ, q = occupancy.get(eid, (0, 0))
        cap = (capacity or {}).get(eid, e.capacity)
        ratio = occ / cap if cap else 0.0
        if eid in closed:
            color, weight, dash, opacity = "#ffffff", 4, "6 6", 0.95
        elif eid in highlight:
            color, weight, dash, opacity = "#00e5ff", 5, None, 1.0
        else:
            color, weight, dash, opacity = (congestion_color(ratio) if occ else "#5b6f8f"), 2 + 3 * min(ratio, 1), None, 0.8
        pair = (min(e.source, e.target), max(e.source, e.target), eid in closed)
        tip = (f"edge {eid} ({e.road_class}) | capacity {cap} | occupancy {occ} | queue {q}"
               + (" | BLOCKED" if eid in closed else ""))
        folium.PolyLine(network.edge_polyline(eid), color=color, weight=weight, opacity=opacity, dash_array=dash,
                        tooltip=tip).add_to(group)
        drawn += 1
        if eid in bottlenecks:
            mid = network.edge_polyline(eid)[len(network.edge_polyline(eid)) // 2]
            folium.CircleMarker(mid, radius=9, color="#ff00aa", fill=True, fill_opacity=0.35,
                                tooltip=f"Bottleneck: edge {eid}").add_to(group)
    group.add_to(m)


def draw_endpoints(m: folium.Map, network: RoadNetwork, origins: Sequence[OriginSpec],
                   destinations: Sequence[DestinationSpec]) -> None:
    for o in origins:
        n = network.nodes[o.node_id]
        folium.Marker((n.lat, n.lon), tooltip=f"Origin (user-defined): {o.label or o.node_id}, weight {o.weight:g}",
                      icon=folium.Icon(color="orange", icon="play", prefix="fa")).add_to(m)
    for d in destinations:
        n = network.nodes[d.node_id]
        status = "HYPOTHETICAL - not an official shelter" if d.hypothetical else "verified"
        avail = "" if d.available else " [UNAVAILABLE]"
        folium.Marker((n.lat, n.lon),
                      tooltip=f"Destination{avail}: {d.name} | capacity {d.capacity} | throughput {d.throughput_per_timestep}/step | {status}",
                      icon=folium.Icon(color="purple" if d.available else "gray", icon="flag", prefix="fa")).add_to(m)


def draw_agents(m: folium.Map, rows: Sequence[dict], max_agents: int = 400) -> int:
    """Draw agent markers from genuine snapshot rows. Returns how many were drawn."""
    stride = max(1, -(-len(rows) // max_agents))
    group = folium.FeatureGroup(name="Agents", show=True)
    drawn = 0
    for r in rows:
        if r["agent_id"] % stride:
            continue
        s = STATE_STYLE[r["state"]]
        folium.CircleMarker((r["latitude"], r["longitude"]), radius=4 if r["state"] != "EVACUATED" else 3,
                            color=s["color"], fill=True, fill_opacity=0.9, weight=1,
                            tooltip=f"agent {r['agent_id']}: {s['label']}").add_to(group)
        drawn += 1
    group.add_to(m)
    return drawn


def add_legend(m: folium.Map) -> None:
    m.get_root().html.add_child(folium.Element(LEGEND_HTML))


def study_area_circle(m: folium.Map, center: Tuple[float, float], radius_m: float, label: str) -> None:
    folium.Circle(center, radius=radius_m, color="#ffb000", fill=False, weight=2, dash_array="8 6",
                  tooltip=label).add_to(m)


def node_click_layer(m: folium.Map, network: RoadNetwork, max_nodes: int = 3000) -> None:
    for i, n in enumerate(network.nodes.values()):
        if i >= max_nodes:
            break
        folium.CircleMarker((n.lat, n.lon), radius=2, color="#9fb3d1", fill=True, fill_opacity=0.7,
                            tooltip=f"node {n.node_id}").add_to(m)


def timestamped_agent_layer(m: folium.Map, frames: Dict[float, List[dict]], period_s: float, max_agents: int = 250):
    """Optional browser-side animation built ONLY from stored snapshot frames."""
    from folium.plugins import TimestampedGeoJson
    features = []
    for t, rows in sorted(frames.items()):
        stride = max(1, -(-len(rows) // max_agents))
        for r in rows:
            if r["agent_id"] % stride:
                continue
            s = STATE_STYLE[r["state"]]
            features.append({"type": "Feature", "geometry": {"type": "Point", "coordinates": [r["longitude"], r["latitude"]]},
                             "properties": {"time": (datetime(2000, 1, 1) + timedelta(seconds=t)).isoformat(),
                                            "popup": f"agent {r['agent_id']}: {s['label']}",
                                            "icon": "circle", "iconstyle": {"fillColor": s["color"], "fillOpacity": 0.9,
                                                                            "stroke": "true", "radius": 4}}})
    TimestampedGeoJson({"type": "FeatureCollection", "features": features}, period="PT10S", add_last_point=False,
                       auto_play=False, loop=False, max_speed=20, loop_button=True, time_slider_drag_update=True,
                       duration="PT10S").add_to(m)
