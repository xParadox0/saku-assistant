"""Local speech-to-text bridge for the containerised Saku bot.

One operation over a Unix socket: turn a short voice note into Indonesian text with faster-whisper on this machine.
No audio is written to disk, kept after the request, or sent to any third-party service. The model loads on the
first request and is released after a period without requests, so it does not hold memory all day.

Runs as a user systemd service (deploy/saku-stt-bridge.service) from its own virtualenv, because the model
libraries are large and are not needed by the web or bot images.
"""
from __future__ import annotations

import argparse
import io
import json
import os
import socketserver
import stat
import threading
import time
from pathlib import Path
from typing import Callable

from .stt import MAX_AUDIO_BYTES, MAX_TEXT_CHARS

MAX_SECONDS = 60
HEADER = b"SAKU-STT/1 "
IDLE_UNLOAD_SECONDS = 15 * 60
READ_TIMEOUT = 15
# Vocabulary hint: makes "5 ribu", "cash", "tunai" and provider names come out the way users write them.
PROMPT = ("Catatan transaksi keuangan dalam bahasa Indonesia: ribu, juta, rupiah, cash, tunai, "
          "GoPay, OVO, DANA, ShopeePay, BCA, BRI, BNI, Mandiri, Jago, SeaBank.")


class BridgeError(RuntimeError):
    pass


class WhisperEngine:
    """Lazy faster-whisper model with idle unload. `transcribe(audio_bytes) -> (text, duration_seconds)`."""

    def __init__(self, model: str = "small", threads: int = 2, idle: int = IDLE_UNLOAD_SECONDS):
        """`model` is a size name ("small") or a local directory with a converted CTranslate2 model.

        Production passes a local directory so the bridge never touches the network or the Hugging Face cache.
        """
        self.model_name, self.threads, self.idle = model, threads, idle
        self._model = None
        self._last = 0.0
        self._lock = threading.Lock()
        threading.Thread(target=self._reaper, daemon=True).start()

    def _reaper(self) -> None:
        while True:
            time.sleep(60)
            with self._lock:
                if self._model is not None and time.monotonic() - self._last > self.idle:
                    self._model = None

    def transcribe(self, audio: bytes) -> tuple[str, float]:
        with self._lock:                       # one transcription at a time keeps memory bounded
            if self._model is None:
                from faster_whisper import WhisperModel
                self._model = WhisperModel(self.model_name, device="cpu", compute_type="int8",
                                           cpu_threads=self.threads)
            self._last = time.monotonic()
            segments, info = self._model.transcribe(io.BytesIO(audio), language="id", beam_size=5,
                                                    initial_prompt=PROMPT, vad_filter=True,
                                                    condition_on_previous_text=False)
            if info.duration > MAX_SECONDS + 1:
                raise BridgeError("audio too long")
            text = " ".join(s.text.strip() for s in segments).strip()
            self._last = time.monotonic()
            return text, info.duration


def read_request(rfile) -> bytes:
    header = rfile.readline(64)
    if not header.startswith(HEADER) or not header.endswith(b"\n"):
        raise BridgeError("invalid request")
    try:
        size = int(header[len(HEADER):].strip())
    except ValueError:
        raise BridgeError("invalid request") from None
    if not 0 < size <= MAX_AUDIO_BYTES:
        raise BridgeError("audio is empty or too large")
    data = rfile.read(size)
    if len(data) != size:
        raise BridgeError("incomplete audio")
    return data


def make_handler(transcribe: Callable[[bytes], tuple[str, float]], slots: threading.BoundedSemaphore,
                 busy_timeout: float):
    class Handler(socketserver.StreamRequestHandler):
        timeout = READ_TIMEOUT

        def handle(self) -> None:
            if not slots.acquire(timeout=busy_timeout):
                response = {"ok": False, "error": "busy"}
                self.wfile.write(json.dumps(response).encode() + b"\n")
                return
            try:
                audio = read_request(self.rfile)
                if not _looks_like_audio(audio):
                    raise BridgeError("unsupported audio format")
                text, _ = transcribe(audio)
                response = {"ok": True, "text": text[:MAX_TEXT_CHARS]}
            except BridgeError as exc:
                response = {"ok": False, "error": str(exc)}
            except Exception:                  # never echo library errors; they may describe the audio
                response = {"ok": False, "error": "transcription failed"}
            finally:
                slots.release()
            self.wfile.write(json.dumps(response, ensure_ascii=False).encode() + b"\n")
    return Handler


def _looks_like_audio(data: bytes) -> bool:
    """Accept only container signatures Telegram voice/audio uses, so the decoder never sees arbitrary formats."""
    head = data[:12]
    return (head.startswith(b"OggS") or head.startswith(b"ID3") or head[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2")
            or head.startswith(b"RIFF") and data[8:12] == b"WAVE" or head[4:8] == b"ftyp"
            or head.startswith(b"\x1aE\xdf\xa3") or head[:2] in (b"\xff\xf1", b"\xff\xf9"))


class Server(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True


def serve(socket_path: str, transcribe: Callable[[bytes], tuple[str, float]], *, max_active: int = 2,
          busy_timeout: float = 20.0) -> None:
    path = Path(socket_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o711)   # traversable by the container uid, not listable by other local users
    if path.exists() or path.is_socket():
        if not stat.S_ISSOCK(path.lstat().st_mode):
            raise SystemExit("refusing to replace a non-socket path")
        path.unlink()
    slots = threading.BoundedSemaphore(max_active)
    with Server(str(path), make_handler(transcribe, slots, busy_timeout)) as server:
        os.chmod(path, 0o666)  # the container client has a different uid; the parent is not listable
        try:
            server.serve_forever()
        finally:
            path.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--socket", required=True)
    parser.add_argument("--model", default="small",
                        help="size name (small, medium) or path to a local CTranslate2 model directory")
    parser.add_argument("--threads", type=int, default=2)
    args = parser.parse_args()
    serve(args.socket, WhisperEngine(args.model, args.threads).transcribe)


if __name__ == "__main__":
    main()
