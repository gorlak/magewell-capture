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
_PORT_SHUTDOWN   = 8294
_PORT_MOOV       = 8296
_PORT_CYCLE      = 8298
_PORT_FINALIZING = 8300


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


def _wait_for_finalizing_done(port: int, timeout: float = 60.0) -> bool:
    """Return True once FINALIZING reaches step=done or step=error."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            data = _http_get_json(port, "/api/status")
            if data.get("state") == "FINALIZING" and data.get("step") in ("done", "error"):
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
    # Fragmented MP4: moov header duration is 0 (data lives in moof/mdat boxes),
    # so format.duration may be "N/A".  Verify content via keyframe timestamps.
    duration_str = info.get("format", {}).get("duration", "N/A")
    if duration_str not in ("N/A", ""):
        assert float(duration_str) >= 2.0, f"session too short: {duration_str}s"
    else:
        kf = subprocess.run(
            ["ffprobe", "-v", "error",
             "-select_streams", "v:0",
             "-show_entries", "packet=pts_time,flags",
             "-of", "csv=p=0", str(sessions[0])],
            capture_output=True, text=True,
        )
        key_times = [
            float(line.split(",")[0])
            for line in kf.stdout.splitlines()
            if ",K" in line and line.split(",")[0] not in ("N/A", "")
        ]
        assert key_times, "no keyframes found in session file"
        assert max(key_times) >= 2.0, (
            f"last keyframe at {max(key_times):.1f}s — session too short"
        )


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

        # Wait for FINALIZING to reach "done", then dismiss to return to INDEX.
        assert _wait_for_finalizing_done(_PORT_CYCLE, timeout=60.0), (
            "did not reach FINALIZING step=done after extraction"
        )
        assert _http_post(_PORT_CYCLE, "/api/dismiss") == 200

        assert _wait_for_state(_PORT_CYCLE, "INDEX", timeout=10.0), (
            "did not return to INDEX after dismiss"
        )

        # Verify the recording appears in the /api/status listing while server is
        # still running.  The listing goes through _RECORDING_NAME_RE; if that regex
        # doesn't match the filename produced by make_recording_path the file exists
        # on disk but is invisible to the UI.
        status = _http_get_json(_PORT_CYCLE, "/api/status")
        assert status["state"] == "INDEX"
        assert len(status.get("recordings", [])) == 1, (
            f"recording not in /api/status listing — _RECORDING_NAME_RE mismatch? "
            f"files on disk: {list((tmp_path).glob('session_*_starting_*.mp4'))}"
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

    # session_????????_??????.json matches only the session meta, not recording
    # metas (session_..._1_starting_5s.json) that glob("session_*.json") also picks up.
    sessions = list(tmp_path.glob("session_????????_??????.json"))
    assert sessions, f"no session meta in {tmp_path}"
    meta = json.loads(sessions[0].read_text())
    assert meta["extractions"][0]["status"] == "done"


@pytest.mark.virtual_device
def test_synthetic_finalizing_dismiss(tmp_path):
    """FINALIZING stays visible after done; /api/dismiss returns to INDEX.

    Guards that the page is not auto-dismissed — the server must stay in
    FINALIZING+done until the client explicitly POSTs /api/dismiss.
    """
    proc = _start_synthetic(tmp_path, _PORT_FINALIZING)
    try:
        assert _wait_for_http(_PORT_FINALIZING), "server did not start"
        assert _http_post(_PORT_FINALIZING, "/api/start") == 200
        time.sleep(1.5)

        assert _http_post(_PORT_FINALIZING, "/api/mark-in?t=0.5") == 200
        time.sleep(2.0)
        assert _http_post(_PORT_FINALIZING, "/api/mark-out?t=2.5") == 200
        assert _http_post(_PORT_FINALIZING, "/api/complete") == 200

        assert _wait_for_finalizing_done(_PORT_FINALIZING, timeout=60.0), (
            "did not reach FINALIZING step=done"
        )

        # State must remain FINALIZING (not auto-switch to INDEX) across several polls.
        for _ in range(3):
            data = _http_get_json(_PORT_FINALIZING, "/api/status")
            assert data.get("state") == "FINALIZING" and data.get("step") == "done", (
                f"expected FINALIZING+done, got {data.get('state')}+{data.get('step')}"
            )
            time.sleep(0.3)

        # Dismiss transitions to INDEX.
        assert _http_post(_PORT_FINALIZING, "/api/dismiss") == 200
        assert _wait_for_state(_PORT_FINALIZING, "INDEX", timeout=5.0), (
            "did not reach INDEX after dismiss"
        )
    finally:
        if proc.returncode is None:
            proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(timeout=15.0)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
