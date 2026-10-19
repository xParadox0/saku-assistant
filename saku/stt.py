"""Client for the local speech-to-text bridge (saku/stt_bridge.py).

The bot sends the raw voice-note bytes over a Unix socket and gets text back. Audio never leaves this machine and
is never written to the database. The protocol is one request per connection:

    -> b"SAKU-STT/1 <byte count>\\n" + <audio bytes>
    <- {"ok": true, "text": "..."}\\n   or   {"ok": false, "error": "..."}\\n
"""
from __future__ import annotations

import json
import socket

MAX_AUDIO_BYTES = 1_000_000
MAX_TEXT_CHARS = 1_000
_MAX_RESPONSE_BYTES = 16 * 1024


class TranscribeError(RuntimeError):
    pass


class UnixTranscriberClient:
    def __init__(self, path: str, timeout: int = 60):
        self.path = path
        self.timeout = timeout

    def transcribe(self, audio: bytes) -> str:
        if not audio or len(audio) > MAX_AUDIO_BYTES:
            raise TranscribeError("audio is empty or too large")
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(self.timeout)
                client.connect(self.path)
                client.sendall(f"SAKU-STT/1 {len(audio)}\n".encode() + audio)
                client.shutdown(socket.SHUT_WR)
                chunks, size = [], 0
                while True:
                    chunk = client.recv(4096)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > _MAX_RESPONSE_BYTES:
                        raise TranscribeError("response too large")
                    chunks.append(chunk)
        except (OSError, TimeoutError) as exc:
            raise TranscribeError("speech service unavailable") from exc
        try:
            response = json.loads(b"".join(chunks).split(b"\n", 1)[0])
        except (ValueError, UnicodeDecodeError) as exc:
            raise TranscribeError("invalid response") from exc
        text = response.get("text") if isinstance(response, dict) else None
        if response.get("ok") is not True or not isinstance(text, str):
            raise TranscribeError("transcription failed")
        return text.strip()[:MAX_TEXT_CHARS]
