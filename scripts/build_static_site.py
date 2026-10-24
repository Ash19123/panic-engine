"""Build the static GitHub Pages site (index.html, live.html, safe.html) from a stored run in the SQLite database.

    python -m scripts.build_static_site --run 11 --out .

The live replay and the safe-stop page are the same self-contained HTML the dashboard uses; only the pieces that need
the Python phone server (state sync, /qr) are replaced by browser-side equivalents.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from database import repositories as repo
from database.connection import connect
from geo import network_cache as cache
from geo import network_loader as loader
from visualization import live_view as lv
from visualization import scene3d as sc

QR_JS = "https://cdnjs.cloudflare.com/ajax/libs/qrcode-generator/1.4.4/qrcode.min.js"
QR_OLD = "$('cQr').src='/qr?data='+encodeURIComponent(u.toString());"
QR_NEW = ("try{var q=qrcode(0,'M');q.addData(u.toString());q.make();$('cQr').src=q.createDataURL(4);}"
          "catch(e){$('cQr').style.display='none';}")

LANDING = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>PANIC ENGINE</title>
<style>
:root{--blue:#2b5c9e;--navy:#1b2a41;--panel:#f0f2f5;--chip:#dce4ee;--ink:#222}
*{box-sizing:border-box}body{margin:0;font:17px/1.55 Arial,Helvetica,sans-serif;color:var(--ink);background:#fff}
.bar{background:var(--navy);color:#fff;padding:10px 20px;font-size:14px;letter-spacing:.08em}
main{max-width:900px;margin:0 auto;padding:28px 20px 60px}
h1{color:var(--blue);font-size:34px;line-height:1.2;margin:.2em 0}h2{color:var(--blue);font-size:22px;margin:1.6em 0 .4em}
.sub{font-size:19px;color:#444;margin:0 0 14px}.chips span{display:inline-block;background:var(--chip);color:#334;font-size:13px;
border-radius:12px;padding:2px 10px;margin:0 6px 6px 0}
.panel{background:var(--panel);border-radius:8px;padding:16px 20px;margin:14px 0}.panel p{margin:.3em 0}
.btns{display:flex;flex-wrap:wrap;gap:12px;margin:18px 0}.btns a{background:var(--blue);color:#fff;text-decoration:none;padding:12px 20px;
border-radius:6px;font-weight:bold}.btns a.alt{background:#fff;color:var(--blue);border:2px solid var(--blue)}
table{border-collapse:collapse;width:100%;font-size:15px}td,th{border-bottom:1px solid #dde;padding:7px 8px;text-align:left;vertical-align:top}
th{background:var(--panel)}code{background:var(--panel);padding:1px 5px;border-radius:4px;font-size:14px}
.cite{border-left:4px solid var(--blue)}small{color:#555}footer{color:#555;font-size:14px;margin-top:40px}
</style></head><body>
<div class="bar">PANIC ENGINE</div>
<main>
<h1>PANIC ENGINE</h1>
<p class="sub">An agent-based evacuation and emergency-alert simulator, with a live 3D replay, a phone view and a nearest safe-stop card.</p>
<div class="chips"><span>agent-based model</span><span>evacuation</span><span>alert channels</span><span>OpenStreetMap</span><span>SQLite</span><span>NetworkX</span></div>
<div class="btns"><a href="live.html">Open the live 3D demo</a><a href="safe.html">Find my nearest safe stop</a>
<a class="alt" href="https://github.com/Ash19123/panic-engine">Source code</a></div>
<div class="panel"><p><b>Educational model.</b> This is a college project, not a real alert system and not a validated predictor.
Safe stops are hypothetical, not official shelters. Nothing here sends a message to anyone.</p></div>

<h2>Why</h2>
<p>Most people do not have a phone in hand when something happens. A phone alert only helps those who see it. PANIC ENGINE asks who gets warned,
how fast, and whether they reach safety in time, on the road network of Amrita Vishwa Vidyapeetham, Amaravati.</p>

<h2>What you can try on this page</h2>
<table><tr><th>Page</th><th>What it shows</th></tr>
<tr><td><a href="live.html">Live 3D demo</a></td><td>A stored bomb-alert drill on the real campus-area road map: 250 people, a blast area, an alert area,
safe stops, a simulated phone with siren, route and tracking, and manual control of one person.</td></tr>
<tr><td><a href="safe.html">Safe-stop card</a></td><td>Tap the map or use your location to see the nearest safe stop, walking distance, directions and a printable card with a QR code.</td></tr></table>
<p><small>These pages replay a saved run. To run new simulations, the alert planner and the SQL console, run the full Streamlit app from the repository.</small></p>

<h2>How it works</h2>
<p>Agents walk a road graph with capacity-limited edges; three routing strategies and four alert channels (phone, siren, neighbour relay, none) are compared on identical populations.
Every run is stored in SQLite and analysed with SQL. In one saved run on the campus map, congestion-aware routing and shortest-path routing
gave different completion rates; the numbers depend on assumed speeds, capacities and reaction delays.</p>

<div class="panel cite"><p><b>Inspiration.</b> We took inspiration from Carles, Chapuis, Douvinet, P&eacute;roche and Bopp (2024),
&ldquo;An Agent-Based Model to Simulate the Effects of Tsunami Warnings on Pedestrian Evacuation: Sensitivity Analysis and Early Findings&rdquo;,
PRIMA 2024, pp. 369&ndash;384, DOI 10.1007/978-3-031-77367-9_28 (HAL hal-04794796).
They study sirens versus cell broadcast and alert timing for a tsunami in Cannes. PANIC ENGINE is our own model for a bomb scenario;
it is inspired by their questions and is not a replication of their work.</p></div>

<footer>Map data &copy; OpenStreetMap contributors (ODbL). Code: <a href="https://github.com/Ash19123/panic-engine">github.com/Ash19123/panic-engine</a></footer>
</main></body></html>
"""


def build(run_id: int, out: Path) -> None:
    conn = connect()
    try:
        run = repo.get_run(conn, run_id)
        cfg = repo.load_scenario_config(conn, run["scenario_id"])
        net = repo.load_network(conn, cfg.network_id)
        bundle = repo.load_replay_bundle(conn, run_id)
    finally:
        conn.close()
    zones = json.loads(cfg.description.split("danger_zones=", 1)[1]) if cfg.description.startswith("danger_zones=") else []
    keep_clear = ([(net.nodes[d.node_id].lat, net.nodes[d.node_id].lon, 70.0) for d in cfg.destinations]
                  + [(z["lat"], z["lon"], z["radius_m"]) for z in zones]
                  + [(h["lat"], h["lon"], h["blast_radius_m"]) for h in cfg.params.hazards])
    if net.is_synthetic:
        blds = sc.synthetic_buildings(net, keep_clear)
    else:
        raw = []
        for m in cache.list_cached():
            raw = loader.load_buildings(m["lat"], m["lon"], m["radius_m"])
            if raw:
                break
        blds = sc.osm_buildings(raw)
    live = lv.build_live_html(net, bundle, cfg.destinations, cfg.origins, zones, cfg.closures, blds,
                              cfg.params.edge_capacity_overrides, None, 900, hazards=cfg.params.hazards,
                              sync_url=None, run_id=run_id)
    safe = lv.build_safe_html(net, cfg.destinations, run["scenario_id"])
    assert QR_OLD in safe, "safe_page.html changed: update QR_OLD"
    safe = safe.replace(QR_OLD, QR_NEW).replace("</head>", f'<script src="{QR_JS}"></script></head>', 1)
    out.mkdir(parents=True, exist_ok=True)
    (out / "live.html").write_text(live, encoding="utf-8")
    (out / "safe.html").write_text(safe, encoding="utf-8")
    (out / "index.html").write_text(LANDING, encoding="utf-8")
    (out / ".nojekyll").write_text("", encoding="utf-8")
    for n in ("index.html", "live.html", "safe.html"):
        print(n, round((out / n).stat().st_size / 1e6, 2), "MB")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", type=int, required=True)
    ap.add_argument("--out", type=Path, default=Path("."))
    a = ap.parse_args()
    build(a.run, a.out)
