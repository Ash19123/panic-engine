"""Download and cache an OpenStreetMap walking network (explicit, recoverable, never automatic).

    python -m scripts.download_network --radius 1500
    python -m scripts.download_network --lat 16.45605 --lon 80.53816 --radius 2500 --store

Requires the optional dependency OSMnx and internet access. On failure nothing is
half-written: the GraphML cache is only created after a successful download.
"""
import argparse  # command-line options
import sys  # exit codes and stderr

from config import settings  # defaults and attribution
from geo import network_cache as cache  # cache lookup
from geo import network_loader as loader  # download and load


def main() -> int:  # returns the process exit code
    settings.configure_logging()  # set up logging
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)  # show the docstring as help
    ap.add_argument("--lat", type=float, default=settings.CENTER_LAT)  # centre latitude
    ap.add_argument("--lon", type=float, default=settings.CENTER_LON)  # centre longitude
    ap.add_argument("--radius", type=int, default=settings.DEFAULT_RADIUS_M, help="metres")  # area radius
    ap.add_argument("--type", default=settings.NETWORK_TYPE, help="OSMnx network type (walk, drive, all)")  # kind of roads
    ap.add_argument("--force", action="store_true", help="download again even if cached")  # ignore the cache
    ap.add_argument("--store", action="store_true", help="also save the network into the SQLite database")  # save to SQL as well
    args = ap.parse_args()  # read the options

    key = cache.find_cached(args.lat, args.lon, args.radius, args.type)  # is this request cached?
    if key and not args.force:  # cached and not forced
        print(f"Already cached (key {key}); use --force to download again.")  # nothing to download
    else:  # download needed
        try:  # downloads can fail
            key = loader.download_osm_network(args.lat, args.lon, args.radius, args.type, status=print)  # fetch and cache
        except loader.NetworkLoadError as exc:  # download problem
            print(f"ERROR: {exc}", file=sys.stderr)  # explain on stderr
            return 1  # failure exit code
    net = loader.load_cached_network(key)  # read the cached network back
    print(f"Cache {key}: {len(net.nodes)} nodes, {len(net.edges)} edges "  # summary line, first half
          f"({net.metadata.get('dropped_disconnected_nodes', 0)} disconnected nodes dropped).")  # and how many nodes were dropped
    print(settings.OSM_ATTRIBUTION)  # required OpenStreetMap credit
    if args.store:  # also save into the database
        from database import repositories as repo  # imported only when needed
        from database.init_db import init_db  # imported only when needed
        conn = init_db()  # open or create the database
        try:  # always close it
            saved = repo.save_network(conn, net)  # store the network
            print(f"Stored in database as network {saved.network_id}.")  # report its id
        finally:  # cleanup
            conn.close()  # close the connection
    return 0  # success


if __name__ == "__main__":  # run only when executed as a script
    sys.exit(main())  # exit with the returned code
