"""Unit tests for meta I/O and finalization (no hardware required).

Covers _atomic_write_json, _write_meta, _write_recording_meta, and
_run_finalization (extraction + transfer) with a lavfi-generated stub MP4.
"""
from __future__ import annotations

import asyncio
import json
import signal
import subprocess
import time
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from monitor import AppContext, AppState, SessionState, _atomic_write_json


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_capturing_ctx(tmp_path: Path, storage_dir: Path | None = None) -> AppContext:
    ctx = AppContext(tmp_path, storage_dir=storage_dir)
    ctx.app_state = AppState.CAPTURING
    ctx.session = SessionState(time.monotonic())
    ctx.session_file = tmp_path / "session_test.mp4"
    ctx.meta_path = tmp_path / "session_test.json"
    ctx.signal_info = (1920, 1080, 60.0, False)
    ctx.started_at = datetime.now()
    return ctx


@pytest.fixture(scope="module")
def stub_mp4(tmp_path_factory) -> Path:
    """10-second H.264 MP4 via ffmpeg lavfi — no hardware, no NVENC."""
    out = tmp_path_factory.mktemp("stub") / "stub.mp4"
    result = subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-y",
            "-f", "lavfi", "-i", "testsrc2=s=320x240:r=30:d=10",
            "-f", "lavfi", "-i", "sine=f=440:r=48000:d=10",
            "-c:v", "libx264", "-preset", "ultrafast",
            "-x264-params", "keyint=30:min-keyint=30",
            "-c:a", "aac", "-b:a", "64k",
            "-movflags", "+faststart",
            str(out),
        ],
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 0, f"ffmpeg lavfi failed:\n{result.stderr.decode()}"
    return out


# ---------------------------------------------------------------------------
# _atomic_write_json
# ---------------------------------------------------------------------------

class TestAtomicWriteJson:
    def test_creates_file(self, tmp_path):
        p = tmp_path / "out.json"
        _atomic_write_json(p, {"k": "v"})
        assert p.exists()

    def test_content_round_trips(self, tmp_path):
        p = tmp_path / "out.json"
        _atomic_write_json(p, {"x": 1, "y": [2, 3]})
        assert json.loads(p.read_text()) == {"x": 1, "y": [2, 3]}

    def test_no_tmp_file_left(self, tmp_path):
        p = tmp_path / "out.json"
        _atomic_write_json(p, {"a": "b"})
        assert not (tmp_path / "out.json.tmp").exists()

    def test_overwrites_existing(self, tmp_path):
        p = tmp_path / "out.json"
        _atomic_write_json(p, {"v": 1})
        _atomic_write_json(p, {"v": 2})
        assert json.loads(p.read_text())["v"] == 2


# ---------------------------------------------------------------------------
# _write_meta
# ---------------------------------------------------------------------------

class TestWriteMeta:
    def test_creates_file(self, tmp_path):
        ctx = _make_capturing_ctx(tmp_path)
        ctx._write_meta()
        assert ctx.meta_path.exists()

    def test_required_fields(self, tmp_path):
        ctx = _make_capturing_ctx(tmp_path)
        ctx._write_meta()
        meta = json.loads(ctx.meta_path.read_text())
        for f in ("session_file", "started_at", "width", "height", "fps",
                  "interlaced", "segments", "extractions", "warnings", "aborted"):
            assert f in meta, f"missing field: {f}"

    def test_signal_info(self, tmp_path):
        ctx = _make_capturing_ctx(tmp_path)
        ctx._write_meta()
        meta = json.loads(ctx.meta_path.read_text())
        assert meta["width"] == 1920
        assert meta["height"] == 1080
        assert meta["fps"] == 60.0
        assert meta["interlaced"] is False

    def test_not_aborted_by_default(self, tmp_path):
        ctx = _make_capturing_ctx(tmp_path)
        ctx._write_meta()
        assert json.loads(ctx.meta_path.read_text())["aborted"] is False

    def test_aborted_flag(self, tmp_path):
        ctx = _make_capturing_ctx(tmp_path)
        ctx._write_meta(aborted=True)
        assert json.loads(ctx.meta_path.read_text())["aborted"] is True

    def test_extractions_written(self, tmp_path):
        ctx = _make_capturing_ctx(tmp_path)
        ex = [{"in": 1.0, "out": 5.0, "status": "pending"}]
        ctx._write_meta(extractions=ex)
        assert json.loads(ctx.meta_path.read_text())["extractions"] == ex

    def test_segments_rounded(self, tmp_path):
        ctx = _make_capturing_ctx(tmp_path)
        ctx.session.segments.append((1.0001, 5.9999))
        ctx._write_meta()
        seg = json.loads(ctx.meta_path.read_text())["segments"][0]
        assert seg["in"] == 1.0
        assert seg["out"] == 6.0


# ---------------------------------------------------------------------------
# _write_recording_meta
# ---------------------------------------------------------------------------

class TestWriteRecordingMeta:
    def test_creates_json_alongside_mp4(self, tmp_path):
        ctx = _make_capturing_ctx(tmp_path)
        output = tmp_path / "recording_test.mp4"
        output.touch()
        ctx._write_recording_meta(output, 2.0, 7.0)
        assert output.with_suffix(".json").exists()

    def test_duration(self, tmp_path):
        ctx = _make_capturing_ctx(tmp_path)
        output = tmp_path / "recording_test.mp4"
        output.touch()
        ctx._write_recording_meta(output, 2.0, 7.0)
        meta = json.loads(output.with_suffix(".json").read_text())
        assert meta["start"] == 2.0
        assert meta["end"] == 7.0
        assert meta["duration"] == pytest.approx(5.0)

    def test_session_name(self, tmp_path):
        ctx = _make_capturing_ctx(tmp_path)
        output = tmp_path / "recording_test.mp4"
        output.touch()
        ctx._write_recording_meta(output, 2.0, 7.0)
        meta = json.loads(output.with_suffix(".json").read_text())
        assert meta["session"] == "session_test.mp4"


# ---------------------------------------------------------------------------
# _run_finalization
# ---------------------------------------------------------------------------

class TestRunFinalization:
    def _make_ctx(self, tmp_path: Path, stub_mp4: Path) -> tuple[AppContext, Path]:
        ctx = AppContext(tmp_path)
        ctx.app_state = AppState.CAPTURING
        ctx.session = SessionState(time.monotonic())
        ctx.session.segments.append((2.0, 6.0))
        # Symlink stub into tmp_path so make_recording_path puts recordings here too.
        session_link = tmp_path / "session_stub.mp4"
        session_link.symlink_to(stub_mp4)
        ctx.session_file = session_link
        ctx.meta_path = tmp_path / "session_stub.json"
        ctx.signal_info = (320, 240, 30.0, False)
        ctx.started_at = datetime.now()
        return ctx, ctx.meta_path

    async def test_extraction_produces_file(self, tmp_path, stub_mp4):
        ctx, _ = self._make_ctx(tmp_path, stub_mp4)
        with patch.object(ctx, "stop_ffmpeg", new_callable=AsyncMock):
            await ctx._run_finalization()
        assert len(list(tmp_path.glob("*_starting_*.mp4"))) == 1

    async def test_meta_status_done(self, tmp_path, stub_mp4):
        ctx, meta_path = self._make_ctx(tmp_path, stub_mp4)
        with patch.object(ctx, "stop_ffmpeg", new_callable=AsyncMock):
            await ctx._run_finalization()
        meta = json.loads(meta_path.read_text())
        assert meta["extractions"][0]["status"] == "done"

    async def test_local_copy_retained(self, tmp_path, stub_mp4):
        # Transfer is now manual — finalization must not remove the local file.
        ctx, meta_path = self._make_ctx(tmp_path, stub_mp4)
        with patch.object(ctx, "stop_ffmpeg", new_callable=AsyncMock):
            await ctx._run_finalization()
        meta = json.loads(meta_path.read_text())
        local = tmp_path / Path(meta["extractions"][0]["output"]).name
        assert local.exists()

    async def test_state_stays_finalizing_until_dismissed(self, tmp_path, stub_mp4):
        ctx, _ = self._make_ctx(tmp_path, stub_mp4)
        ctx.app_state = AppState.FINALIZING
        with patch.object(ctx, "stop_ffmpeg", new_callable=AsyncMock):
            await ctx._run_finalization()

        assert ctx._finalize_progress["step"] == "done"
        # Multiple polls all return FINALIZING — user must dismiss explicitly.
        for _ in range(3):
            s = ctx.status_dict()
            assert s["state"] == "FINALIZING" and s["step"] == "done"
        # Dismiss transitions to INDEX.
        ctx._reset_capture()
        ctx.app_state = AppState.INDEX
        assert ctx.status_dict()["state"] == "INDEX"

    def _make_ctx_empty_session(self, tmp_path: Path, filename: str = "session_empty.mp4") -> AppContext:
        """Ctx with a zero-byte session file so ffprobe fails (no moov)."""
        ctx = AppContext(tmp_path)
        ctx.app_state = AppState.FINALIZING
        ctx.session = SessionState(time.monotonic())
        ctx.session.segments.append((2.0, 6.0))
        empty = tmp_path / filename
        empty.touch()
        ctx.session_file = empty
        ctx.meta_path = tmp_path / empty.with_suffix(".json").name
        ctx.signal_info = (320, 240, 30.0, False)
        ctx.started_at = datetime.now()
        return ctx

    async def test_incomplete_session_sets_error_step(self, tmp_path):
        """Zero-byte session (no moov) → ffprobe fails → step=error, no extraction."""
        ctx = self._make_ctx_empty_session(tmp_path)
        with patch.object(ctx, "stop_ffmpeg", new_callable=AsyncMock):
            await ctx._run_finalization()

        assert ctx._finalize_progress["step"] == "error"
        assert "unreadable" in ctx._finalize_progress.get("error", "").lower()
        meta = json.loads(ctx.meta_path.read_text())
        assert all(e["status"] == "failed" for e in meta["extractions"])
        # No recording files should have been created.
        assert not list(tmp_path.glob("*_starting_*.mp4"))

    async def test_error_step_stays_finalizing_until_dismissed(self, tmp_path):
        """step=error stays FINALIZING indefinitely; dismissed explicitly → INDEX."""
        ctx = self._make_ctx_empty_session(tmp_path, "session_err.mp4")
        with patch.object(ctx, "stop_ffmpeg", new_callable=AsyncMock):
            await ctx._run_finalization()

        assert ctx._finalize_progress["step"] == "error"
        for _ in range(3):
            s = ctx.status_dict()
            assert s["state"] == "FINALIZING" and s["step"] == "error"
        ctx._reset_capture()
        ctx.app_state = AppState.INDEX
        assert ctx.status_dict()["state"] == "INDEX"


# ---------------------------------------------------------------------------
# stop_ffmpeg
# ---------------------------------------------------------------------------

class TestStopFfmpeg:
    """Unit tests for stop_ffmpeg with mocked subprocess.

    _SIGINT_TIMEOUT and _SIGKILL_TIMEOUT are set to 50 ms so tests run fast
    without changing real-world behaviour in production.
    """

    def _make_ctx(self, tmp_path: Path) -> AppContext:
        ctx = AppContext(tmp_path)
        ctx._SIGINT_TIMEOUT = 0.05
        ctx._SIGKILL_TIMEOUT = 0.05
        return ctx

    def _mock_proc(self, wait_coro_factory) -> MagicMock:
        """Return a mock asyncio.Process with the given wait() coroutine factory."""
        proc = MagicMock()
        proc.returncode = None
        proc.pid = 12345
        proc.wait = wait_coro_factory
        return proc

    async def test_clean_sigint_exit(self, tmp_path):
        """Proc exits immediately after SIGINT — kill() is never called."""
        ctx = self._make_ctx(tmp_path)

        async def _wait():
            return 0  # exits right away

        ctx.proc = self._mock_proc(_wait)
        await ctx.stop_ffmpeg()

        ctx.proc.send_signal.assert_called_once_with(signal.SIGINT)
        ctx.proc.kill.assert_not_called()

    async def test_sigkill_fallback(self, tmp_path):
        """Proc ignores SIGINT (SIGINT timeout fires) — kill() is called once, then proc exits."""
        ctx = self._make_ctx(tmp_path)
        _calls = [0]

        async def _wait():
            _calls[0] += 1
            if _calls[0] == 1:
                await asyncio.sleep(9999)  # hangs on first call → SIGINT timeout fires
            # second call (after kill) returns immediately

        ctx.proc = self._mock_proc(_wait)
        await ctx.stop_ffmpeg()

        ctx.proc.send_signal.assert_called_once_with(signal.SIGINT)
        ctx.proc.kill.assert_called_once()

    async def test_dstate_both_timeouts_fire(self, tmp_path):
        """D-state sim: both SIGINT and SIGKILL timeouts expire, stop_ffmpeg still returns.

        This was the original hang bug — before the inner TimeoutError was caught,
        stop_ffmpeg would raise and finalization would never run.
        """
        ctx = self._make_ctx(tmp_path)

        async def _wait():
            await asyncio.sleep(9999)  # never exits (D-state)

        ctx.proc = self._mock_proc(_wait)

        t0 = time.monotonic()
        await ctx.stop_ffmpeg()
        elapsed = time.monotonic() - t0

        # Should return promptly after both short timeouts, not block forever.
        assert elapsed < 1.0, f"stop_ffmpeg blocked for {elapsed:.2f}s despite short timeouts"
        ctx.proc.kill.assert_called_once()
