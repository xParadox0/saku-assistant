"""Narrow client contract for Saku's transaction-extraction agent.

The bot never gives the model database access. It sends one redacted message plus account display labels to a
local Unix socket and validates the returned structure before any write is attempted.
"""
from __future__ import annotations

import json
import socket
from datetime import date

from .normalise import redact

MODEL = "claude-haiku-5-5"
MAX_MESSAGE_CHARS = 1_000
MAX_TRANSACTIONS = 10
MAX_RESPONSE_BYTES = 64 * 1024


class AgentError(RuntimeError):
    pass


def validate_extraction(value: object) -> dict:
    if not isinstance(value, dict) or value.get("intent") not in {"record", "question", "unknown"}:
        raise AgentError("invalid agent result")
    raw_transactions = value.get("transactions")
    if not isinstance(raw_transactions, list) or len(raw_transactions) > MAX_TRANSACTIONS:
        raise AgentError("invalid transaction list")
    transactions = []
    for raw in raw_transactions:
        if not isinstance(raw, dict):
            raise AgentError("invalid transaction")
        description = raw.get("description")
        amount = raw.get("amount_idr")
        direction = raw.get("direction")
        kind = raw.get("kind", "normal")
        booked_on = raw.get("date")
        account_hint = raw.get("account_hint")
        if description is not None and (not isinstance(description, str) or not 1 <= len(description.strip()) <= 80):
            raise AgentError("invalid description")
        if amount is not None and (not isinstance(amount, int) or isinstance(amount, bool) or not 0 < amount <= 10**15):
            raise AgentError("invalid amount")
        if direction not in {"expense", "income"}:
            raise AgentError("invalid direction")
        if kind not in {"normal", "own_transfer"}:
            raise AgentError("invalid kind")
        if booked_on is not None:
            if not isinstance(booked_on, str):
                raise AgentError("invalid date")
            try:
                date.fromisoformat(booked_on)
            except ValueError as exc:
                raise AgentError("invalid date") from exc
        if account_hint is not None and (not isinstance(account_hint, str) or len(account_hint.strip()) > 60):
            raise AgentError("invalid account hint")
        transactions.append({
            "description": description.strip() if description else None,
            "amount_idr": amount,
            "direction": direction,
            "kind": kind,
            "date": booked_on,
            "account_hint": account_hint.strip() if account_hint else None,
        })
    return {"intent": value["intent"], "transactions": transactions}


class UnixAgentClient:
    def __init__(self, path: str, timeout: int = 45):
        self.path = path
        self.timeout = timeout

    def extract(self, text: str, accounts: list[dict], today: date) -> dict:
        if not text or len(text) > MAX_MESSAGE_CHARS:
            raise AgentError("message is empty or too long")
        safe_accounts = [{"label": str(a["label"])[:60], "provider": str(a["provider"])[:30]}
                         for a in accounts[:20]]
        request = json.dumps({"op": "extract_transactions", "text": redact(text),
                              "accounts": safe_accounts, "today": today.isoformat()}, ensure_ascii=False).encode() + b"\n"
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(self.timeout)
                client.connect(self.path)
                client.sendall(request)
                chunks, size = [], 0
                while True:
                    chunk = client.recv(4096)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > MAX_RESPONSE_BYTES:
                        raise AgentError("agent response too large")
                    chunks.append(chunk)
                    if b"\n" in chunk:
                        break
        except (OSError, TimeoutError) as exc:
            raise AgentError("agent unavailable") from exc
        try:
            response = json.loads(b"".join(chunks).split(b"\n", 1)[0])
        except (ValueError, UnicodeDecodeError) as exc:
            raise AgentError("invalid agent response") from exc
        if response.get("ok") is not True or response.get("model") != MODEL:
            raise AgentError("agent request failed")
        return validate_extraction(response.get("result"))
