"""Async client and nonblocking admission control for the isolated parser service."""
from __future__ import annotations

import asyncio
import base64
import json
import os
import threading
import time
from collections import deque
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

from ..normalise import RawTxn, valid_transaction_amount
from .sandbox import MAX_UPLOAD_BYTES, ParseOutcome

DEFAULT_SOCKET = "/run/saku-parser/parser.sock"
MAX_ROWS = 5_000
MAX_REQUEST_BYTES = ((MAX_UPLOAD_BYTES + 2) // 3) * 4 + 4_096
MAX_RESPONSE_BYTES = 8 * 1024 * 1024


class ParserAdmission:
    """Zero-wait global admission plus a bounded per-tenant sliding-window quota."""

    def __init__(self, max_active: int = 1, per_tenant_limit: int = 5,
                 per_tenant_window: float = 600.0, max_tracked_tenants: int = 4096, clock=None):
        if not 1 <= max_active <= 64:
            raise ValueError("max_active must be between 1 and 64")
        if not 1 <= per_tenant_limit <= 100 or per_tenant_window <= 0:
            raise ValueError("invalid per-tenant import quota")
        if not 1 <= max_tracked_tenants <= 65_536:
            raise ValueError("max_tracked_tenants must be between 1 and 65536")
        self._slots = threading.BoundedSemaphore(max_active)
        self._per_tenant_limit = per_tenant_limit
        self._per_tenant_window = per_tenant_window
        self._max_tracked_tenants = max_tracked_tenants
        self._clock = clock or time.monotonic
        self._rate_lock = threading.Lock()
        self._tenant_attempts: dict[str, deque[float]] = {}

    def _prune_tenant(self, tenant_id: str, now: float) -> None:
        attempts = self._tenant_attempts.get(tenant_id)
        if attempts is None:
            return
        while attempts and now - attempts[0] >= self._per_tenant_window:
            attempts.popleft()
        if not attempts:
            self._tenant_attempts.pop(tenant_id, None)

    def try_acquire(self, tenant_id: str | None = None) -> bool:
        # No identity is used only by isolated low-level tests. The HTTP middleware
        # requires a tenant key after successful authorization.
        if tenant_id is None:
            return self._slots.acquire(blocking=False)
        if not isinstance(tenant_id, str) or not 1 <= len(tenant_id) <= 128:
            return False
        now = self._clock()
        with self._rate_lock:
            self._prune_tenant(tenant_id, now)
            attempts = self._tenant_attempts.get(tenant_id)
            if attempts is not None and len(attempts) >= self._per_tenant_limit:
                return False
            if attempts is None and len(self._tenant_attempts) >= self._max_tracked_tenants:
                for old_tenant in list(self._tenant_attempts):
                    self._prune_tenant(old_tenant, now)
                if len(self._tenant_attempts) >= self._max_tracked_tenants:
                    return False
            if not self._slots.acquire(blocking=False):
                return False
            self._tenant_attempts.setdefault(tenant_id, deque()).append(now)
            return True

    def release(self) -> None:
        self._slots.release()


class ParserClient:
    def __init__(self, socket_path: str | Path | None = None, timeout: float = 35.0):
        self.socket_path = str(socket_path or os.environ.get("SAKU_PARSER_SOCKET", DEFAULT_SOCKET))
        if not 0.05 <= timeout <= 60:
            raise ValueError("timeout must be between 0.05 and 60 seconds")
        self.timeout = timeout

    async def parse_upload(self, data: bytes, suffix: str, account: dict) -> ParseOutcome:
        if not isinstance(data, bytes) or len(data) > MAX_UPLOAD_BYTES:
            return ParseOutcome(None, None, "the upload is too large to parse. Nothing was imported")
        if suffix not in (".pdf", ".csv"):
            return ParseOutcome(None, None, "unsupported file type. Nothing was imported")
        provider = account.get("provider")
        last4 = account.get("last4")
        if not isinstance(provider, str) or len(provider) > 30:
            return ParseOutcome(None, None, "the selected account could not be verified. Nothing was imported")
        if last4 is not None and (not isinstance(last4, str) or len(last4) != 4 or not last4.isascii() or not last4.isdigit()):
            return ParseOutcome(None, None, "the selected account could not be verified. Nothing was imported")
        message = {
            "op": "parse",
            "suffix": suffix,
            "account": {"provider": provider, "last4": last4},
            "data": base64.b64encode(data).decode("ascii"),
        }
        try:
            response = await asyncio.wait_for(self._exchange(message), timeout=self.timeout)
        except asyncio.TimeoutError:
            return ParseOutcome(None, None, "the statement parser timed out. Nothing was imported")
        except (OSError, ValueError, UnicodeError, asyncio.LimitOverrunError):
            return ParseOutcome(None, None, "the statement parser is temporarily unavailable. Nothing was imported")
        return self._outcome(response)

    async def health_check(self) -> bool:
        try:
            response = await asyncio.wait_for(self._exchange({"op": "health"}), timeout=min(self.timeout, 2.0))
        except (asyncio.TimeoutError, OSError, ValueError, UnicodeError, asyncio.LimitOverrunError):
            return False
        return response.get("ok") is True or response.get("busy") is True

    async def _exchange(self, message: dict) -> dict:
        payload = json.dumps(message, ensure_ascii=True, separators=(",", ":")).encode("ascii") + b"\n"
        if len(payload) > MAX_REQUEST_BYTES:
            raise ValueError("request exceeds parser IPC limit")

        async def exchange():
            reader, writer = await asyncio.open_unix_connection(self.socket_path, limit=MAX_RESPONSE_BYTES + 1)
            try:
                writer.write(payload)
                await writer.drain()
                line = await reader.readline()
                if not line or len(line) > MAX_RESPONSE_BYTES:
                    raise ValueError("invalid parser response size")
                response = json.loads(line)
                if not isinstance(response, dict):
                    raise ValueError("invalid parser response")
                return response
            finally:
                writer.close()
                try:
                    await writer.wait_closed()
                except OSError:
                    pass

        return await exchange()

    @staticmethod
    def _outcome(response: dict) -> ParseOutcome:
        if response.get("busy") is True:
            return ParseOutcome(None, None, "the statement parser is busy; retry shortly. Nothing was imported", busy=True)
        if response.get("ok") is not True:
            return ParseOutcome(None, None, "the statement parser returned an invalid response. Nothing was imported")
        parser = response.get("parser")
        error = response.get("error")
        unsupported = response.get("unsupported") is True
        raw_rows = response.get("rows")
        if error is not None:
            if not isinstance(error, str) or len(error) > 500 or raw_rows is not None:
                return ParseOutcome(None, None, "the statement parser returned an invalid response. Nothing was imported")
            return ParseOutcome(parser if isinstance(parser, str) else None, None, error, unsupported)
        if unsupported:
            return ParseOutcome(None, None, None, unsupported=True)
        if not isinstance(parser, str) or not parser or len(parser) > 80 or not isinstance(raw_rows, list):
            return ParseOutcome(None, None, "the statement parser returned an invalid response. Nothing was imported")
        if not 1 <= len(raw_rows) <= MAX_ROWS:
            return ParseOutcome(None, None, "the statement parser returned an invalid row count. Nothing was imported")
        rows = []
        try:
            for item in raw_rows:
                if not isinstance(item, list) or len(item) != 4:
                    raise ValueError
                day_text, amount_text, description, kind = item
                if not isinstance(day_text, str) or not isinstance(amount_text, str):
                    raise ValueError
                if not isinstance(description, str) or len(description) > 2_000 or not isinstance(kind, str):
                    raise ValueError
                amount = Decimal(amount_text)
                if not valid_transaction_amount(amount):
                    raise ValueError
                rows.append(RawTxn(date.fromisoformat(day_text), amount, description, kind))
        except (InvalidOperation, TypeError, ValueError, ArithmeticError):
            return ParseOutcome(None, None, "the statement parser returned invalid transaction data. Nothing was imported")
        return ParseOutcome(parser, rows, None)
