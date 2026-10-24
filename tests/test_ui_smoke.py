"""Headless smoke test: every Streamlit page must render without raising (uses a temp database)."""
import importlib
from pathlib import Path

import pytest

st_testing = pytest.importorskip("streamlit.testing.v1")
ROOT = Path(__file__).resolve().parent.parent
PAGES = ["app.py"]


@pytest.fixture
def seeded_db(tmp_path, monkeypatch):
    from config import settings
    monkeypatch.setattr(settings, "DB_PATH", tmp_path / "ui.db")
    from database.init_db import init_db
    from database.seed_data import seed
    conn = init_db(settings.DB_PATH)
    seed(conn, run_demo=True)
    conn.close()
    from visualization import theme
    theme._init_once.clear()
    theme._load_network_cached.clear()
    return settings.DB_PATH


@pytest.mark.parametrize("page", PAGES)
def test_page_renders_with_data(page, seeded_db):
    at = st_testing.AppTest.from_file(str(ROOT / page), default_timeout=60).run()
    assert not at.exception, [e.value for e in at.exception]


@pytest.mark.parametrize("page", PAGES)
def test_page_renders_on_empty_database(page, tmp_path, monkeypatch):
    from config import settings
    monkeypatch.setattr(settings, "DB_PATH", tmp_path / "empty.db")
    from visualization import theme
    theme._init_once.clear()
    theme._load_network_cached.clear()
    at = st_testing.AppTest.from_file(str(ROOT / page), default_timeout=60).run()
    assert not at.exception, [e.value for e in at.exception]


def test_run_button_creates_a_run_with_a_danger_zone(seeded_db):
    from database.connection import connect
    at = st_testing.AppTest.from_file(str(ROOT / "app.py"), default_timeout=120).run()
    [s for s in at.sidebar.slider if s.label == "Danger zones (roads closed)"][0].set_value(1)   # one danger zone
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    run_btn = [b for b in at.sidebar.button if "RUN SIMULATION" in b.label][0]
    run_btn.click()
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    conn = connect(seeded_db)
    try:
        n = conn.execute("SELECT COUNT(*) FROM simulation_runs WHERE label='live' AND status='COMPLETED'").fetchone()[0]
        closures = conn.execute("SELECT COUNT(*) FROM scenario_closures").fetchone()[0]
    finally:
        conn.close()
    assert n == 1 and closures > 0


def test_bomb_area_run_sends_alerts_and_strikes(seeded_db):
    from database.connection import connect
    at = st_testing.AppTest.from_file(str(ROOT / "app.py"), default_timeout=180).run()
    [s for s in at.sidebar.slider if s.label == "Bomb areas"][0].set_value(1)
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    [b for b in at.sidebar.button if "RUN SIMULATION" in b.label][0].click()
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    conn = connect(seeded_db)
    try:
        alerts = conn.execute("SELECT COUNT(*) FROM simulation_events WHERE event_type='ALERT_SENT'").fetchone()[0]
        strikes = conn.execute("SELECT COUNT(*) FROM simulation_events WHERE event_type='HAZARD_STRIKE'").fetchone()[0]
    finally:
        conn.close()
    assert alerts > 0 and strikes == 1


def test_one_click_demo_button(seeded_db):
    from database.connection import connect
    at = st_testing.AppTest.from_file(str(ROOT / "app.py"), default_timeout=180).run()
    [b for b in at.button if "BOMB-ALERT DEMO" in b.label][0].click()
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    conn = connect(seeded_db)
    try:
        n = conn.execute("SELECT COUNT(*) FROM simulation_runs WHERE label='live' AND status='COMPLETED'").fetchone()[0]
        strikes = conn.execute("SELECT COUNT(*) FROM simulation_events WHERE event_type='HAZARD_STRIKE'").fetchone()[0]
    finally:
        conn.close()
    assert n == 1 and strikes == 1


def test_alert_channels_from_the_sidebar_leave_some_people_unwarned(seeded_db):
    from database.connection import connect
    at = st_testing.AppTest.from_file(str(ROOT / "app.py"), default_timeout=180).run()
    [s for s in at.sidebar.slider if s.label == "Bomb areas"][0].set_value(1)
    at.run()
    [s for s in at.sidebar.slider if s.label == "People with a phone at hand (%)"][0].set_value(30)
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    [b for b in at.sidebar.button if "RUN SIMULATION" in b.label][0].click()
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    conn = connect(seeded_db)
    try:
        rows = dict(conn.execute("SELECT COALESCE(alert_channel,'-'), COUNT(*) FROM agents GROUP BY 1").fetchall())
    finally:
        conn.close()
    assert rows.get("NONE", 0) > 0 and rows.get("PHONE", 0) > 0
