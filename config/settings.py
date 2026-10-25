"""Central configuration for PANIC ENGINE.

Values can be overridden through environment variables (or a ``.env`` file in
the project root, see ``.env.example``). No secret keys are required anywhere.
"""
from __future__ import annotations  # allow modern type hints on older Python versions

import logging  # standard logging module
import os  # environment variables
from pathlib import Path  # object-style file paths

BASE_DIR = Path(__file__).resolve().parent.parent  # project root: two folders above this file


def _load_dotenv(path: Path) -> None:  # read KEY=VALUE lines from a .env file into the environment
    """Tiny .env reader so python-dotenv is not a hard dependency."""
    if not path.is_file():  # no .env file: nothing to do
        return  # leave the environment unchanged
    for raw in path.read_text(encoding="utf-8").splitlines():  # go through the file line by line
        line = raw.strip()  # remove surrounding spaces
        if not line or line.startswith("#") or "=" not in line:  # skip blanks, comments and malformed lines
            continue  # go to the next line
        key, value = line.split("=", 1)  # split at the first "=" only
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))  # real environment variables win over .env


_load_dotenv(BASE_DIR / ".env")  # load the optional .env file at import time

DATA_DIR = Path(os.environ.get("PANIC_DATA_DIR", BASE_DIR / "data")).resolve()  # folder for runtime data
DB_PATH = Path(os.environ.get("PANIC_DB_PATH", DATA_DIR / "panic_engine.db")).resolve()  # the SQLite database file
CACHE_DIR = Path(os.environ.get("PANIC_CACHE_DIR", DATA_DIR / "cache")).resolve()  # downloaded map cache folder

# --- Study area ---------------------------------------------------------
# Initial map-centre ESTIMATE for Amrita Vishwa Vidyapeetham, Amaravati.
# It must be checked against map data before it is trusted.
STUDY_AREA_NAME = "Amrita Vishwa Vidyapeetham, Amaravati - Kuragallu/Nowluru - Mangalagiri (AP, India)"  # label shown in the UI
CENTER_LAT = float(os.environ.get("PANIC_CENTER_LAT", 16.45605))  # map centre latitude
CENTER_LON = float(os.environ.get("PANIC_CENTER_LON", 80.53816))  # map centre longitude
DEFAULT_RADIUS_M = int(os.environ.get("PANIC_RADIUS_M", 6000))  # default download radius in metres
MAX_RADIUS_M = 8000  # largest radius the UI allows

# --- Network / movement defaults ---------------------------------------
NETWORK_TYPE = "walk"  # OpenStreetMap network type: walkable roads
WALKING_SPEED_MPS = 1.4  # average walking speed in metres per second
OSM_ATTRIBUTION = "Map data (c) OpenStreetMap contributors, ODbL 1.0 - https://www.openstreetmap.org/copyright"  # required credit
NOMINATIM_USER_AGENT = os.environ.get("PANIC_USER_AGENT", "panic-engine-college-project/1.0")  # identifies us to the geocoder

ROUTING_STRATEGIES = ("SHORTEST_PATH", "CONGESTION_AWARE", "LOAD_BALANCED")  # the three supported routing policies
ENGINE_VERSION = "1.0.0"  # stored with every run so results can be traced to a version

logger = logging.getLogger("panic_engine")  # shared logger for the project


def ensure_directories() -> None:  # make sure the data and cache folders exist
    DATA_DIR.mkdir(parents=True, exist_ok=True)  # create the data folder if missing
    CACHE_DIR.mkdir(parents=True, exist_ok=True)  # create the cache folder if missing


def configure_logging(level: int = logging.INFO) -> None:  # set up console logging once
    if logging.getLogger().handlers:  # something already configured logging
        return  # do not configure it twice
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")  # timestamped log lines
