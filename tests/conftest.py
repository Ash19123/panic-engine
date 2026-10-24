import pytest

from database.init_db import init_db


@pytest.fixture
def conn(tmp_path):
    c = init_db(tmp_path / "test.db")
    yield c
    c.close()


@pytest.fixture
def seeded(conn):
    from database.seed_data import seed
    sid = seed(conn)
    return conn, sid
