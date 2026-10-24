import asyncio
import threading
from datetime import date
from decimal import Decimal
from pathlib import Path

from saku.normalise import RawTxn
from saku.parsers.sandbox import ParseOutcome
from saku.parsers.worker_client import ParserAdmission, ParserClient
from saku.parser_service import ParserService


ROW = RawTxn(date(2026, 9, 1), Decimal("-123.00"), "SYNTHETIC ROW", "normal")
ACCOUNT = {"provider": "bca", "last4": "6789"}


def test_worker_round_trip_returns_only_structured_parse_result(tmp_path):
    seen = []

    def parse(data, suffix, account, workdir):
        seen.append((data, suffix, account["provider"], account["last4"], Path(workdir).is_dir()))
        return ParseOutcome("bca-pdf/1", [ROW], None)

    async def scenario():
        socket_path = tmp_path / "parser.sock"
        service = ParserService(socket_path, work_root=tmp_path, parse_fn=parse)
        await service.start()
        try:
            return await ParserClient(socket_path, timeout=2).parse_upload(
                b"synthetic bytes", ".pdf", {"provider": "bca", "last4": "6789"})
        finally:
            await service.close()

    outcome = asyncio.run(scenario())
    assert outcome == ParseOutcome("bca-pdf/1", [ROW], None)
    assert seen == [(b"synthetic bytes", ".pdf", "bca", "6789", True)]
    assert sorted(p.name for p in tmp_path.iterdir()) == []


def test_worker_rejects_excess_parse_without_queueing(tmp_path):
    started = threading.Event()
    release = threading.Event()
    calls = []

    def slow_parse(data, suffix, account, workdir):
        calls.append(data)
        started.set()
        assert release.wait(3)
        return ParseOutcome("bca-pdf/1", [ROW], None)

    async def scenario():
        socket_path = tmp_path / "parser.sock"
        service = ParserService(socket_path, work_root=tmp_path, parse_fn=slow_parse, max_concurrent=1)
        await service.start()
        client = ParserClient(socket_path, timeout=2)
        first = asyncio.create_task(client.parse_upload(b"one", ".pdf", {"provider": "bca"}))
        try:
            assert await asyncio.to_thread(started.wait, 2)
            assert await client.health_check()
            second = await client.parse_upload(b"two", ".pdf", {"provider": "bca"})
            assert second.error and "busy" in second.error.lower()
            assert len(calls) == 1
            release.set()
            return await first
        finally:
            release.set()
            await service.close()

    outcome = asyncio.run(scenario())
    assert outcome.rows == [ROW]
    assert calls == [b"one"]
    assert list(tmp_path.iterdir()) == []


def test_worker_hides_parser_exception_and_releases_capacity(tmp_path):
    calls = 0

    def sometimes_fails(data, suffix, account, workdir):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("PRIVATE STATEMENT TEXT MUST NOT ESCAPE")
        return ParseOutcome("bca-pdf/1", [ROW], None)

    async def scenario():
        socket_path = tmp_path / "parser.sock"
        service = ParserService(socket_path, work_root=tmp_path, parse_fn=sometimes_fails, max_concurrent=1)
        await service.start()
        client = ParserClient(socket_path, timeout=2)
        try:
            failed = await client.parse_upload(b"first", ".pdf", {"provider": "bca"})
            recovered = await client.parse_upload(b"second", ".pdf", {"provider": "bca"})
            return failed, recovered
        finally:
            await service.close()

    failed, recovered = asyncio.run(scenario())
    assert failed.error and "PRIVATE STATEMENT TEXT" not in failed.error
    assert recovered.rows == [ROW]
    assert calls == 2
    assert sorted(p.name for p in tmp_path.iterdir()) == []


def test_client_timeout_is_a_neutral_parse_failure(tmp_path):
    async def scenario():
        socket_path = tmp_path / "parser.sock"

        async def hang(reader, writer):
            await reader.readline()
            await asyncio.sleep(0.2)
            writer.close()
            await writer.wait_closed()

        server = await asyncio.start_unix_server(hang, path=str(socket_path))
        try:
            result = await ParserClient(socket_path, timeout=0.05).parse_upload(
                b"synthetic", ".pdf", {"provider": "bca"})
            return result
        finally:
            server.close()
            await server.wait_closed()

    outcome = asyncio.run(scenario())
    assert outcome.error is not None
    assert "synthetic" not in outcome.error


def test_cancelled_client_does_not_leak_worker_tempdir_or_capacity(tmp_path):
    async def run():
        socket_path = tmp_path / "parser.sock"
        work_root = tmp_path / "work"
        started = threading.Event()
        release = threading.Event()
        finished = threading.Event()
        calls = []

        def parse(data, suffix, account, workdir):
            calls.append((data, Path(workdir).exists()))
            if len(calls) == 1:
                started.set()
                assert release.wait(2)
                finished.set()
            return ParseOutcome("bca-pdf/1", [ROW], None)

        service = ParserService(socket_path, work_root, parse_fn=parse, max_concurrent=1)
        await service.start()
        client = ParserClient(socket_path, timeout=2)
        try:
            pending = asyncio.create_task(client.parse_upload(b"slow", ".pdf", ACCOUNT))
            assert await asyncio.to_thread(started.wait, 2)
            pending.cancel()
            try:
                await pending
            except asyncio.CancelledError:
                pass
            release.set()
            assert await asyncio.to_thread(finished.wait, 2)
            for _ in range(50):
                if not list(work_root.iterdir()):
                    break
                await asyncio.sleep(0.01)
            assert not list(work_root.iterdir())
            next_result = await ParserClient(socket_path, timeout=2).parse_upload(b"next", ".pdf", ACCOUNT)
            assert next_result == ParseOutcome("bca-pdf/1", [ROW], None)
            assert len(calls) == 2
        finally:
            release.set()
            await service.close()

    asyncio.run(run())


def test_admission_controller_is_nonblocking_and_recovers_after_release():
    gate = ParserAdmission(max_active=1)
    assert gate.try_acquire()
    assert not gate.try_acquire()
    gate.release()
    assert gate.try_acquire()
    gate.release()


def test_worker_rejects_serialized_rows_that_exceed_the_ipc_limit(monkeypatch, tmp_path):
    import base64
    import io
    import json
    import sys
    from types import SimpleNamespace

    from saku import parsers
    from saku.parsers import sandbox
    from saku.parsers.worker_client import MAX_RESPONSE_BYTES

    rows = [RawTxn(date(2026, 9, 1), Decimal("1.00"), "💸" * 2_000, "normal") for _ in range(400)]
    request = {"suffix": ".csv", "workdir": str(tmp_path), "account": {"provider": "bank_a", "last4": None},
               "data": base64.b64encode(b"synthetic").decode("ascii")}
    monkeypatch.setattr(parsers, "needs_password", lambda _path: False)
    monkeypatch.setattr(parsers, "pick", lambda *_: SimpleNamespace(name="saku-csv/1"))
    monkeypatch.setattr(parsers, "parse_for_account", lambda *_: rows)

    class Capture:
        def __init__(self):
            self.buffer = io.BytesIO()

        def write(self, value):
            encoded = value.encode("utf-8")
            self.buffer.write(encoded)
            return len(value)

    capture = Capture()
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(json.dumps(request).encode("utf-8"))))
    monkeypatch.setattr(sys, "stdout", capture)

    sandbox._worker()

    encoded = capture.buffer.getvalue()
    outcome = json.loads(encoded)
    assert len(encoded) <= MAX_RESPONSE_BYTES
    assert outcome["rows"] is None
    assert outcome["error"] and "Nothing was imported" in outcome["error"]


def test_parser_and_client_reject_values_not_representable_in_the_ledger():
    from saku.parser_service import ParserService

    oversized = RawTxn(date(2026, 9, 1), Decimal("10000000000000000.00"), "SYNTHETIC", "normal")
    response = ParserService._serialize(ParseOutcome("bca-pdf/1", [oversized], None))
    assert response["rows"] is None and response["error"]

    for amount in ("10000000000000000.00", "0.001"):
        client_result = ParserClient._outcome({
            "ok": True, "parser": "bca-pdf/1", "rows": [["2026-09-01", amount, "SYNTHETIC", "normal"]],
            "error": None, "unsupported": False,
        })
        assert client_result.rows is None and client_result.error
