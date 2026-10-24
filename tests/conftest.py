import asyncio
import os
import threading
import tempfile

import psycopg
import pytest

from saku.db import init_schema
from saku.repo import Repo
from saku.parser_service import ParserService


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


@pytest.fixture(scope="session")
def parser_socket(tmp_path_factory):
    root = tmp_path_factory.mktemp("saku-parser")
    work_root = root / "work"
    work_root.mkdir()
    socket_path = root / "parser.sock"
    loop = asyncio.new_event_loop()
    service = ParserService(socket_path, work_root=work_root, max_concurrent=1)
    ready = threading.Event()
    failure = []

    def run():
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(service.start())
            ready.set()
            loop.run_forever()
        except BaseException as exc:
            failure.append(exc)
            ready.set()
        finally:
            if not loop.is_closed():
                loop.run_until_complete(service.close())
                loop.close()

    thread = threading.Thread(target=run, name="test-parser-service", daemon=True)
    thread.start()
    assert ready.wait(5), "test parser service did not start"
    assert not failure, f"test parser service failed to start: {failure!r}"
    yield socket_path
    asyncio.run_coroutine_threadsafe(service.close(), loop).result(timeout=5)
    loop.call_soon_threadsafe(loop.stop)
    thread.join(timeout=5)
    assert not thread.is_alive(), "test parser service did not stop"
