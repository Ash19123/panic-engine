import pytest  # the test framework

from database.init_db import init_db  # create a database


@pytest.fixture
def conn(tmp_path):  # a fresh, empty database for each test
    c = init_db(tmp_path / "test.db")  # create it in a temporary folder
    yield c  # hand it to the test
    c.close()  # close it afterwards


@pytest.fixture
def seeded(conn):  # a database with the synthetic network and demo scenario
    from database.seed_data import seed  # imported here to keep imports light
    sid = seed(conn)  # insert the demo data
    return conn, sid  # the connection and the demo scenario id
