"""Download and cache an OpenStreetMap walking network (explicit, recoverable, never automatic).

    python -m scripts.download_network --radius 1500
    python -m scripts.download_network --lat 16.45605 --lon 80.53816 --radius 2500 --store

Requires the optional dependency OSMnx and internet access. On failure nothing is
half-written: the GraphML cache is only created after a successful download.
"""
import argparse
import sys

from config import settings
from geo import network_cache as cache
from geo import network_loader as loader


def main() -> int:
    settings.configure_logging()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--lat", type=float, default=settings.CENTER_LAT)
    ap.add_argument("--lon", type=float, default=settings.CENTER_LON)
    ap.add_argument("--radius", type=int, default=settings.DEFAULT_RADIUS_M, help="metres")
    ap.add_argument("--type", default=settings.NETWORK_TYPE, help="OSMnx network type (walk, drive, all)")
    ap.add_argument("--force", action="store_true", help="download again even if cached")
    ap.add_argument("--store", action="store_true", help="also save the network into the SQLite database")
    args = ap.parse_args()

    key = cache.find_cached(args.lat, args.lon, args.radius, args.type)
    if key and not args.force:
        print(f"Already cached (key {key}); use --force to download again.")
    else:
        try:
            key = loader.download_osm_network(args.lat, args.lon, args.radius, args.type, status=print)
        except loader.NetworkLoadError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
    net = loader.load_cached_network(key)
    print(f"Cache {key}: {len(net.nodes)} nodes, {len(net.edges)} edges "
          f"({net.metadata.get('dropped_disconnected_nodes', 0)} disconnected nodes dropped).")
    print(settings.OSM_ATTRIBUTION)
    if args.store:
        from database import repositories as repo
        from database.init_db import init_db
        conn = init_db()
        try:
            saved = repo.save_network(conn, net)
            print(f"Stored in database as network {saved.network_id}.")
        finally:
            conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
