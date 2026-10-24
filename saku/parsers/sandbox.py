"""Run statement parsing in a separate, resource-limited process.

Why: a 5 MB upload can be a PDF whose compressed streams expand to gigabytes of text operations. Parsing it in the
web process would pin the CPU, block every tenant's requests and risk the OOM killer. PDF libraries also log
warnings that quote decrypted page text, which must never reach server logs.

The worker subprocess runs inside the separate parser container (network disabled,
read-only root filesystem, no database credentials, cgroup memory/CPU/pid limits):
- gets the file bytes on stdin and returns JSON on stdout; it receives no database connection and its environment is
  allowlisted;
- runs with RLIMIT_AS (memory), RLIMIT_CPU and a wall-clock timeout, and is killed when it exceeds them;
- has stderr discarded, and silences the pypdf/pdfminer loggers itself.

The parser service itself accepts work over a private Unix socket and has bounded
admission. The web route never runs PDF parsing in the web container.
"""
from __future__ import annotations

import base64
import json
import os
import selectors
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path

from ..normalise import RawTxn, valid_transaction_amount

MEMORY_LIMIT = 768 * 1024 * 1024
CPU_SECONDS = 20
WALL_SECONDS = 30
MAX_UPLOAD_BYTES = 5 * 1024 * 1024
MAX_WORKER_OUTPUT_BYTES = 7 * 1024 * 1024
MAX_WORKER_ROWS = 5_000
MAX_WORKER_DESCRIPTION_CHARS = 2_000

_SAFE_ENV = ("PATH", "LANG", "LC_ALL", "TZ", "HOME")
_PACKAGE_ROOT = str(Path(__file__).resolve().parents[2])


@dataclass(frozen=True)
class ParseOutcome:
    parser: str | None
    rows: list[RawTxn] | None
    error: str | None
    unsupported: bool = False
    busy: bool = False


@dataclass(frozen=True)
class _WorkerExecution:
    returncode: int | None
    stdout: bytes
    failure: str | None = None


def worker_env() -> dict:
    env = {k: os.environ[k] for k in _SAFE_ENV if k in os.environ}
    env["PYTHONPATH"] = _PACKAGE_ROOT
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONHASHSEED"] = "0"
    return env


def _kill_worker_group(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except OSError:
        try:
            proc.kill()
        except OSError:
            pass


def _stop_worker(proc: subprocess.Popen) -> None:
    _kill_worker_group(proc)
    try:
        proc.wait(timeout=1.0)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
        except OSError:
            pass
        try:
            proc.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            pass


def _run_bounded_worker(request: bytes, workdir: str, timeout: float, memory: int, cpu: int) -> _WorkerExecution:
    """Run the parser with bounded stdout and kill its process group on excess output or timeout."""
    request_path = Path(workdir) / "worker-request.json"
    proc = None
    selector = None
    output = bytearray()
    failure = None
    try:
        fd = os.open(request_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as request_file:
            request_file.write(request)
        with request_path.open("rb") as request_file:
            proc = subprocess.Popen(
                [sys.executable, "-m", "saku.parsers.worker_entry", str(memory), str(cpu)],
                stdin=request_file, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                env=worker_env(), cwd=workdir, bufsize=0, start_new_session=True,
            )
        assert proc.stdout is not None
        os.set_blocking(proc.stdout.fileno(), False)
        selector = selectors.DefaultSelector()
        selector.register(proc.stdout, selectors.EVENT_READ)
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                failure = "timeout"
                break
            if not selector.select(remaining):
                failure = "timeout"
                break
            try:
                chunk = os.read(proc.stdout.fileno(), min(64 * 1024, MAX_WORKER_OUTPUT_BYTES + 1 - len(output)))
            except BlockingIOError:
                continue
            if not chunk:
                break
            if len(output) + len(chunk) > MAX_WORKER_OUTPUT_BYTES:
                failure = "output_limit"
                break
            output.extend(chunk)

        if failure is not None:
            _stop_worker(proc)
        else:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                failure = "timeout"
                _stop_worker(proc)
            else:
                try:
                    proc.wait(timeout=remaining)
                except subprocess.TimeoutExpired:
                    failure = "timeout"
                    _stop_worker(proc)
        return _WorkerExecution(proc.returncode, bytes(output), failure)
    except OSError:
        if proc is not None:
            _stop_worker(proc)
        return _WorkerExecution(None, b"", "failed")
    finally:
        if selector is not None:
            selector.close()
        if proc is not None and proc.stdout is not None:
            proc.stdout.close()


def parse_upload(data: bytes, suffix: str, account: dict, workdir: str, *, timeout: float = WALL_SECONDS,
                 memory: int = MEMORY_LIMIT, cpu: int = CPU_SECONDS) -> ParseOutcome:
    """Parse upload bytes for one account. Never raises for bad input; every outcome is a ParseOutcome."""
    request = json.dumps({"suffix": suffix, "workdir": workdir,
                          "account": {"provider": account.get("provider"), "last4": account.get("last4")},
                          "data": base64.b64encode(data).decode()})
    try:
        execution = _run_bounded_worker(request.encode(), workdir, timeout, memory, cpu)
    finally:
        _clean(workdir)
    if execution.failure == "timeout":
        return ParseOutcome(None, None, "the file took too long to read. Nothing was imported")
    if execution.failure == "output_limit":
        return ParseOutcome(None, None, "the file produced too much output. Nothing was imported")
    if execution.failure is not None or execution.returncode != 0:
        return ParseOutcome(None, None, "the file could not be read safely; it may be damaged or too complex. "
                                        "Nothing was imported")
    try:
        out = json.loads(execution.stdout)
    except ValueError:
        return ParseOutcome(None, None, "the file could not be read. Nothing was imported")
    rows = None
    if out.get("rows") is not None:
        rows = [RawTxn(date.fromisoformat(r[0]), Decimal(r[1]), r[2], r[3]) for r in out["rows"]]
    return ParseOutcome(out.get("parser"), rows, out.get("error"), bool(out.get("unsupported")))


def _clean(workdir: str) -> None:
    for p in Path(workdir).glob("*"):
        try:
            p.unlink()
        except OSError:
            pass


def _worker() -> None:
    import logging
    for name in ("pypdf", "pdfminer", "pdfplumber"):
        lg = logging.getLogger(name)
        lg.setLevel(logging.CRITICAL + 1)
        lg.propagate = False
        lg.addHandler(logging.NullHandler())
    logging.lastResort = None
    import warnings
    warnings.simplefilter("ignore")

    from . import PASSWORD_MESSAGE, ParseError, needs_password, parse_for_account, pick

    req = json.loads(sys.stdin.buffer.read())
    path = Path(req["workdir"]) / f"statement{req['suffix']}"
    path.write_bytes(base64.b64decode(req["data"]))
    out: dict
    try:
        if needs_password(path):
            out = {"parser": None, "rows": None, "error": PASSWORD_MESSAGE}
        else:
            parser = pick(path, req["account"].get("provider") or "")
            if parser is None:
                out = {"parser": None, "rows": None, "error": None, "unsupported": True}
            else:
                try:
                    rows = parse_for_account(parser, path, req["account"])
                    out = {"parser": parser.name, "error": None,
                           "rows": [[r.booked_on.isoformat(), str(r.amount), r.description, r.kind] for r in rows]}
                except ParseError as e:
                    out = {"parser": parser.name, "rows": None, "error": str(e)}
    except Exception:
        out = {"parser": None, "rows": None, "error": "the file could not be read. Nothing was imported"}
    finally:
        try:
            path.unlink()
        except OSError:
            pass
    sys.stdout.write(_encode_worker_result(out))


def _encode_worker_result(out: dict) -> str:
    """Serialize a bounded IPC result; never let hostile text inflate the parent process's captured stdout."""
    failure = '{"parser":null,"rows":null,"error":"the file could not be read safely; its result exceeded a safe limit. Nothing was imported"}'
    if not isinstance(out, dict):
        return failure
    parser = out.get("parser")
    error = out.get("error")
    unsupported = bool(out.get("unsupported"))
    if error is not None:
        if not isinstance(error, str) or len(error) > 500:
            return failure
        if parser is not None and (not isinstance(parser, str) or len(parser) > 80):
            parser = None
        encoded = json.dumps({"parser": parser, "rows": None, "error": error, "unsupported": unsupported},
                             separators=(",", ":"))
        return encoded if len(encoded) <= MAX_WORKER_OUTPUT_BYTES else failure
    if unsupported:
        return '{"parser":null,"rows":null,"error":null,"unsupported":true}'
    rows = out.get("rows")
    if (not isinstance(parser, str) or not parser or len(parser) > 80
            or not isinstance(rows, list) or not 1 <= len(rows) <= MAX_WORKER_ROWS):
        return failure

    prefix = '{"parser":' + json.dumps(parser, separators=(",", ":")) + ',"error":null,"rows":['
    parts = []
    size = len(prefix) + 2  # closing brackets
    try:
        for row in rows:
            if not isinstance(row, list) or len(row) != 4:
                return failure
            booked_on, amount, description, kind = row
            if (not isinstance(booked_on, str) or len(booked_on) != 10
                    or date.fromisoformat(booked_on).isoformat() != booked_on
                    or not isinstance(amount, str) or len(amount) > 64
                    or not valid_transaction_amount(Decimal(amount))
                    or not isinstance(description, str) or len(description) > MAX_WORKER_DESCRIPTION_CHARS
                    or not isinstance(kind, str) or len(kind) > 80):
                return failure
            part = json.dumps(row, separators=(",", ":"))
            next_size = size + len(part) + (1 if parts else 0)
            if next_size > MAX_WORKER_OUTPUT_BYTES:
                return failure
            parts.append(part)
            size = next_size
    except Exception:
        return failure
    return prefix + ",".join(parts) + "]}"


if __name__ == "__main__":
    _worker()
