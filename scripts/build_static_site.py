"""Build the static GitHub Pages site (index.html, live.html, hero.jpg) from a stored run in the SQLite database.

    python -m scripts.build_static_site --run 11 --out .

The live replay is the same self-contained HTML the dashboard uses, with the Python phone server (state sync) switched off.
hero.jpg is a screenshot of the replay that you capture yourself and drop next to index.html.
"""
from __future__ import annotations  # modern type hints on older Python

import argparse  # command-line options
import json  # read danger zones stored as JSON text
from pathlib import Path  # file paths

from database import repositories as repo  # load runs and scenarios
from database.connection import connect  # open the database
from geo import network_cache as cache  # list cached map areas
from geo import network_loader as loader  # load building footprints
from visualization import live_view as lv  # builds the live page
from visualization import scene3d as sc  # building decoration helpers

INDEX_TEMPLATE = Path(__file__).with_name("site_index.html")  # the landing page lives in its own file


def build(run_id: int, out: Path) -> None:  # write the site files for one stored run
    conn = connect()  # open the database
    try:  # always close it
        run = repo.get_run(conn, run_id)  # the run row
        cfg = repo.load_scenario_config(conn, run["scenario_id"])  # its scenario
        net = repo.load_network(conn, cfg.network_id)  # its road network
        bundle = repo.load_replay_bundle(conn, run_id)  # its stored frames
    finally:  # cleanup
        conn.close()  # close the database
    zones = json.loads(cfg.description.split("danger_zones=", 1)[1]) if cfg.description.startswith("danger_zones=") else []  # danger zones saved as JSON in the description
    keep_clear = ([(net.nodes[d.node_id].lat, net.nodes[d.node_id].lon, 70.0) for d in cfg.destinations]  # no buildings on safe stops (part 1)
                  + [(z["lat"], z["lon"], z["radius_m"]) for z in zones]  # nor inside danger zones (part 2)
                  + [(h["lat"], h["lon"], h["blast_radius_m"]) for h in cfg.params.hazards])  # nor inside blast radii (part 3)
    if net.is_synthetic:  # fake grid
        blds = sc.synthetic_buildings(net, keep_clear)  # generate decorative blocks
    else:  # real map
        raw = []  # footprints found so far
        for m in cache.list_cached():  # each cached area
            raw = loader.load_buildings(m["lat"], m["lon"], m["radius_m"])  # its footprints
            if raw:  # found some
                break  # stop searching
        blds = sc.osm_buildings(raw)  # convert to drawable buildings
    live = lv.build_live_html(net, bundle, cfg.destinations, cfg.origins, zones, cfg.closures, blds,  # build the page (part 1)
                              cfg.params.edge_capacity_overrides, None, 900, hazards=cfg.params.hazards,  # (part 2)
                              sync_url=None, run_id=run_id)  # no phone server on a static site (part 3)
    back = ('<a href="index.html" style="position:fixed;right:12px;bottom:12px;z-index:99999;background:#2b5c9e;color:#fff;'  # a floating link (part 1)
            'font:600 13px Segoe UI,Arial,sans-serif;padding:8px 14px;border-radius:20px;text-decoration:none;'  # (part 2)
            'box-shadow:0 4px 14px rgba(0,0,0,.4)">&larr; About this project</a>')  # link back to the landing page
    live = live.replace("<body>", "<body>" + back, 1) if "<body>" in live else live  # inject it right after <body>
    out.mkdir(parents=True, exist_ok=True)  # make sure the output folder exists
    (out / "live.html").write_text(live, encoding="utf-8")  # the live replay page
    (out / "index.html").write_text(INDEX_TEMPLATE.read_text(encoding="utf-8"), encoding="utf-8")  # the landing page
    (out / ".nojekyll").write_text("", encoding="utf-8")  # tell GitHub Pages not to process the files
    for n in ("index.html", "live.html"):  # report the sizes
        print(n, round((out / n).stat().st_size / 1e6, 2), "MB")  # file name and size


if __name__ == "__main__":  # run only when executed as a script
    ap = argparse.ArgumentParser()  # option parser
    ap.add_argument("--run", type=int, required=True)  # which stored run to publish
    ap.add_argument("--out", type=Path, default=Path("."))  # output folder
    a = ap.parse_args()  # read the options
    build(a.run, a.out)  # build the site
