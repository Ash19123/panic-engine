"""Central configuration for PANIC ENGINE.

Values can be overridden through environment variables (or a ``.env`` file in
the project root, see ``.env.example``). No secret keys are required anywhere.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    """Tiny .env reader so python-dotenv is not a hard dependency."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv(BASE_DIR / ".env")

DATA_DIR = Path(os.environ.get("PANIC_DATA_DIR", BASE_DIR / "data")).resolve()
DB_PATH = Path(os.environ.get("PANIC_DB_PATH", DATA_DIR / "panic_engine.db")).resolve()
CACHE_DIR = Path(os.environ.get("PANIC_CACHE_DIR", DATA_DIR / "cache")).resolve()

# --- Study area ---------------------------------------------------------
# Initial map-centre ESTIMATE for Amrita Vishwa Vidyapeetham, Amaravati.
# It must be checked against map data before it is trusted.
STUDY_AREA_NAME = "Amrita Vishwa Vidyapeetham, Amaravati - Kuragallu/Nowluru - Mangalagiri (AP, India)"
CENTER_LAT = float(os.environ.get("PANIC_CENTER_LAT", 16.45605))
CENTER_LON = float(os.environ.get("PANIC_CENTER_LON", 80.53816))
DEFAULT_RADIUS_M = int(os.environ.get("PANIC_RADIUS_M", 6000))
MAX_RADIUS_M = 8000

# --- Network / movement defaults ---------------------------------------
NETWORK_TYPE = "walk"
WALKING_SPEED_MPS = 1.4
OSM_ATTRIBUTION = "Map data (c) OpenStreetMap contributors, ODbL 1.0 - https://www.openstreetmap.org/copyright"
NOMINATIM_USER_AGENT = os.environ.get("PANIC_USER_AGENT", "panic-engine-college-project/1.0")

ROUTING_STRATEGIES = ("SHORTEST_PATH", "CONGESTION_AWARE", "LOAD_BALANCED")
ENGINE_VERSION = "1.0.0"

logger = logging.getLogger("panic_engine")


def ensure_directories() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)


def configure_logging(level: int = logging.INFO) -> None:
    if logging.getLogger().handlers:
        return
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
