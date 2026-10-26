"""Build the static GitHub Pages site (index.html, live.html, phone.html, hero.jpg) from a stored run in the SQLite database.

    python -m scripts.build_static_site --run 11 --out .

The live replay is the same self-contained HTML the dashboard uses, with the Python phone server (state sync) switched off.
hero.jpg is a screenshot of the replay that you capture yourself and drop next to index.html.
The results section is generated from scripts/results/*.json, written by ``python -m scripts.compare_strategies``.
"""
from __future__ import annotations  # modern type hints on older Python

import argparse  # command-line options
import base64  # embed the QR code image in the page
import io  # in-memory buffer for the QR code
import json  # read danger zones stored as JSON text
from pathlib import Path  # file paths

from database import repositories as repo  # load runs and scenarios
from database.connection import connect  # open the database
from geo import network_cache as cache  # list cached map areas
from geo import network_loader as loader  # load building footprints
from visualization import live_view as lv  # builds the live page
from visualization import scene3d as sc  # building decoration helpers

INDEX_TEMPLATE = Path(__file__).with_name("site_index.html")  # the landing page lives in its own file
RESULTS_DIR = Path(__file__).with_name("results")  # JSON written by scripts.compare_strategies
SITE_URL = "https://ash19123.github.io/panic-engine/"  # where the site is published
STRATS = [("SHORTEST_PATH", "Shortest path"), ("CONGESTION_AWARE", "Congestion-aware"), ("LOAD_BALANCED", "Load-balanced")]  # same order everywhere


def load_result(name: str):  # one results file, or None if it has not been generated
    p = RESULTS_DIR / name  # its path
    return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else None  # parsed JSON


def pm(m: dict, unit: str = "%") -> str:  # "mean ± sd" with the same precision everywhere
    return f"{m['mean']:.1f}{unit} &plusmn; {m['sd']:.1f}"  # one decimal for every number


def qr_data_uri(url: str) -> str:  # a QR code for the phone page, as an inline image
    import segno  # QR code generator
    buf = io.BytesIO()  # buffer for the PNG
    segno.make(url, error="m").save(buf, kind="png", scale=6, border=2)  # draw it
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")  # inline image address


def results_html() -> str:  # the "What we saw" section, generated from the stored comparison runs
    bomb = {L: load_result(f"campus_bomb_lead{L}.json") for L in (60, 180, 300, 400)}  # bomb drill, four warning lead times
    stress = {"Synthetic grid": load_result("grid_stress.json"), "Campus map": load_result("campus_stress.json")}  # no-bomb crowd stress
    main = bomb.get(180)  # the headline scenario
    if not main:  # nothing generated yet
        return '<section id="results"><div class="wrap"><h2>What we saw</h2><p class="sub">Run <code>python -m scripts.compare_strategies</code> to generate results.</p></div></section>'
    n_seeds, agents = len(main["seeds"]), main["agents"]  # sample size
    h = ['<section id="results"><div class="wrap"><h2>What we saw</h2>',
         f'<p class="sub">All three routing strategies were run on identical crowds: the same {agents} people, the same start points and the same random seed. '
         f'Every figure is the mean &plusmn; standard deviation over {n_seeds} different random crowds.</p>',
         f'<h3 class="rh">Bomb drill on the campus map (alert {int(main["lead_s"])} s before the strike)</h3>',
         '<table class="rt"><tr><th>Routing</th><th>Reached safety</th><th>Caught in blast (simulated deaths)</th><th>Median time to safety</th></tr>']
    for key, label in STRATS:  # one row per strategy, same columns and precision
        m = main["strategies"][key]  # its summary
        med = f"{m['median_s']['mean']:.0f} s &plusmn; {m['median_s']['sd']:.0f}" if m["median_s"] else "n/a"  # median walking time
        h.append(f'<tr><td><b>{label}</b></td><td class="n">{pm(m["evacuated_pct"])}</td><td class="n bad">{pm(m["caught_pct"])} ({m["caught"]["mean"]:.0f} of {agents})</td><td class="n">{med}</td></tr>')
    h.append("</table>")
    h.append('<h3 class="rh">Deaths against warning time</h3>')
    h.append('<p class="sub" style="margin-top:0">People caught inside the blast radius at the strike, by how early the alert was sent.</p>')
    h.append('<table class="rt"><tr><th>Routing</th>' + "".join(f'<th>{L} s warning</th>' for L in (60, 180, 300, 400)) + "</tr>")
    for key, label in STRATS:  # one row per strategy, one column per lead time
        cells = "".join(f'<td class="n">{pm(bomb[L]["strategies"][key]["caught_pct"])}</td>' if bomb.get(L) else "<td>n/a</td>" for L in (60, 180, 300, 400))
        h.append(f"<tr><td><b>{label}</b></td>{cells}</tr>")
    h.append("</table>")
    h.append('<h3 class="rh">Crowd stress test (no bomb, everyone leaves at once)</h3>')
    h.append('<p class="sub" style="margin-top:0">Share of people who reached a safe stop within 30 minutes.</p>')
    h.append('<table class="rt"><tr><th>Routing</th>' + "".join(f"<th>{k}</th>" for k, v in stress.items() if v) + "</tr>")
    for key, label in STRATS:  # one row per strategy, one column per map
        cells = "".join(f'<td class="n">{pm(v["strategies"][key]["evacuated_pct"])}</td>' for v in stress.values() if v)
        h.append(f"<tr><td><b>{label}</b></td>{cells}</tr>")
    h.append("</table>")
    sp, ca = main["strategies"]["SHORTEST_PATH"]["caught_pct"]["mean"], main["strategies"]["CONGESTION_AWARE"]["caught_pct"]["mean"]  # headline difference between strategies
    gap = max(abs(bomb[L]["strategies"][k]["caught_pct"]["mean"] - bomb[L]["strategies"]["SHORTEST_PATH"]["caught_pct"]["mean"]) for L in (60, 180, 300, 400) if bomb.get(L) for k, _ in STRATS)  # largest gap between strategies
    c60 = bomb[60]["strategies"]["SHORTEST_PATH"]["caught_pct"]["mean"] if bomb.get(60) else None  # toll with the shortest warning
    g, c = stress["Synthetic grid"], stress["Campus map"]  # stress-test results
    h.append('<div class="panel" style="margin-top:22px"><h3>What this tells us</h3><ul class="tick">'
             + (f'<li><b>Warning time decides the death toll.</b> With a 60 s warning about {c60:.0f}% of the crowd is caught; with 300 s or more, almost nobody is.</li>' if c60 is not None else "")
             + f'<li><b>In the bomb drill the routing rule barely matters.</b> The three strategies differ by at most {gap:.1f} percentage points, inside the run-to-run spread, because the people at risk simply need to walk out of the blast area.</li>'
             + ('<li><b>Routing matters when the crowd itself is the problem.</b> In the stress test shortest path got '
                f'{c["strategies"]["SHORTEST_PATH"]["evacuated_pct"]["mean"]:.0f}% of people to safety on the campus map and {g["strategies"]["SHORTEST_PATH"]["evacuated_pct"]["mean"]:.0f}% on the grid, against '
                f'{c["strategies"]["CONGESTION_AWARE"]["evacuated_pct"]["mean"]:.0f}% and {g["strategies"]["CONGESTION_AWARE"]["evacuated_pct"]["mean"]:.0f}% for congestion-aware routing.</li>' if g and c else "")
             + '</ul></div>')
    h.append('<p class="cap" style="text-align:left;margin-top:18px">These come from an educational model with assumed walking speeds, capacities and reaction delays. They show how the strategies compare '
             'inside the model, not what would happen in a real emergency. Reproduce them with <code>python -m scripts.compare_strategies</code>.</p>')
    h.append("</div></section>")
    return "".join(h)  # the section


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
    phone = lv.build_phone_html(net, bundle, cfg.destinations, cfg.params.hazards, cfg.closures, run_id, sync_url=None)  # mobile page, no dashboard to sync with
    out.mkdir(parents=True, exist_ok=True)  # make sure the output folder exists
    (out / "live.html").write_text(live, encoding="utf-8")  # the live replay page
    (out / "phone.html").write_text(phone, encoding="utf-8")  # the phone page
    index = INDEX_TEMPLATE.read_text(encoding="utf-8").replace("<!--RESULTS-->", results_html()).replace("__PHONE_QR__", qr_data_uri(SITE_URL + "phone.html"))  # fill in results and QR code
    (out / "index.html").write_text(index, encoding="utf-8")  # the landing page
    (out / ".nojekyll").write_text("", encoding="utf-8")  # tell GitHub Pages not to process the files
    for n in ("index.html", "live.html", "phone.html"):  # report the sizes
        print(n, round((out / n).stat().st_size / 1e6, 2), "MB")  # file name and size


if __name__ == "__main__":  # run only when executed as a script
    ap = argparse.ArgumentParser()  # option parser
    ap.add_argument("--run", type=int, required=True)  # which stored run to publish
    ap.add_argument("--out", type=Path, default=Path("."))  # output folder
    a = ap.parse_args()  # read the options
    build(a.run, a.out)  # build the site
