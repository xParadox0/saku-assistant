import os
import tempfile

import psycopg
import pytest

from saku.db import init_schema
from saku.repo import Repo


@pytest.fixture(scope="session")
def db_url():
    """Use TEST_DATABASE_URL when set (CI), otherwise start a throwaway local PostgreSQL via pgserver."""
    url = os.environ.get("TEST_DATABASE_URL")
    if url:
        yield url
        return
    pgserver = pytest.importorskip("pgserver")
    srv = pgserver.get_server(tempfile.mkdtemp(prefix="saku-pg-"), cleanup_mode="delete")
    yield srv.get_uri()


@pytest.fixture
def conn(db_url):
    with psycopg.connect(db_url, autocommit=True) as c:
        c.execute("DROP SCHEMA IF EXISTS public CASCADE")
        c.execute("CREATE SCHEMA public")
        init_schema(c)
        yield c


@pytest.fixture
def repo(conn):
    return Repo(conn)
