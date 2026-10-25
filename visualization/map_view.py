"""Folium map construction. Pure functions: network + data in, ``folium.Map`` out."""
from __future__ import annotations  # modern type hints on older Python

from typing import Dict, Iterable, List, Optional, Sequence, Tuple  # type hints

from datetime import datetime, timedelta  # time handling for the animated layer

import folium  # 2D web maps

from config import settings  # shared settings
from simulation.models import ClosureSpec, DestinationSpec, OriginSpec, RoadNetwork  # scenario building blocks
from visualization.theme import STATE_STYLE  # colours and labels per agent state

OSM_TILES = "OpenStreetMap"  # name of the keyless background tiles
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
"""  # HTML box that explains the symbols on the map


def congestion_color(ratio: float) -> str:  # colour for how full a road is
    r = max(0.0, min(1.0, ratio))  # clamp to 0..1
    if r < 0.5:                      # green -> yellow
        g, rd = 200, int(80 + 350 * r)  # green stays high, red grows
    else:                            # yellow -> red
        rd, g = 235, int(200 - 360 * (r - 0.5))  # red stays high, green fades
    return f"#{min(rd, 255):02x}{max(g, 0):02x}3c"  # hexadecimal colour code


def base_map(network: RoadNetwork, zoom: int = 15) -> folium.Map:  # an empty map for a network
    if network.center:  # the network has a stored centre
        center = network.center  # use it
    else:  # otherwise use the middle of its bounding box
        s, w, n, e = network.bounds()  # south, west, north, east
        center = ((s + n) / 2, (w + e) / 2)  # the middle
    # Synthetic roads must never be drawn on top of real streets (that would imply they exist):
    # use a blank background for them and the OpenStreetMap tiles only for real data.
    m = folium.Map(location=center, zoom_start=zoom, tiles=None if network.is_synthetic else OSM_TILES,  # map with tiles only for real data (part 1)
                   control_scale=True, max_zoom=19)  # scale bar and zoom limit (part 2)
    if network.is_synthetic:  # fake grid
        m.get_root().html.add_child(folium.Element(  # add a permanent warning banner
            "<style>.leaflet-container{background:#0b1626 !important}</style>"  # dark blank background
            '<div style="position:fixed;top:10px;left:60px;z-index:9999;background:#ff8a3d;color:#111;'  # banner style (part 1)
            'padding:3px 10px;border-radius:4px;font:600 12px sans-serif">SYNTHETIC NETWORK - not real roads</div>'))  # banner style (part 2) and text
    s, w, n, e = network.bounds()  # extent of the network
    m.fit_bounds([[s, w], [n, e]])  # zoom to fit it
    return m  # the map


def draw_roads(m: folium.Map, network: RoadNetwork, occupancy: Optional[Dict[int, Tuple[int, int]]] = None,
               closed: Iterable[int] = (), capacity: Optional[Dict[int, int]] = None,
               bottlenecks: Iterable[int] = (), highlight: Iterable[int] = (), max_edges: int = 6000) -> None:  # draw the roads
    occupancy = occupancy or {}  # road -> (occupancy, queue)
    closed, bottlenecks, highlight = set(closed), set(bottlenecks), set(highlight)  # sets for fast lookup
    group = folium.FeatureGroup(name="Road segments", show=True)  # a toggleable layer
    drawn = 0  # roads drawn so far
    seen_pairs = set()  # (kept for compatibility)
    for eid, e in network.edges.items():  # every road
        if drawn >= max_edges:  # too many to draw
            break  # stop
        occ, q = occupancy.get(eid, (0, 0))  # people on it and waiting for it
        cap = (capacity or {}).get(eid, e.capacity)  # its capacity, possibly overridden
        ratio = occ / cap if cap else 0.0  # how full it is
        if eid in closed:  # closed road
            color, weight, dash, opacity = "#ffffff", 4, "6 6", 0.95  # white dashed line
        elif eid in highlight:  # highlighted road
            color, weight, dash, opacity = "#00e5ff", 5, None, 1.0  # cyan line
        else:  # ordinary road
            color, weight, dash, opacity = (congestion_color(ratio) if occ else "#5b6f8f"), 2 + 3 * min(ratio, 1), None, 0.8  # coloured by how full it is, thicker when fuller
        pair = (min(e.source, e.target), max(e.source, e.target), eid in closed)  # (kept for compatibility)
        tip = (f"edge {eid} ({e.road_class}) | capacity {cap} | occupancy {occ} | queue {q}"  # tooltip text (part 1)
               + (" | BLOCKED" if eid in closed else ""))  # tooltip text (part 2)
        folium.PolyLine(network.edge_polyline(eid), color=color, weight=weight, opacity=opacity, dash_array=dash,  # draw the road (part 1)
                        tooltip=tip).add_to(group)  # draw the road (part 2)
        drawn += 1  # count it
        if eid in bottlenecks:  # flagged bottleneck
            mid = network.edge_polyline(eid)[len(network.edge_polyline(eid)) // 2]  # a point in the middle of the road
            folium.CircleMarker(mid, radius=9, color="#ff00aa", fill=True, fill_opacity=0.35,  # pink circle (part 1)
                                tooltip=f"Bottleneck: edge {eid}").add_to(group)  # pink circle (part 2)
    group.add_to(m)  # add the layer to the map


def draw_endpoints(m: folium.Map, network: RoadNetwork, origins: Sequence[OriginSpec],
                   destinations: Sequence[DestinationSpec]) -> None:  # draw start places and safe stops
    for o in origins:  # each origin
        n = network.nodes[o.node_id]  # its node
        folium.Marker((n.lat, n.lon), tooltip=f"Origin (user-defined): {o.label or o.node_id}, weight {o.weight:g}",  # marker (part 1)
                      icon=folium.Icon(color="orange", icon="play", prefix="fa")).add_to(m)  # orange play icon (part 2)
    for d in destinations:  # each safe stop
        n = network.nodes[d.node_id]  # its node
        status = "HYPOTHETICAL - not an official shelter" if d.hypothetical else "verified"  # honesty label
        avail = "" if d.available else " [UNAVAILABLE]"  # mark closed stops
        folium.Marker((n.lat, n.lon),  # marker (part 1)
                      tooltip=f"Destination{avail}: {d.name} | capacity {d.capacity} | throughput {d.throughput_per_timestep}/step | {status}",  # details (part 2)
                      icon=folium.Icon(color="purple" if d.available else "gray", icon="flag", prefix="fa")).add_to(m)  # flag icon, grey if closed (part 3)


def draw_agents(m: folium.Map, rows: Sequence[dict], max_agents: int = 400) -> int:  # draw people
    """Draw agent markers from genuine snapshot rows. Returns how many were drawn."""
    stride = max(1, -(-len(rows) // max_agents))  # draw every n-th agent to stay under the limit
    group = folium.FeatureGroup(name="Agents", show=True)  # a toggleable layer
    drawn = 0  # markers drawn
    for r in rows:  # each snapshot row
        if r["agent_id"] % stride:  # not part of the sample
            continue  # skip it
        s = STATE_STYLE[r["state"]]  # colour and label of its state
        folium.CircleMarker((r["latitude"], r["longitude"]), radius=4 if r["state"] != "EVACUATED" else 3,  # small circle at the position (part 1)
                            color=s["color"], fill=True, fill_opacity=0.9, weight=1,  # coloured by state (part 2)
                            tooltip=f"agent {r['agent_id']}: {s['label']}").add_to(group)  # hover text (part 3)
        drawn += 1  # count it
    group.add_to(m)  # add the layer to the map
    return drawn  # how many were drawn


def add_legend(m: folium.Map) -> None:  # add the legend box
    m.get_root().html.add_child(folium.Element(LEGEND_HTML))  # insert the HTML


def study_area_circle(m: folium.Map, center: Tuple[float, float], radius_m: float, label: str) -> None:  # outline of the study area
    folium.Circle(center, radius=radius_m, color="#ffb000", fill=False, weight=2, dash_array="8 6",  # amber dashed circle (part 1)
                  tooltip=label).add_to(m)  # with a label (part 2)


def node_click_layer(m: folium.Map, network: RoadNetwork, max_nodes: int = 3000) -> None:  # small dots on nodes so they can be picked
    for i, n in enumerate(network.nodes.values()):  # each node
        if i >= max_nodes:  # too many
            break  # stop
        folium.CircleMarker((n.lat, n.lon), radius=2, color="#9fb3d1", fill=True, fill_opacity=0.7,  # small dot (part 1)
                            tooltip=f"node {n.node_id}").add_to(m)  # with its id on hover (part 2)


def timestamped_agent_layer(m: folium.Map, frames: Dict[float, List[dict]], period_s: float, max_agents: int = 250):  # browser-side animation
    """Optional browser-side animation built ONLY from stored snapshot frames."""
    from folium.plugins import TimestampedGeoJson  # imported only when needed
    features = []  # one GeoJSON feature per agent per frame
    for t, rows in sorted(frames.items()):  # each frame in time order
        stride = max(1, -(-len(rows) // max_agents))  # sample to stay under the limit
        for r in rows:  # each agent row
            if r["agent_id"] % stride:  # not part of the sample
                continue  # skip it
            s = STATE_STYLE[r["state"]]  # colour and label of its state
            features.append({"type": "Feature", "geometry": {"type": "Point", "coordinates": [r["longitude"], r["latitude"]]},  # a point at the agent's position
                             "properties": {"time": (datetime(2000, 1, 1) + timedelta(seconds=t)).isoformat(),  # fake calendar date plus the simulation time
                                            "popup": f"agent {r['agent_id']}: {s['label']}",  # popup text
                                            "icon": "circle", "iconstyle": {"fillColor": s["color"], "fillOpacity": 0.9,  # circle style (part 1)
                                                                            "stroke": "true", "radius": 4}}})  # circle style (part 2)
    TimestampedGeoJson({"type": "FeatureCollection", "features": features}, period="PT10S", add_last_point=False,  # animated layer (part 1)
                       auto_play=False, loop=False, max_speed=20, loop_button=True, time_slider_drag_update=True,  # controls (part 2)
                       duration="PT10S").add_to(m)  # each point lasts 10 s (part 3)
