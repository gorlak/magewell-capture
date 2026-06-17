"""Unit tests for meta I/O and finalization (no hardware required).

Covers _atomic_write_json, _write_meta, _write_recording_meta, and
_run_finalization (extraction + transfer) with a lavfi-generated stub MP4.
"""
from __future__ import annotations

import json
import subprocess
import time
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from monitor import AppContext, AppState, SessionState, _atomic_write_json


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_capturing_ctx(tmp_path: Path, transfer_dest: Path | None = None) -> AppContext:
    ctx = AppContext(tmp_path, transfer_dest=transfer_dest)
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
        ctx.session_file = stub_mp4
        ctx.meta_path = tmp_path / "session_stub.json"
        ctx.signal_info = (320, 240, 30.0, False)
        ctx.started_at = datetime.now()
        return ctx, ctx.meta_path

    async def test_extraction_produces_file(self, tmp_path, stub_mp4):
        ctx, _ = self._make_ctx(tmp_path, stub_mp4)
        with patch.object(ctx, "stop_ffmpeg", new_callable=AsyncMock):
            await ctx._run_finalization()
        assert len(list(tmp_path.glob("recording_*.mp4"))) == 1

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
        local = tmp_path / meta["extractions"][0]["output"]
        assert local.exists()

    async def test_state_returns_to_index(self, tmp_path, stub_mp4):
        ctx, _ = self._make_ctx(tmp_path, stub_mp4)
        with patch.object(ctx, "stop_ffmpeg", new_callable=AsyncMock):
            await ctx._run_finalization()
        assert ctx.app_state is AppState.INDEX
