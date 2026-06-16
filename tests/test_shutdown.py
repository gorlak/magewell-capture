"""Integration tests for monitor.py shutdown behaviour.

Requires the Magewell capture hardware (/dev/video0 V4L2, hw:CARD=HDMI ALSA)
and NVENC (T400 or similar).  These are hardware-in-loop tests that only run
on the dedicated capture machine.

Run with:
    pytest tests/test_shutdown.py -v -s
"""
from __future__ import annotations

import base64
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
PYTHON = ROOT / ".venv/bin/python"
MONITOR = ROOT / "scripts/monitor.py"

# Use non-default ports so tests don't conflict with a running instance on
# 8090/8091.  Each test gets its own pair to avoid TIME_WAIT races.
_HTTP_PORT_1 = 8290   # test 1: WS on 8291
_HTTP_PORT_2 = 8292   # test 2: WS on 8293


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _wait_for_http(port: int, timeout: float = 20.0) -> bool:
    """Poll /api/status until the server responds, or until timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            urllib.request.urlopen(
                f"http://localhost:{port}/api/status", timeout=1.0
            )
            return True
        except Exception:
            time.sleep(0.25)
    return False


def _http_post(port: int, path: str) -> int:
    req = urllib.request.Request(
        f"http://localhost:{port}{path}",
        data=b"",
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5.0) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


def _connect_stubborn_ws(host: str, port: int, timeout: float = 10.0) -> socket.socket:
    """Open a WebSocket connection via a raw socket that never sends a Close frame.

    Simulates a browser tab: completes the HTTP Upgrade handshake but does not
    respond to the server's Close frame, keeping the TCP connection open.
    Incoming frames are drained so the server's send buffer never fills (a full
    buffer would cause the server to drop the connection, which would make
    ws_server.wait_closed() return quickly and hide the bug we're testing).
    """
    deadline = time.monotonic() + timeout
    sock: socket.socket | None = None
    while True:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(min(2.0, max(0.1, deadline - time.monotonic())))
            sock.connect((host, port))
            break
        except (ConnectionRefusedError, OSError):
            if sock is not None:
                sock.close()
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.2)

    key = base64.b64encode(os.urandom(16)).decode()
    sock.sendall((
        f"GET / HTTP/1.1\r\nHost: {host}:{port}\r\n"
        f"Upgrade: websocket\r\nConnection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
    ).encode())

    sock.settimeout(min(5.0, max(0.1, deadline - time.monotonic())))
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            break
        buf += chunk
    sock.settimeout(None)

    assert b"101" in buf, (
        f"WebSocket upgrade failed:\n{buf[:300].decode(errors='replace')}"
    )

    # Drain incoming video frames in a daemon thread so the server never sees
    # a full buffer.  We never send a Close frame — that's the whole point.
    def _drain() -> None:
        try:
            while sock.recv(65536):
                pass
        except Exception:
            pass

    threading.Thread(target=_drain, daemon=True).start()
    return sock


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.device
def test_shutdown_exits_within_7s_with_ws_client_connected(tmp_path):
    """After SIGTERM, monitor.py must exit within 7 s even when a stubborn
    WebSocket client (one that never sends a Close frame — like a browser tab)
    is holding the connection open.

    Without the fix: ws_server.wait_closed() blocks until the websockets
    library's internal close_timeout expires (~10 s) or indefinitely.
    With the fix: asyncio.timeout(3.0) fires and we proceed.
    """
    proc = subprocess.Popen(
        [str(PYTHON), str(MONITOR),
         "--output-dir", str(tmp_path),
         "--port", str(_HTTP_PORT_1)],
        stderr=subprocess.PIPE,
    )
    ws_sock: socket.socket | None = None
    try:
        assert _wait_for_http(_HTTP_PORT_1, timeout=20.0), (
            "monitor.py HTTP server did not become ready within 20 s"
        )

        status = _http_post(_HTTP_PORT_1, "/api/start")
        assert status == 200, f"POST /api/start returned {status}"
        ws_sock = _connect_stubborn_ws("127.0.0.1", _HTTP_PORT_1 + 1, timeout=10.0)
        time.sleep(2.0)  # give ffmpeg time to start and push initial fMP4 frames

        proc.send_signal(signal.SIGTERM)

        try:
            proc.wait(timeout=7.0)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            try:
                stderr = proc.stderr.read().decode(errors="replace")
            except Exception:
                stderr = "(unavailable)"
            pytest.fail(
                "monitor.py did not exit within 7 s after SIGTERM — "
                "ws_server.wait_closed() hang?\n\nstderr:\n" + stderr
            )
    finally:
        if ws_sock is not None:
            try:
                ws_sock.close()
            except Exception:
                pass
        if proc.returncode is None:
            proc.kill()
            proc.wait()


@pytest.mark.device
def test_session_file_has_valid_moov_after_shutdown(tmp_path):
    """After SIGTERM + clean exit the session file must have a valid moov atom
    with both video and audio streams, and duration ≥ 8 s.

    Without the pipe-drain fix: ffmpeg's single I/O thread stalls on the full
    64 KB pipe buffer and never writes the moov atom.  The original 15 s timeout
    then fires, SIGKILL is sent, and the file is permanently unrecoverable.
    With the fix: pipe_task is cancelled immediately, drain_task keeps the pipe
    empty, and ffmpeg can write the moov before exiting gracefully.
    """
    proc = subprocess.Popen(
        [str(PYTHON), str(MONITOR),
         "--output-dir", str(tmp_path),
         "--port", str(_HTTP_PORT_2)],
        stderr=subprocess.PIPE,
    )
    try:
        assert _wait_for_http(_HTTP_PORT_2, timeout=20.0), (
            "monitor.py HTTP server did not become ready within 20 s"
        )

        status = _http_post(_HTTP_PORT_2, "/api/start")
        assert status == 200, f"POST /api/start returned {status}"
        time.sleep(10.0)  # record 10 s of content

        proc.send_signal(signal.SIGTERM)

        try:
            proc.wait(timeout=150.0)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            pytest.fail("monitor.py did not exit within 150 s after SIGTERM")
    finally:
        if proc.returncode is None:
            proc.kill()
            proc.wait()

    session_files = list(tmp_path.glob("session_*.mp4"))
    assert session_files, f"No session_*.mp4 found in {tmp_path}"
    session_file = session_files[0]

    result = subprocess.run(
        ["ffprobe", "-v", "error",
         "-show_entries", "format=duration:stream=codec_type,codec_name",
         "-of", "json", str(session_file)],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, (
        "ffprobe failed — session file is likely missing its moov atom.\n"
        f"ffprobe stderr:\n{result.stderr}"
    )

    info = json.loads(result.stdout)
    codec_types = {s["codec_type"] for s in info.get("streams", [])}
    assert "video" in codec_types, (
        "No video stream in session file — NVENC may have failed silently.\n"
        f"Streams: {info.get('streams')}"
    )
    assert "audio" in codec_types, (
        f"No audio stream in session file.\nStreams: {info.get('streams')}"
    )

    duration = float(info["format"]["duration"])
    assert duration >= 8.0, (
        f"Session file too short: {duration:.1f}s (expected ≥ 8 s)"
    )
