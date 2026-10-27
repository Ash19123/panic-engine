"""Look & feel plus small shared Streamlit helpers (no business logic)."""
from __future__ import annotations  # modern type hints on older Python

from contextlib import contextmanager  # turns a generator into a "with" block
from typing import Iterator  # type hint

import streamlit as st  # the dashboard framework

from config import settings  # database path and attribution
from database.connection import connect  # open the database
from database.init_db import init_db  # create or migrate the database

PALETTE = {  # colours used by the charts
    "ink": "#000000", "panel": "#0b0b0b", "line": "#2b2b2b", "text": "#f2f2f2", "muted": "#9a9a9a",  # background and text colours
    "accent": "#ffffff", "ok": "#e6e6e6", "warn": "#bdbdbd", "bad": "#ffffff", "info": "#cfcfcf",  # accent and status colours
}
# Agent states: colour AND a label are used so colour is never the only cue.
STATE_STYLE = {  # how each agent state is shown
    "WAITING": {"color": "#8b97a8", "label": "Waiting (not departed)", "glyph": "o"},  # grey
    "MOVING": {"color": "#1ec850", "label": "Moving", "glyph": ">"},  # green
    "QUEUED": {"color": "#ffaa00", "label": "Queued / held", "glyph": "||"},  # amber
    "EVACUATED": {"color": "#009624", "label": "Evacuated", "glyph": "+"},  # dark green
    "UNABLE_TO_EVACUATE": {"color": "#d62020", "label": "Unable to evacuate", "glyph": "x"},  # red
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
.stButton button[kind="primary"] { background: #fff; color: #000; }
.stButton button[kind="primary"]:hover { background: #d9d9d9; color: #000; }
.stButton button[kind="primary"] p, .stButton button[kind="primary"] span { color: #000 !important; }  /* keep the label dark on the white button, also when the button has a tooltip */
div[data-baseweb="select"] > div, input, textarea { background: #0b0b0b !important; color: #fff !important; border-color: #2b2b2b !important; }
span[data-baseweb="tag"] { background:#2b2b2b !important; border:1px solid #777; }
span[data-baseweb="tag"] * { color:#fff !important; }
span[data-tag] { background:#2b2b2b !important; border:1px solid #777 !important; }  /* newer Streamlit marks selected-value chips with data-tag */
span[data-tag], span[data-tag] * { color:#fff !important; }
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
.block-container { padding-top: 3.8rem; max-width: 1500px; }
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
"""  # the black-and-white theme as one CSS block that is injected into the page

DISCLAIMER = ("Educational simulation and preliminary decision-support prototype. Results depend entirely on the "  # text (1)
              "stated model assumptions; they are NOT validated predictions of real crowd behaviour and must not "  # text (2)
              "be used for real emergency planning.")  # text (3)


def _stretch() -> dict:  # which keyword makes widgets fill their container
    """Keyword that makes a widget fill its container, valid for the installed Streamlit.

    ``use_container_width=True`` is deprecated (and removed in newer releases); ``width="stretch"`` replaces it.
    """
    try:  # the version string may be unusual
        major, minor = (int(x) for x in st.__version__.split(".")[:2])  # first two numbers of the version
    except ValueError:                                  # unusual version string: assume a recent release
        return {"width": "stretch"}  # the new keyword
    return {"width": "stretch"} if (major, minor) >= (1, 52) else {"use_container_width": True}  # new keyword from 1.52, old one before


STRETCH = _stretch()  # computed once at import time


def setup_page(title: str, subtitle: str = "", icon: str = "🚨", header: bool = True) -> None:  # common page setup
    st.set_page_config(page_title=f"PANIC ENGINE - {title}", page_icon=icon, layout="wide")  # browser tab title and wide layout
    st.markdown(CSS, unsafe_allow_html=True)  # apply the theme
    if header:  # show a title
        st.title(title)  # big title
        if subtitle:  # and a caption
            st.caption(subtitle)  # small text under it
    ensure_db()  # make sure the database exists


@st.cache_resource(show_spinner=False)
def _init_once(path: str) -> bool:  # create or migrate the database once per process
    init_db(path).close()  # run the migrations and close the connection
    return True  # cached result


def ensure_db() -> None:  # make sure the database is ready
    _init_once(str(settings.DB_PATH))  # cached, so it runs only once


@contextmanager
def db() -> Iterator:  # "with db() as conn:"
    """Short-lived connection per page run (SQLite connections are not shared across Streamlit threads)."""
    conn = connect()  # open
    try:  # run the caller's block
        yield conn  # hand the connection over
    finally:  # always
        conn.close()  # close it


def source_badge(is_synthetic: bool) -> None:  # label showing where the map came from
    if is_synthetic:  # fake grid
        st.markdown('<span class="pe-badge pe-synth">SYNTHETIC NETWORK - not real roads</span>', unsafe_allow_html=True)  # warning badge
    else:  # real map
        st.markdown('<span class="pe-badge pe-real">REAL OpenStreetMap DATA</span>', unsafe_allow_html=True)  # real-data badge
        st.caption(settings.OSM_ATTRIBUTION)  # required credit


def note(text: str, warn: bool = False) -> None:  # a highlighted note box
    cls = "pe-note pe-disclaimer" if warn else "pe-note"  # style class
    st.markdown(f'<div class="{cls}">{text}</div>', unsafe_allow_html=True)  # show it


def disclaimer() -> None:  # the standard honesty disclaimer
    note(DISCLAIMER, warn=True)  # as a warning-style note


def fmt_pct(x) -> str:  # format a fraction as a percentage
    return "n/a" if x is None or x != x else f"{100 * x:.1f}%"  # n/a for missing or NaN


def fmt_num(x, unit: str = "") -> str:  # format a number with thousands separators
    return "n/a" if x is None or x != x else f"{x:,.1f}{unit}"  # n/a for missing or NaN


@st.cache_resource(show_spinner=False)
def _load_network_cached(network_id: str):  # load a network once and keep it in memory
    from database import repositories as repo  # imported here to avoid a circular import
    with db() as conn:  # short-lived connection
        return repo.load_network(conn, network_id)  # the network


def current_network(conn):  # the network chosen in the UI
    """Network chosen on the Study Area page (or the newest one in the database)."""
    from database import repositories as repo  # imported here to avoid a circular import
    nets = repo.list_networks(conn)  # all stored networks
    if not nets:  # none yet
        return None  # nothing to return
    ids = [n["network_id"] for n in nets]  # their ids
    nid = st.session_state.get("network_id")  # the one chosen earlier
    if nid not in ids:  # none chosen or no longer present
        nid = ids[0]  # use the newest
        st.session_state["network_id"] = nid  # remember it
    return _load_network_cached(nid)  # the network


def ensure_demo_button(conn) -> bool:  # offer demo data when the database is empty
    """Offer one-click synthetic demo data when the database has no network yet."""
    from database import repositories as repo  # imported here to avoid a circular import
    if repo.list_networks(conn):  # data exists
        return True  # nothing to offer
    st.info("The database has no road network yet. Use the synthetic demonstration (no internet needed) "  # message (1)
            "or download OpenStreetMap data on the Study Area page.")  # message (2)
    if st.button("Create synthetic demo network + demo scenario"):  # the user clicks
        from database.seed_data import seed  # imported only when needed
        seed(conn)  # create the demo data
        st.rerun()  # redraw the page
    return False  # still no data
