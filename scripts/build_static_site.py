"""Build the static GitHub Pages site (index.html, live.html, hero.jpg) from a stored run in the SQLite database.

    python -m scripts.build_static_site --run 11 --out .

The live replay is the same self-contained HTML the dashboard uses, with the Python phone server (state sync) switched off.
hero.jpg is a screenshot of the replay that you capture yourself and drop next to index.html.
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

INDEX_TEMPLATE = Path(__file__).with_name("site_index.html")  # the landing page lives in its own file


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
    back = ('<a href="index.html" style="position:fixed;right:12px;bottom:12px;z-index:99999;background:#2b5c9e;color:#fff;'
            'font:600 13px Segoe UI,Arial,sans-serif;padding:8px 14px;border-radius:20px;text-decoration:none;'
            'box-shadow:0 4px 14px rgba(0,0,0,.4)">&larr; About this project</a>')  # link back to the landing page
    live = live.replace("<body>", "<body>" + back, 1) if "<body>" in live else live  # inject it right after <body>
    out.mkdir(parents=True, exist_ok=True)
    (out / "live.html").write_text(live, encoding="utf-8")
    (out / "index.html").write_text(INDEX_TEMPLATE.read_text(encoding="utf-8"), encoding="utf-8")
    (out / ".nojekyll").write_text("", encoding="utf-8")
    for n in ("index.html", "live.html"):
        print(n, round((out / n).stat().st_size / 1e6, 2), "MB")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", type=int, required=True)
    ap.add_argument("--out", type=Path, default=Path("."))
    a = ap.parse_args()
    build(a.run, a.out)
