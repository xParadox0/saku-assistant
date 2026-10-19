"""Speech bridge and its client, over a real Unix socket, with a fake engine (no model download in CI)."""
import json
import socket
import threading
import time
from pathlib import Path

import pytest

from saku import stt, stt_bridge
from saku.stt import TranscribeError, UnixTranscriberClient


@pytest.fixture
def bridge(tmp_path):
    seen = []

    def engine(audio: bytes):
        seen.append(audio)
        if audio == b"OggSBOOM":
            raise RuntimeError("library detail that might describe the audio")
        if audio == b"OggSLONG":
            raise stt_bridge.BridgeError("audio too long")
        return "  bayar parkir mobil 5.000  ", 2.0

    path = str(Path(tmp_path) / "stt.sock")
    t = threading.Thread(target=stt_bridge.serve, args=(path, engine), daemon=True)
    t.start()
    for _ in range(100):
        if Path(path).exists():
            break
        time.sleep(0.02)
    return path, seen


def test_round_trip_returns_trimmed_text(bridge):
    path, seen = bridge
    assert UnixTranscriberClient(path).transcribe(b"OggS audio") == "bayar parkir mobil 5.000"
    assert seen == [b"OggS audio"]


def test_engine_errors_are_generic(bridge):
    path, _ = bridge
    with pytest.raises(TranscribeError) as e:
        UnixTranscriberClient(path).transcribe(b"OggSBOOM")
    assert "library detail" not in str(e.value)


def test_too_long_audio_is_refused(bridge):
    path, _ = bridge
    with pytest.raises(TranscribeError):
        UnixTranscriberClient(path).transcribe(b"OggSLONG")


def test_client_refuses_oversized_audio_before_connecting(tmp_path):
    with pytest.raises(TranscribeError):
        UnixTranscriberClient(str(tmp_path / "missing.sock")).transcribe(b"x" * (stt.MAX_AUDIO_BYTES + 1))


def test_bridge_rejects_bad_header_and_oversize_claim(bridge):
    path, seen = bridge
    for payload in (b"GET / HTTP/1.1\n", f"SAKU-STT/1 {stt.MAX_AUDIO_BYTES + 1}\n".encode(), b"SAKU-STT/1 abc\n"):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(5)
            s.connect(path)
            s.sendall(payload)
            s.shutdown(socket.SHUT_WR)
            reply = json.loads(s.makefile("rb").readline())
        assert reply["ok"] is False
    assert seen == []


def test_unavailable_bridge_is_a_transcribe_error(tmp_path):
    with pytest.raises(TranscribeError):
        UnixTranscriberClient(str(tmp_path / "nothing.sock"), timeout=1).transcribe(b"OggS")


def test_bridge_socket_refuses_to_replace_a_regular_file(tmp_path):
    p = tmp_path / "stt.sock"
    p.write_text("not a socket")
    with pytest.raises(SystemExit):
        stt_bridge.serve(str(p), lambda a: ("", 0.0))
    assert p.read_text() == "not a socket"


def test_bridge_rejects_when_busy_instead_of_queueing_forever(tmp_path):
    gate = threading.Event()

    def slow(audio):
        gate.wait(5)
        return "ok", 1.0

    path = str(tmp_path / "busy.sock")
    threading.Thread(target=stt_bridge.serve, args=(path, slow), kwargs={"max_active": 1, "busy_timeout": 0.2},
                     daemon=True).start()
    for _ in range(100):
        if Path(path).exists():
            break
        time.sleep(0.02)
    first = threading.Thread(target=lambda: UnixTranscriberClient(path).transcribe(b"OggS1"), daemon=True)
    first.start()
    time.sleep(0.2)
    with pytest.raises(TranscribeError):
        UnixTranscriberClient(path, timeout=5).transcribe(b"OggS2")
    gate.set()
    first.join(5)


def test_bridge_socket_directory_is_not_world_listable(bridge):
    path, _ = bridge
    mode = Path(path).parent.stat().st_mode & 0o777
    assert mode & 0o004 == 0 and mode & 0o002 == 0


def test_non_audio_bytes_never_reach_the_decoder(bridge):
    path, seen = bridge
    for junk in (b"%PDF-1.7 not audio", b"MZ\x90\x00 exe", b"<html>"):
        with pytest.raises(TranscribeError):
            UnixTranscriberClient(path).transcribe(junk)
    assert seen == []
