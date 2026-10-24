"""Look & feel plus small shared Streamlit helpers (no business logic)."""
from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

import streamlit as st

from config import settings
from database.connection import connect
from database.init_db import init_db

PALETTE = {
    "ink": "#000000", "panel": "#0b0b0b", "line": "#2b2b2b", "text": "#f2f2f2", "muted": "#9a9a9a",
    "accent": "#ffffff", "ok": "#e6e6e6", "warn": "#bdbdbd", "bad": "#ffffff", "info": "#cfcfcf",
}
# Agent states: colour AND a label are used so colour is never the only cue.
STATE_STYLE = {
    "WAITING": {"color": "#8b97a8", "label": "Waiting (not departed)", "glyph": "o"},
    "MOVING": {"color": "#1ec850", "label": "Moving", "glyph": ">"},
    "QUEUED": {"color": "#ffaa00", "label": "Queued / held", "glyph": "||"},
    "EVACUATED": {"color": "#009624", "label": "Evacuated", "glyph": "+"},
    "UNABLE_TO_EVACUATE": {"color": "#d62020", "label": "Unable to evacuate", "glyph": "x"},
}

CSS = """
<style>
.stApp { background: #000; color: #f2f2f2; }
header[data-testid="stHeader"] { background: #000; }
section[data-testid="stSidebar"] { background: #050505; border-right: 1px solid #2b2b2b; }
section[data-testid="stSidebar"] * { color: #f2f2f2; }
h1, h2, h3 { letter-spacing: .04em; font-weight: 700; color: #fff; }
h1 { border-bottom: 2px solid #fff; padding-bottom: .3rem; }
div[data-testid="stMetric"] { background: #0b0b0b; border: 1px solid #2b2b2b; border-radius: 4px; padding: .6rem .8rem; }
div[data-testid="stMetricValue"] { color: #fff; }
div[data-testid="stMetricLabel"] p { color: #9a9a9a; text-transform: uppercase; letter-spacing: .06em; font-size: .7rem; }
.stButton > button { background: #000; color: #fff; border: 1px solid #fff; border-radius: 2px; font-weight: 600; }
.stButton > button:hover { background: #fff; color: #000; border-color: #fff; }
.stButton > button:disabled { border-color: #444; color: #555; background: #000; }
.stButton > button[kind="primary"] { background: #fff; color: #000; }
.stButton > button[kind="primary"]:hover { background: #d9d9d9; color: #000; }
div[data-baseweb="select"] > div, input, textarea { background: #0b0b0b !important; color: #fff !important; border-color: #2b2b2b !important; }
span[data-baseweb="tag"] { background:#2b2b2b !important; border:1px solid #777; }
span[data-baseweb="tag"] * { color:#fff !important; }
div[data-testid="stDeckGlJsonChart"], div[data-testid="stDeckGlJsonChart"] > div { background:#000 !important; }
div[data-testid="stDeckGlJsonChart"] canvas.mapboxgl-canvas { visibility:hidden !important; }
div[data-testid="stDeckGlJsonChart"] .mapboxgl-ctrl-attrib, div[data-testid="stDeckGlJsonChart"] .mapboxgl-ctrl-logo { display:none !important; }
.pe-badge { display:inline-block; padding:.15rem .6rem; border:1px solid #fff; font-weight:700; font-size:.75rem;
    letter-spacing:.08em; margin-right:.4rem; text-transform:uppercase; }
.pe-real { background:#fff; color:#000; }
.pe-synth { background:#000; color:#fff; border-style:dashed; }
.pe-note { background:#0b0b0b; border-left:3px solid #fff; padding:.5rem .8rem; color:#9a9a9a; font-size:.85rem; }
.pe-disclaimer { border-left-color:#777; }
.pe-legend span { margin-right: 1.1rem; font-size:.85rem; color:#ddd; }
.pe-sw { display:inline-block; width:.8rem; height:.8rem; margin-right:.35rem; vertical-align:-1px; border:1px solid #fff; }
.block-container { padding-top: 1.2rem; max-width: 1500px; }
.pe-top { display:flex; justify-content:space-between; align-items:flex-end; gap:1rem; flex-wrap:wrap;
  border-bottom:2px solid #fff; padding-bottom:.7rem; margin-bottom:.8rem; }
.pe-brand { font-size:2.1rem; font-weight:900; letter-spacing:.14em; line-height:1; color:#fff; }
.pe-brand i { color:#ff3b3b; font-style:normal; }
.pe-tag { color:#9a9a9a; font-size:.82rem; letter-spacing:.04em; margin-top:.35rem; }
.pe-chips { display:flex; gap:.4rem; flex-wrap:wrap; }
.pe-chip { border:1px solid #555; padding:.18rem .6rem; font-size:.7rem; font-weight:700; letter-spacing:.09em; text-transform:uppercase; color:#ddd; }
.pe-chip.red { border-color:#ff3b3b; color:#ff6a6a; } .pe-chip.green { border-color:#1ec850; color:#4ee37b; }
.pe-chip.amber { border-color:#ffb000; color:#ffb000; } .pe-chip.white { background:#fff; color:#000; border-color:#fff; }
.stTabs [data-baseweb="tab-list"] { gap:4px; border-bottom:1px solid #2b2b2b; }
.stTabs [data-baseweb="tab"] { background:#0b0b0b; border:1px solid #2b2b2b; border-bottom:none; padding:.5rem 1.1rem;
  font-weight:800; letter-spacing:.05em; color:#9a9a9a; }
.stTabs [aria-selected="true"] { background:#fff !important; color:#000 !important; }
.stTabs [data-baseweb="tab-highlight"], .stTabs [data-baseweb="tab-border"] { display:none; }
.pe-step { background:#0b0b0b; border:1px solid #2b2b2b; padding:.7rem .8rem; height:100%; min-height:150px; }
.pe-step b { display:inline-block; background:#fff; color:#000; width:1.5rem; height:1.5rem; text-align:center; line-height:1.5rem; }
.pe-step h5 { margin:.5rem 0 .25rem; letter-spacing:.08em; text-transform:uppercase; font-size:.8rem; color:#fff; }
.pe-step p { margin:0; color:#9a9a9a; font-size:.78rem; line-height:1.35; }
.pe-cta { border:1px solid #fff; padding:.8rem 1rem; margin:.3rem 0 .8rem; background:#0b0b0b; }
.pe-cta b { letter-spacing:.06em; } .pe-cta span { color:#9a9a9a; font-size:.85rem; }
section[data-testid="stSidebar"] h3 { font-size:.8rem; letter-spacing:.16em; text-transform:uppercase; color:#9a9a9a;
  border-top:1px solid #2b2b2b; padding-top:.7rem; margin-top:.6rem; }
section[data-testid="stSidebar"] details { border:1px solid #2b2b2b; background:#070707; }
</style>
"""

DISCLAIMER = ("Educational simulation and preliminary decision-support prototype. Results depend entirely on the "
              "stated model assumptions; they are NOT validated predictions of real crowd behaviour and must not "
              "be used for real emergency planning.")


def _stretch() -> dict:
    """Keyword that makes a widget fill its container, valid for the installed Streamlit.

    ``use_container_width=True`` is deprecated (and removed in newer releases); ``width="stretch"`` replaces it.
    """
    try:
        major, minor = (int(x) for x in st.__version__.split(".")[:2])
    except ValueError:                                  # unusual version string: assume a recent release
        return {"width": "stretch"}
    return {"width": "stretch"} if (major, minor) >= (1, 52) else {"use_container_width": True}


STRETCH = _stretch()


def setup_page(title: str, subtitle: str = "", icon: str = "🚨", header: bool = True) -> None:
    st.set_page_config(page_title=f"PANIC ENGINE - {title}", page_icon=icon, layout="wide")
    st.markdown(CSS, unsafe_allow_html=True)
    if header:
        st.title(title)
        if subtitle:
            st.caption(subtitle)
    ensure_db()


@st.cache_resource(show_spinner=False)
def _init_once(path: str) -> bool:
    init_db(path).close()
    return True


def ensure_db() -> None:
    _init_once(str(settings.DB_PATH))


@contextmanager
def db() -> Iterator:
    """Short-lived connection per page run (SQLite connections are not shared across Streamlit threads)."""
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


def source_badge(is_synthetic: bool) -> None:
    if is_synthetic:
        st.markdown('<span class="pe-badge pe-synth">SYNTHETIC NETWORK - not real roads</span>', unsafe_allow_html=True)
    else:
        st.markdown('<span class="pe-badge pe-real">REAL OpenStreetMap DATA</span>', unsafe_allow_html=True)
        st.caption(settings.OSM_ATTRIBUTION)


def note(text: str, warn: bool = False) -> None:
    cls = "pe-note pe-disclaimer" if warn else "pe-note"
    st.markdown(f'<div class="{cls}">{text}</div>', unsafe_allow_html=True)


def disclaimer() -> None:
    note(DISCLAIMER, warn=True)


def fmt_pct(x) -> str:
    return "n/a" if x is None or x != x else f"{100 * x:.1f}%"


def fmt_num(x, unit: str = "") -> str:
    return "n/a" if x is None or x != x else f"{x:,.1f}{unit}"


@st.cache_resource(show_spinner=False)
def _load_network_cached(network_id: str):
    from database import repositories as repo
    with db() as conn:
        return repo.load_network(conn, network_id)


def current_network(conn):
    """Network chosen on the Study Area page (or the newest one in the database)."""
    from database import repositories as repo
    nets = repo.list_networks(conn)
    if not nets:
        return None
    ids = [n["network_id"] for n in nets]
    nid = st.session_state.get("network_id")
    if nid not in ids:
        nid = ids[0]
        st.session_state["network_id"] = nid
    return _load_network_cached(nid)


def ensure_demo_button(conn) -> bool:
    """Offer one-click synthetic demo data when the database has no network yet."""
    from database import repositories as repo
    if repo.list_networks(conn):
        return True
    st.info("The database has no road network yet. Use the synthetic demonstration (no internet needed) "
            "or download OpenStreetMap data on the Study Area page.")
    if st.button("Create synthetic demo network + demo scenario"):
        from database.seed_data import seed
        seed(conn)
        st.rerun()
    return False
