"""Headless smoke test: every Streamlit page must render without raising (uses a temp database)."""
import importlib  # (kept for tests that reload modules)
from pathlib import Path  # file paths

import pytest  # the test framework

st_testing = pytest.importorskip("streamlit.testing.v1")  # skip these tests if Streamlit's test helper is missing
ROOT = Path(__file__).resolve().parent.parent  # project root
PAGES = ["app.py"]  # pages to test


@pytest.fixture
def seeded_db(tmp_path, monkeypatch):  # a temporary database with demo runs
    from config import settings  # settings to patch
    monkeypatch.setattr(settings, "DB_PATH", tmp_path / "ui.db")  # point the app at a temporary file
    from database.init_db import init_db  # create the database
    from database.seed_data import seed  # insert demo data
    conn = init_db(settings.DB_PATH)  # create it
    seed(conn, run_demo=True)  # insert the demo scenario and simulate it
    conn.close()  # close it
    from visualization import theme  # cached helpers to reset
    theme._init_once.clear()  # forget that the database was initialised
    theme._load_network_cached.clear()  # forget cached networks
    return settings.DB_PATH  # the database path


@pytest.mark.parametrize("page", PAGES)
def test_page_renders_with_data(page, seeded_db):  # the page works with data
    at = st_testing.AppTest.from_file(str(ROOT / page), default_timeout=60).run()  # run the page headlessly
    assert not at.exception, [e.value for e in at.exception]  # no exception was raised


@pytest.mark.parametrize("page", PAGES)
def test_page_renders_on_empty_database(page, tmp_path, monkeypatch):  # the page works without data
    from config import settings  # settings to patch
    monkeypatch.setattr(settings, "DB_PATH", tmp_path / "empty.db")  # point the app at an empty database
    from visualization import theme  # cached helpers to reset
    theme._init_once.clear()  # forget that the database was initialised
    theme._load_network_cached.clear()  # forget cached networks
    at = st_testing.AppTest.from_file(str(ROOT / page), default_timeout=60).run()  # run the page headlessly
    assert not at.exception, [e.value for e in at.exception]  # no exception was raised


def test_run_button_creates_a_run_with_a_danger_zone(seeded_db):  # the RUN SIMULATION button
    from database.connection import connect  # open the database
    at = st_testing.AppTest.from_file(str(ROOT / "app.py"), default_timeout=120).run()  # run the page
    [s for s in at.sidebar.slider if s.label == "Danger zones (roads closed)"][0].set_value(1)   # one danger zone
    at.run()  # re-run with the new value
    assert not at.exception, [e.value for e in at.exception]  # no exception
    run_btn = [b for b in at.sidebar.button if "RUN SIMULATION" in b.label][0]  # the run button
    run_btn.click()  # press it
    at.run()  # re-run so the click takes effect
    assert not at.exception, [e.value for e in at.exception]  # no exception
    conn = connect(seeded_db)  # open the database
    try:  # always close it
        n = conn.execute("SELECT COUNT(*) FROM simulation_runs WHERE label='live' AND status='COMPLETED'").fetchone()[0]  # completed live runs
        closures = conn.execute("SELECT COUNT(*) FROM scenario_closures").fetchone()[0]  # stored road closures
    finally:  # cleanup
        conn.close()  # close the database
    assert n == 1 and closures > 0  # one run was stored and the danger zone closed some roads


def test_bomb_area_run_sends_alerts_and_strikes(seeded_db):  # a bomb run through the UI
    from database.connection import connect  # open the database
    at = st_testing.AppTest.from_file(str(ROOT / "app.py"), default_timeout=180).run()  # run the page
    [s for s in at.sidebar.slider if s.label == "Bomb areas"][0].set_value(1)  # one bomb
    at.run()  # re-run with the new value
    assert not at.exception, [e.value for e in at.exception]  # no exception
    [b for b in at.sidebar.button if "RUN SIMULATION" in b.label][0].click()  # press the run button
    at.run()  # re-run so the click takes effect
    assert not at.exception, [e.value for e in at.exception]  # no exception
    conn = connect(seeded_db)  # open the database
    try:  # always close it
        alerts = conn.execute("SELECT COUNT(*) FROM simulation_events WHERE event_type='ALERT_SENT'").fetchone()[0]  # alerts stored
        strikes = conn.execute("SELECT COUNT(*) FROM simulation_events WHERE event_type='HAZARD_STRIKE'").fetchone()[0]  # strikes stored
    finally:  # cleanup
        conn.close()  # close the database
    assert alerts > 0 and strikes == 1  # alerts were sent and the bomb struck once


def test_one_click_demo_button(seeded_db):  # the demo button
    from database.connection import connect  # open the database
    at = st_testing.AppTest.from_file(str(ROOT / "app.py"), default_timeout=180).run()  # run the page
    [b for b in at.button if "BOMB-ALERT DEMO" in b.label][0].click()  # press the demo button
    at.run()  # re-run so the click takes effect
    assert not at.exception, [e.value for e in at.exception]  # no exception
    conn = connect(seeded_db)  # open the database
    try:  # always close it
        n = conn.execute("SELECT COUNT(*) FROM simulation_runs WHERE label='live' AND status='COMPLETED'").fetchone()[0]  # completed live runs
        strikes = conn.execute("SELECT COUNT(*) FROM simulation_events WHERE event_type='HAZARD_STRIKE'").fetchone()[0]  # stored strikes
    finally:  # cleanup
        conn.close()  # close the database
    assert n == 1 and strikes == 1  # one run with one strike


def test_alert_channels_from_the_sidebar_leave_some_people_unwarned(seeded_db):  # the phone-share slider
    from database.connection import connect  # open the database
    at = st_testing.AppTest.from_file(str(ROOT / "app.py"), default_timeout=180).run()  # run the page
    [s for s in at.sidebar.slider if s.label == "Bomb areas"][0].set_value(1)  # one bomb
    at.run()  # re-run so the alert-channel controls appear
    [s for s in at.sidebar.slider if s.label == "People with a phone at hand (%)"][0].set_value(30)  # only 30% have a phone
    at.run()  # re-run with the new value
    assert not at.exception, [e.value for e in at.exception]  # no exception
    [b for b in at.sidebar.button if "RUN SIMULATION" in b.label][0].click()  # press the run button
    at.run()  # re-run so the click takes effect
    assert not at.exception, [e.value for e in at.exception]  # no exception
    conn = connect(seeded_db)  # open the database
    try:  # always close it
        rows = dict(conn.execute("SELECT COALESCE(alert_channel,'-'), COUNT(*) FROM agents GROUP BY 1").fetchall())  # people per channel
    finally:  # cleanup
        conn.close()  # close the database
    assert rows.get("NONE", 0) > 0 and rows.get("PHONE", 0) > 0  # some were unwarned and some were warned by phone
