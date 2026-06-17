"""Tier 1 tests for AppContext background tasks.

Tests the stall detector and unexpected-ffmpeg-exit handler in isolation.
No hardware, no real ffmpeg process.
"""
from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from monitor import AppContext, AppState, SessionState


# ---------------------------------------------------------------------------
# Shared fixture
# ---------------------------------------------------------------------------

def _make_capturing_ctx(tmp_path: Path) -> AppContext:
    """AppContext pre-set to CAPTURING (no real ffmpeg)."""
    ctx = AppContext(tmp_path)
    ctx.app_state = AppState.CAPTURING
    ctx.session = SessionState(time.monotonic())
    ctx.session_file = tmp_path / "session_test.mp4"
    ctx.meta_path = tmp_path / "session_test.json"
    ctx.signal_info = (1920, 1080, 60.0, False)
    ctx.started_at = datetime.now()
    return ctx


# ---------------------------------------------------------------------------
# Stall detector helpers
# ---------------------------------------------------------------------------

async def _run_stall(ctx: AppContext, n_sleeps: int, disk_free: int = 500 * 10**9) -> None:
    """Run _stall_detector with instant sleeps; cancel after n_sleeps calls.

    fast_sleep does NOT await anything — an async function without an await
    point is valid and runs as a synchronous step inside the outer coroutine.
    Avoiding a nested await prevents the patch from being applied recursively.

    _stall_detector catches CancelledError internally, so awaiting it here
    returns normally (no exception to catch).
    """
    call = 0

    async def fast_sleep(_t: float) -> None:
        nonlocal call
        call += 1
        if call >= n_sleeps:
            raise asyncio.CancelledError

    with patch("monitor.asyncio.sleep", fast_sleep):
        with patch("monitor.shutil.disk_usage") as mock_du:
            mock_du.return_value = MagicMock(free=disk_free, total=512 * 10**9)
            await ctx._stall_detector()


# ---------------------------------------------------------------------------
# Stall detector tests
# ---------------------------------------------------------------------------

class TestStallDetector:
    async def test_zero_byte_file_after_settling_warns(self, tmp_path):
        ctx = _make_capturing_ctx(tmp_path)
        ctx.session_file.touch()  # 0 bytes — never written

        # 2 sleeps: settling (5 s) + 1 loop iteration before cancel
        await _run_stall(ctx, n_sleeps=2)

        assert any("not growing after 5s" in w for w in ctx.session.warnings)

    async def test_static_file_warns_stalled(self, tmp_path):
        ctx = _make_capturing_ctx(tmp_path)
        ctx.session_file.write_bytes(b"\x00" * (5 * 1024 * 1024))  # 5 MB, never grows

        # 3 sleeps: settling + 1 loop (stall fires) + 1 cancel
        await _run_stall(ctx, n_sleeps=3)

        assert any("stalled at 5 MB" in w for w in ctx.session.warnings)

    async def test_growing_file_does_not_warn_stalled(self, tmp_path):
        ctx = _make_capturing_ctx(tmp_path)
        ctx.session_file.write_bytes(b"\x00" * (10 * 1024 * 1024))

        call = 0

        async def growing_sleep(_t: float) -> None:
            nonlocal call
            call += 1
            size = ctx.session_file.stat().st_size
            ctx.session_file.write_bytes(b"\x00" * (size + 5 * 1024 * 1024))
            if call >= 4:
                raise asyncio.CancelledError

        with patch("monitor.asyncio.sleep", growing_sleep):
            with patch("monitor.shutil.disk_usage") as mock_du:
                mock_du.return_value = MagicMock(free=500 * 10**9, total=512 * 10**9)
                await ctx._stall_detector()

        assert not any("stalled" in w for w in ctx.session.warnings)

    async def test_low_disk_warns(self, tmp_path):
        ctx = _make_capturing_ctx(tmp_path)
        ctx.session_file.write_bytes(b"\x00" * (10 * 1024 * 1024))

        call = 0

        async def growing_sleep(_t: float) -> None:
            nonlocal call
            call += 1
            size = ctx.session_file.stat().st_size
            ctx.session_file.write_bytes(b"\x00" * (size + 5 * 1024 * 1024))
            if call >= 3:
                raise asyncio.CancelledError

        # 30 GB free = ~3 h at 10 GB/hr → below 4 h threshold
        with patch("monitor.asyncio.sleep", growing_sleep):
            with patch("monitor.shutil.disk_usage") as mock_du:
                mock_du.return_value = MagicMock(free=30 * 10**9, total=512 * 10**9)
                await ctx._stall_detector()

        assert any("low disk space" in w for w in ctx.session.warnings)
        assert not any("stalled" in w for w in ctx.session.warnings)

    async def test_adequate_disk_does_not_warn(self, tmp_path):
        ctx = _make_capturing_ctx(tmp_path)
        ctx.session_file.write_bytes(b"\x00" * (10 * 1024 * 1024))

        # 200 GB free = 20 h remaining → well above threshold
        await _run_stall(ctx, n_sleeps=3, disk_free=200 * 10**9)

        assert not any("low disk" in w for w in ctx.session.warnings)


# ---------------------------------------------------------------------------
# Unexpected ffmpeg exit tests
# ---------------------------------------------------------------------------

class TestUnexpectedFfmpegExit:
    def _mock_proc(self, returncode: int) -> MagicMock:
        proc = MagicMock()
        proc.returncode = returncode
        proc.wait = AsyncMock(return_value=None)
        return proc

    async def test_unexpected_exit_transitions_to_index(self, tmp_path):
        ctx = _make_capturing_ctx(tmp_path)
        ctx.proc = self._mock_proc(returncode=1)

        await ctx._watch_ffmpeg_exit()

        assert ctx.app_state is AppState.INDEX
        assert ctx.session is None  # reset by _reset_capture

    async def test_unexpected_exit_writes_warning_to_meta(self, tmp_path):
        ctx = _make_capturing_ctx(tmp_path)
        ctx.proc = self._mock_proc(returncode=1)
        meta_path = ctx.meta_path  # save before _reset_capture clears it

        await ctx._watch_ffmpeg_exit()

        meta = json.loads(meta_path.read_text())
        assert meta["aborted"] is True
        assert any("ffmpeg exited unexpectedly" in w for w in meta["warnings"])
        assert any("rc=1" in w for w in meta["warnings"])

    async def test_expected_exit_does_not_transition(self, tmp_path):
        ctx = _make_capturing_ctx(tmp_path)
        ctx.proc = self._mock_proc(returncode=0)
        ctx._stopping_ffmpeg = True  # controlled shutdown in progress

        await ctx._watch_ffmpeg_exit()

        assert ctx.app_state is AppState.CAPTURING  # unchanged

    async def test_unexpected_exit_cancels_background_tasks(self, tmp_path):
        ctx = _make_capturing_ctx(tmp_path)
        ctx.proc = self._mock_proc(returncode=1)

        async def _forever() -> None:
            await asyncio.sleep(9999)

        # Attach real tasks and save references before _reset_capture clears them.
        pipe_task   = asyncio.create_task(_forever())
        stderr_task = asyncio.create_task(_forever())
        stall_task  = asyncio.create_task(_forever())
        ctx._pipe_task   = pipe_task
        ctx._stderr_task = stderr_task
        ctx._stall_task  = stall_task

        await ctx._watch_ffmpeg_exit()

        assert pipe_task.cancelled()
        assert stderr_task.cancelled()
        assert stall_task.cancelled()
