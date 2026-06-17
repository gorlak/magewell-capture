"""Virtual-device integration tests (marker: virtual_device).

Run monitor.py with --synthetic (lavfi sources, libx264) so these tests need
no Magewell hardware and no NVENC.  They run in CI.

    pytest -m virtual_device -v -s
"""
from __future__ import annotations

import json
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

ROOT    = Path(__file__).parent.parent
PYTHON  = ROOT / ".venv/bin/python"
MONITOR = ROOT / "scripts/monitor.py"

# Each test gets its own port pair to avoid TIME_WAIT conflicts.
# test_shutdown.py uses 8290/8292 so start above that.
_PORT_SHUTDOWN = 8294
_PORT_MOOV     = 8296
_PORT_CYCLE    = 8298


# ---------------------------------------------------------------------------
# Helpers (duplicated from test_shutdown.py to keep files self-contained)
# ---------------------------------------------------------------------------

def _wait_for_http(port: int, timeout: float = 20.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            urllib.request.urlopen(f"http://localhost:{port}/api/status", timeout=1.0)
            return True
        except Exception:
            time.sleep(0.2)
    return False


def _http_post(port: int, path: str) -> int:
    req = urllib.request.Request(
        f"http://localhost:{port}{path}", data=b"", method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5.0) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


def _http_get_json(port: int, path: str) -> dict:
    with urllib.request.urlopen(f"http://localhost:{port}{path}", timeout=5.0) as r:
        return json.loads(r.read())


def _wait_for_state(port: int, state: str, timeout: float = 60.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            data = _http_get_json(port, "/api/status")
            if data.get("state") == state:
                return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def _start_synthetic(tmp_path: Path, port: int, extra_args: list[str] | None = None) -> subprocess.Popen:
    cmd = [
        str(PYTHON), str(MONITOR),
        "--synthetic",
        "--sessions-dir", str(tmp_path),
        "--port", str(port),
    ]
    if extra_args:
        cmd += extra_args
    return subprocess.Popen(cmd, stderr=subprocess.PIPE)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.virtual_device
def test_synthetic_exits_cleanly_after_sigterm(tmp_path):
    """SIGTERM during CAPTURING: monitor exits within 15 s (synthetic is fast)."""
    proc = _start_synthetic(tmp_path, _PORT_SHUTDOWN)
    try:
        assert _wait_for_http(_PORT_SHUTDOWN), "server did not start"
        assert _http_post(_PORT_SHUTDOWN, "/api/start") == 200
        time.sleep(2.0)
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=15.0)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            pytest.fail("monitor.py did not exit within 15 s after SIGTERM")
    finally:
        if proc.returncode is None:
            proc.kill()
            proc.wait()


@pytest.mark.virtual_device
def test_synthetic_session_has_valid_moov(tmp_path):
    """After SIGTERM the session file must be a valid MP4 (moov present)."""
    proc = _start_synthetic(tmp_path, _PORT_MOOV)
    try:
        assert _wait_for_http(_PORT_MOOV), "server did not start"
        assert _http_post(_PORT_MOOV, "/api/start") == 200
        time.sleep(3.0)
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=30.0)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            pytest.fail("monitor.py did not exit within 30 s")
    finally:
        if proc.returncode is None:
            proc.kill()
            proc.wait()

    sessions = list(tmp_path.glob("session_*.mp4"))
    assert sessions, f"no session_*.mp4 in {tmp_path}"

    result = subprocess.run(
        ["ffprobe", "-v", "error",
         "-show_entries", "format=duration:stream=codec_type",
         "-of", "json", str(sessions[0])],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, (
        f"ffprobe failed — session file may be missing moov:\n{result.stderr}"
    )
    info = json.loads(result.stdout)
    codec_types = {s["codec_type"] for s in info.get("streams", [])}
    assert "video" in codec_types
    assert "audio" in codec_types
    assert float(info["format"]["duration"]) >= 2.0


@pytest.mark.virtual_device
def test_synthetic_record_extract(tmp_path):
    """Full cycle: start → mark-in → mark-out → complete → verify extraction."""
    proc = _start_synthetic(tmp_path, _PORT_CYCLE)
    try:
        assert _wait_for_http(_PORT_CYCLE), "server did not start"
        assert _http_post(_PORT_CYCLE, "/api/start") == 200
        time.sleep(1.5)                                   # let stream stabilise

        assert _http_post(_PORT_CYCLE, "/api/mark-in?t=0.5") == 200
        time.sleep(2.0)                                   # record ~2 s of content
        assert _http_post(_PORT_CYCLE, "/api/mark-out?t=2.5") == 200

        assert _http_post(_PORT_CYCLE, "/api/complete") == 200

        assert _wait_for_state(_PORT_CYCLE, "INDEX", timeout=60.0), (
            "did not return to INDEX after finalization"
        )
    finally:
        if proc.returncode is None:
            proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(timeout=15.0)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()

    # Recording stays in sessions dir — transfer is now manual via /view page.
    recordings = list(tmp_path.glob("session_*_starting_*.mp4"))
    assert len(recordings) == 1, f"expected 1 recording in {tmp_path}, got {recordings}"

    sessions = list(tmp_path.glob("session_*.json"))
    assert sessions
    meta = json.loads(sessions[0].read_text())
    assert meta["extractions"][0]["status"] == "done"
