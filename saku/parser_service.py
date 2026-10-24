"""Unix-socket parser service, intended to run in a networkless, read-only container."""
from __future__ import annotations

import asyncio
import base64
import json
import os
import shutil
import stat
import tempfile
import threading
from datetime import date
from pathlib import Path

from .parsers import sandbox
from .parsers.sandbox import MAX_UPLOAD_BYTES, ParseOutcome, WALL_SECONDS
from .parsers.worker_client import MAX_REQUEST_BYTES, MAX_RESPONSE_BYTES, MAX_ROWS, DEFAULT_SOCKET, ParserClient
from .normalise import valid_transaction_amount

ERROR_SAFE = "the file could not be read safely. Nothing was imported"
BUSY_SAFE = "the statement parser is busy; retry shortly. Nothing was imported"


class ParserService:
    """A one-job-at-a-time worker with no application credentials or network access."""

    def __init__(self, socket_path: str | Path = DEFAULT_SOCKET, work_root: str | Path = "/tmp/saku-parser-jobs",
                 parse_fn=None, max_concurrent: int = 1):
        if not 1 <= max_concurrent <= 4:
            raise ValueError("max_concurrent must be between 1 and 4")
        self.socket_path = Path(socket_path)
        self.work_root = Path(work_root)
        self.parse_fn = parse_fn
        self._slots = threading.BoundedSemaphore(max_concurrent)
        self._server: asyncio.AbstractServer | None = None
        self._socket_inode: int | None = None

    async def start(self) -> None:
        self.socket_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.work_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            existing = self.socket_path.lstat()
        except FileNotFoundError:
            existing = None
        if existing is not None:
            if not stat.S_ISSOCK(existing.st_mode):
                raise RuntimeError("parser socket path exists and is not a socket")
            self.socket_path.unlink()
        self._server = await asyncio.start_unix_server(
            self._handle, path=str(self.socket_path), limit=MAX_REQUEST_BYTES + 1, backlog=2)
        os.chmod(self.socket_path, 0o600)
        self._socket_inode = self.socket_path.stat().st_ino

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        try:
            current = self.socket_path.lstat()
            if stat.S_ISSOCK(current.st_mode) and current.st_ino == self._socket_inode:
                self.socket_path.unlink()
        except FileNotFoundError:
            pass
        self._socket_inode = None

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        acquired = self._slots.acquire(blocking=False)
        try:
            if not acquired:
                await self._send(writer, {"busy": True, "error": BUSY_SAFE})
                return
            try:
                line = await asyncio.wait_for(reader.readline(), timeout=3.0)
                if not line or len(line) > MAX_REQUEST_BYTES:
                    await self._send(writer, self._failure())
                    return
                request = json.loads(line)
                if not isinstance(request, dict):
                    await self._send(writer, self._failure())
                    return
                if request.get("op") == "health":
                    await self._send(writer, {"ok": True})
                    return
                data, suffix, account = self._decode_request(request)
                workdir = tempfile.mkdtemp(prefix="parse-", dir=self.work_root)
                try:
                    parser = self.parse_fn or sandbox.parse_upload
                    outcome = await asyncio.to_thread(parser, data, suffix, account, workdir)
                except Exception:
                    outcome = ParseOutcome(None, None, ERROR_SAFE)
                finally:
                    shutil.rmtree(workdir, ignore_errors=True)
                await self._send(writer, self._serialize(outcome))
            except (asyncio.TimeoutError, asyncio.LimitOverrunError, UnicodeError, ValueError, TypeError, OSError):
                await self._send(writer, self._failure())
        finally:
            if acquired:
                self._slots.release()
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass

    @staticmethod
    def _decode_request(request: dict) -> tuple[bytes, str, dict]:
        if request.get("op") != "parse":
            raise ValueError("unsupported operation")
        suffix = request.get("suffix")
        account = request.get("account")
        encoded = request.get("data")
        if suffix not in (".pdf", ".csv") or not isinstance(account, dict) or not isinstance(encoded, str):
            raise ValueError("invalid parse request")
        provider = account.get("provider")
        last4 = account.get("last4")
        if not isinstance(provider, str) or not 1 <= len(provider) <= 30:
            raise ValueError("invalid account metadata")
        if last4 is not None and (not isinstance(last4, str) or len(last4) != 4 or not last4.isascii() or not last4.isdigit()):
            raise ValueError("invalid account metadata")
        data = base64.b64decode(encoded, validate=True)
        if len(data) > MAX_UPLOAD_BYTES:
            raise ValueError("upload too large")
        return data, suffix, {"provider": provider, "last4": last4}

    @classmethod
    def _serialize(cls, outcome: ParseOutcome) -> dict:
        if not isinstance(outcome, ParseOutcome):
            return cls._failure()
        if outcome.busy:
            return {"busy": True, "error": BUSY_SAFE}
        if outcome.error is not None:
            if not isinstance(outcome.error, str) or len(outcome.error) > 500:
                return cls._failure()
            parser = outcome.parser if isinstance(outcome.parser, str) and len(outcome.parser) <= 80 else None
            return {"ok": True, "parser": parser, "rows": None, "error": outcome.error,
                    "unsupported": bool(outcome.unsupported)}
        if outcome.unsupported:
            return {"ok": True, "parser": None, "rows": None, "error": None, "unsupported": True}
        if not isinstance(outcome.parser, str) or not outcome.parser or len(outcome.parser) > 80:
            return cls._failure()
        if not isinstance(outcome.rows, list) or not 1 <= len(outcome.rows) <= MAX_ROWS:
            return cls._failure()
        rows = []
        try:
            for row in outcome.rows:
                if not isinstance(row.booked_on, date) or not valid_transaction_amount(row.amount):
                    return cls._failure()
                if not isinstance(row.description, str) or len(row.description) > 2_000:
                    return cls._failure()
                rows.append([row.booked_on.isoformat(), str(row.amount), row.description, row.kind])
        except (AttributeError, TypeError, ValueError):
            return cls._failure()
        payload = {"ok": True, "parser": outcome.parser, "rows": rows, "error": None, "unsupported": False}
        if len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > MAX_RESPONSE_BYTES:
            return cls._failure()
        return payload

    @staticmethod
    def _failure() -> dict:
        return {"ok": True, "parser": None, "rows": None, "error": ERROR_SAFE, "unsupported": False}

    @staticmethod
    async def _send(writer: asyncio.StreamWriter, response: dict) -> None:
        payload = json.dumps(response, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
        if len(payload) > MAX_RESPONSE_BYTES:
            payload = json.dumps(ParserService._failure(), separators=(",", ":")).encode("ascii") + b"\n"
        writer.write(payload)
        try:
            await asyncio.wait_for(writer.drain(), timeout=2.0)
        except (asyncio.TimeoutError, OSError):
            pass


async def _serve() -> None:
    service = ParserService()
    await service.start()
    try:
        assert service._server is not None
        async with service._server:
            await service._server.serve_forever()
    finally:
        await service.close()


def main() -> int:
    import sys
    if sys.argv[1:] == ["--healthcheck"]:
        return 0 if asyncio.run(ParserClient().health_check()) else 1
    asyncio.run(_serve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
