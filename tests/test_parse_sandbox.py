"""Statement parsing runs in a separate, resource-limited process (saku/parsers/sandbox.py).

These are the guarantees the web route relies on: a crafted file cannot block the server or exhaust its memory,
library warnings never carry statement text into logs, the worker never sees database credentials, and every
outcome is a value the route can record.
"""
import os
import sys
import time
from decimal import Decimal

from bca_fixture import bomb, build, corrupt_stream, sample
from saku.parsers import sandbox


BCA = {"provider": "bca", "last4": "6789"}


def test_bca_statement_parses_in_the_sandbox(tmp_path):
    out = sandbox.parse_upload(build(sample()), ".pdf", BCA, str(tmp_path))
    assert out.error is None and out.parser == "bca-pdf/1"
    assert len(out.rows) == 10 and out.rows[0].amount == Decimal("1500000.00")
    assert out.rows[2].kind == "fee"
    assert list(tmp_path.iterdir()) == []


def test_csv_still_parses_in_the_sandbox(tmp_path):
    out = sandbox.parse_upload(b"date,amount,description\n2026-10-01,-45000,KOPI\n", ".csv",
                               {"provider": "bank_a", "last4": None}, str(tmp_path))
    assert out.error is None and out.parser == "saku-csv/1" and len(out.rows) == 1


def test_unsupported_file_is_reported_as_unsupported(tmp_path):
    out = sandbox.parse_upload(b"%PDF-1.4 fake", ".pdf", BCA, str(tmp_path))
    assert out.unsupported and out.rows is None


def test_password_message_is_neutral_for_any_bank(tmp_path):
    data = build(sample(), user_password="secret123")
    out = sandbox.parse_upload(data, ".pdf", {"provider": "bri", "last4": None}, str(tmp_path))
    assert "password" in out.error and "BCA" not in out.error


def test_decompression_bomb_is_stopped_quickly(tmp_path):
    started = time.monotonic()
    out = sandbox.parse_upload(bomb(2_000_000), ".pdf", BCA, str(tmp_path), timeout=3)
    assert time.monotonic() - started < 8
    assert out.rows is None and out.error
    assert list(tmp_path.iterdir()) == []


def test_library_warnings_never_reach_our_stderr(tmp_path, capfd):
    data = corrupt_stream(build(sample(), encrypt=False))
    sandbox.parse_upload(data, ".pdf", BCA, str(tmp_path))
    captured = capfd.readouterr()
    for secret in ("0123456789", "BUDI", "CONTOH", "1,500,000"):
        assert secret not in captured.err and secret not in captured.out


def test_worker_environment_does_not_inherit_secret_variables(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@h/db")
    monkeypatch.setenv("SAKU_SECRET", "x" * 40)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    env = sandbox.worker_env()
    assert not {"DATABASE_URL", "SAKU_SECRET", "TELEGRAM_BOT_TOKEN"} & set(env)
    assert env["PYTHONPATH"] and os.path.isdir(env["PYTHONPATH"])
    assert sys.executable  # the worker is started with the same interpreter


def test_worker_launch_avoids_preexec_in_the_multithreaded_service(monkeypatch, tmp_path):
    import subprocess

    fake_root = tmp_path / "fake-root"
    package = fake_root / "saku" / "parsers"
    package.mkdir(parents=True)
    (fake_root / "saku" / "__init__.py").write_text("")
    (package / "__init__.py").write_text("")
    payload = '{"parser":"saku-csv/1","rows":[["2026-10-01","1.00","SYNTHETIC","normal"]],"error":null,"unsupported":false}'
    (package / "worker_entry.py").write_text("import sys\nsys.stdout.write(" + repr(payload) + ")\n")
    workdir = tmp_path / "work"
    workdir.mkdir()
    monkeypatch.setattr(sandbox, "_PACKAGE_ROOT", str(fake_root))
    captured = {}
    real_popen = subprocess.Popen

    def capture_popen(args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return real_popen(args, **kwargs)

    monkeypatch.setattr(sandbox.subprocess, "Popen", capture_popen)
    out = sandbox.parse_upload(b"synthetic", ".csv", {"provider": "bank_a"}, str(workdir))

    assert out.error is None and out.parser == "saku-csv/1"
    assert out.rows is not None and len(out.rows) == 1
    assert captured["args"][:3] == [sys.executable, "-m", "saku.parsers.worker_entry"]
    assert "preexec_fn" not in captured["kwargs"]
    assert captured["kwargs"]["start_new_session"] is True
    assert list(workdir.iterdir()) == []


def test_worker_entry_applies_cpu_memory_and_core_limits_before_parsing(monkeypatch):
    import resource
    from saku.parsers import worker_entry

    events = []
    monkeypatch.setattr(worker_entry.resource, "setrlimit",
                        lambda kind, value: events.append((kind, value)))
    monkeypatch.setattr(worker_entry, "_worker", lambda: events.append("parse"))

    worker_entry.main([str(sandbox.MEMORY_LIMIT), str(sandbox.CPU_SECONDS)])

    assert events == [
        (resource.RLIMIT_AS, (sandbox.MEMORY_LIMIT, sandbox.MEMORY_LIMIT)),
        (resource.RLIMIT_CPU, (sandbox.CPU_SECONDS, sandbox.CPU_SECONDS)),
        (resource.RLIMIT_CORE, (0, 0)),
        "parse",
    ]


def test_worker_stdout_overflow_kills_child_before_wall_timeout(tmp_path, monkeypatch):
    fake_root = tmp_path / "fake-root"
    package = fake_root / "saku" / "parsers"
    package.mkdir(parents=True)
    (fake_root / "saku" / "__init__.py").write_text("")
    (package / "__init__.py").write_text("")
    (package / "worker_entry.py").write_text(
        "import sys, time\nsys.stdout.buffer.write(b'x' * 2048)\nsys.stdout.flush()\ntime.sleep(5)\n"
    )
    workdir = tmp_path / "work"
    workdir.mkdir()
    monkeypatch.setattr(sandbox, "_PACKAGE_ROOT", str(fake_root))
    monkeypatch.setattr(sandbox, "MAX_WORKER_OUTPUT_BYTES", 1024)

    started = time.monotonic()
    result = sandbox.parse_upload(b"synthetic", ".csv", {"provider": "bca"}, str(workdir), timeout=2)

    assert result.error and "output" in result.error.lower() and "Nothing was imported" in result.error
    assert time.monotonic() - started < 1.5
    assert list(workdir.iterdir()) == []