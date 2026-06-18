#!/home/geoff/Projects/magewell-capture/.venv/bin/python
"""monitor.py — session lifecycle: INDEX → CAPTURING → FINALIZING → INDEX.

HTTP (:8090) serves the web UI, static files, and JSON API.
WebSocket (:8091) streams fMP4 video during CAPTURING only.

State machine:
    INDEX → CAPTURING      (POST /api/start)
    CAPTURING → FINALIZING (POST /api/complete — background extraction)
    CAPTURING → INDEX      (POST /api/abort)
    FINALIZING → INDEX     (automatic on extraction completion)

See DECISIONS.md for design rationale.

Usage:
    .venv/bin/python scripts/monitor.py [--sessions-dir DIR] [--port PORT]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import shutil
import signal
import socket
import sys
import time
from datetime import datetime, timedelta
from enum import Enum
from http import HTTPStatus
from pathlib import Path

from websockets.asyncio.server import serve, ServerConnection

from capture_shared import (
    SYNTHETIC_SIGNAL,
    build_extract_cmd,
    build_monitor_cmd,
    make_output_path,
    make_recording_path,
    probe_signal,
)

DEFAULT_PORT = 8090
WS_PORT_OFFSET = 1
WEB_DIR = Path(__file__).parent / "web"

_WARN_PATTERNS = [
    re.compile(r"Error while writing output"),
    re.compile(r"Error initializing output stream"),
    re.compile(r"No space left on device"),
    re.compile(r"Conversion failed"),
    re.compile(r"NVENC.+[Ee]rror|[Ee]rror.+NVENC"),
]

_SESSION_NAME_RE   = re.compile(r"session_\d{8}_\d{6}\.mp4")
_RECORDING_NAME_RE = re.compile(r"session_\d{8}_\d{6}_\d+_starting_(?:\d+h)?(?:\d+m)?\d+s\.mp4")
_NAME_RE           = re.compile(r"session_\d{8}_\d{6}(?:_\d+_starting_(?:\d+h)?(?:\d+m)?\d+s)?\.mp4")

# HEVC Main10 CQ21 @1080p60 ≈ 20 Mbps ≈ 10 GB/hr (measured; see DECISIONS.md)
_HEVC_BYTES_PER_HOUR = 10 * 1_000_000_000

_REPO_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_SESSIONS_DIR = _REPO_ROOT / "sessions"
_DEFAULT_STORAGE_DIR = _REPO_ROOT / "storage"


# ---------------------------------------------------------------------------
# State enums
# ---------------------------------------------------------------------------

class AppState(Enum):
    INDEX      = "INDEX"
    CAPTURING  = "CAPTURING"
    FINALIZING = "FINALIZING"


class State(Enum):  # record sub-state within CAPTURING
    STANDBY   = "STANDBY"
    RECORDING = "RECORDING"


# ---------------------------------------------------------------------------
# SessionState — in-CAPTURING mark tracker (API unchanged for existing tests)
# ---------------------------------------------------------------------------

class SessionState:
    """Tracks record in/out points for the current capture session."""

    def __init__(self, start_time: float):
        self.start_time = start_time
        self.state = State.STANDBY
        self.segments: list[tuple[float, float]] = []
        self._current_in: float | None = None
        self._current_in_wall: float | None = None
        self.warnings: list[str] = []

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.start_time

    @property
    def recording_elapsed(self) -> float | None:
        if self._current_in_wall is None:
            return None
        return time.monotonic() - self._current_in_wall

    def mark_in(self, stream_time: float) -> bool:
        if self.state is State.RECORDING:
            return False
        self._current_in = stream_time
        self._current_in_wall = time.monotonic()
        self.state = State.RECORDING
        return True

    def mark_out(self, stream_time: float) -> bool:
        if self.state is not State.RECORDING or self._current_in is None:
            return False
        self.segments.append((self._current_in, stream_time))
        self._current_in = None
        self._current_in_wall = None
        self.state = State.STANDBY
        return True

    def add_warning(self, msg: str) -> None:
        if msg not in self.warnings:
            self.warnings.append(msg)
        print(f"[monitor] WARNING: {msg}", file=sys.stderr)

    def finalize(self, stream_time: float) -> None:
        if self.state is State.RECORDING and self._current_in is not None:
            self.segments.append((self._current_in, stream_time))
            self._current_in = None
            self._current_in_wall = None
            self.state = State.STANDBY

    def to_dict(self) -> dict:
        return {
            "state": self.state.value,
            "elapsed": round(self.elapsed, 1),
            "recording_elapsed": (
                round(self.recording_elapsed, 1)
                if self.recording_elapsed is not None
                else None
            ),
            "segments": [
                {"in": round(s, 3), "out": round(e, 3)}
                for s, e in self.segments
            ],
            "warnings": list(self.warnings),
        }


async def _parse_ffmpeg_progress(
    reader: asyncio.StreamReader,
    rec_duration: float,
    *targets: dict,
) -> None:
    """Consume ffmpeg -progress pipe output and update all target dicts."""
    out_time_us = 0
    speed = 0.0
    async for line_bytes in reader:
        line = line_bytes.decode("utf-8", errors="replace").strip()
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        if key == "out_time_us":
            try:
                out_time_us = max(0, int(val))
            except ValueError:
                pass
        elif key == "speed":
            try:
                speed = float(val.rstrip("x").strip() or "0")
            except ValueError:
                pass
        elif key == "progress":
            if val.strip() == "end":
                for t in targets:
                    t.update({"pct": 100, "speed": None, "eta_s": None})
            else:
                pct = min(99, int(out_time_us / (rec_duration * 1_000_000) * 100)) if rec_duration > 0 else 0
                eta_s: int | None = None
                if speed > 0 and rec_duration > 0:
                    remaining_us = max(0, rec_duration * 1_000_000 - out_time_us)
                    eta_s = round(remaining_us / speed / 1_000_000)
                update = {"pct": pct, "speed": round(speed, 1), "eta_s": eta_s}
                for t in targets:
                    t.update(update)


# ---------------------------------------------------------------------------
# AppContext — global server state and capture lifecycle
# ---------------------------------------------------------------------------

class AppContext:
    """Shared state for the entire server lifetime."""

    # Overridable in tests to avoid 45+10 second waits.
    _SIGINT_TIMEOUT: float = 45.0
    _SIGKILL_TIMEOUT: float = 10.0

    def __init__(
        self, sessions_dir: Path, storage_dir: Path | None = None, synthetic: bool = False,
    ) -> None:
        self.sessions_dir = sessions_dir
        self.storage_dir = storage_dir
        self.synthetic = synthetic
        self.app_state = AppState.INDEX
        self.broadcaster = StreamBroadcaster()
        # set during CAPTURING
        self.session: SessionState | None = None
        self.session_file: Path | None = None
        self.meta_path: Path | None = None
        self.signal_info: tuple[int, int, float, bool] | None = None
        self.started_at: datetime | None = None
        self.proc: asyncio.subprocess.Process | None = None
        self._pipe_task: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None
        self._stall_task: asyncio.Task | None = None
        self._ffmpeg_watcher_task: asyncio.Task | None = None
        self._stopping_ffmpeg: bool = False
        # set during FINALIZING
        self.finalize_task: asyncio.Task | None = None
        self._finalize_progress: dict = {}
        # per-recording transfer state (filename → state dict), persists in memory
        self._transfers: dict[str, dict] = {}

    # ---- status ----

    def status_dict(self) -> dict:
        if self.app_state is AppState.INDEX:
            return {"state": "INDEX", **self._file_listing()}
        if self.app_state is AppState.CAPTURING:
            assert self.session is not None
            d = self.session.to_dict()
            session_state = d.pop("state")
            return {"state": "CAPTURING", "session_state": session_state, **d}
        if self.app_state is AppState.FINALIZING:
            return {"state": "FINALIZING", **self._finalize_progress}
        return {"state": "UNKNOWN"}

    def _file_listing(self) -> dict:
        sessions: list[dict] = []
        recordings: list[dict] = []
        try:
            for p in sorted(self.sessions_dir.glob("*.mp4"), reverse=True):
                size = p.stat().st_size
                if _RECORDING_NAME_RE.fullmatch(p.name):
                    recordings.append({"name": p.name, "size": size})
                elif _SESSION_NAME_RE.fullmatch(p.name):
                    sessions.append({
                        "name": p.name,
                        "size": size,
                        "has_meta": p.with_suffix(".json").exists(),
                    })
        except OSError:
            pass
        disk: dict = {}
        try:
            usage = shutil.disk_usage(self.sessions_dir)
            disk = {"free": usage.free, "total": usage.total}
        except OSError:
            pass
        return {"sessions": sessions, "recordings": recordings, "disk": disk}

    # ---- meta ----

    def session_meta_dict(
        self,
        extractions: list[dict] | None = None,
        aborted: bool = False,
    ) -> dict:
        assert self.session and self.session_file and self.signal_info and self.started_at
        w, h, fps, interlaced = self.signal_info
        return {
            "session_file": self.session_file.name,
            "started_at": self.started_at.strftime("%Y-%m-%dT%H:%M:%S"),
            "width": w,
            "height": h,
            "fps": fps,
            "interlaced": interlaced,
            "segments": [
                {"in": round(s, 3), "out": round(e, 3)}
                for s, e in self.session.segments
            ],
            "extractions": extractions if extractions is not None else [],
            "warnings": list(self.session.warnings),
            "aborted": aborted,
        }

    def _write_meta(self, extractions: list[dict] | None = None, aborted: bool = False) -> None:
        assert self.meta_path
        _atomic_write_json(self.meta_path, self.session_meta_dict(extractions, aborted))

    def _write_recording_meta(
        self, output: Path, start: float, end: float
    ) -> None:
        assert self.session_file and self.signal_info and self.started_at
        w, h, fps, interlaced = self.signal_info
        meta = {
            "session": self.session_file.name,
            "started_at": (self.started_at + timedelta(seconds=start)).strftime("%Y-%m-%dT%H:%M:%S"),
            "start": round(start, 3),
            "end": round(end, 3),
            "duration": round(end - start, 3),
            "width": w,
            "height": h,
            "fps": fps,
            "interlaced": interlaced,
        }
        _atomic_write_json(output.with_suffix(".json"), meta)

    async def _transfer_recording(self, output: Path) -> None:
        """Transfer a recording to storage_dir via rsync (user-triggered).

        Updates self._transfers[output.name] with live progress (pct, rate_mbps)
        and on completion with summary stats (size_mb, elapsed_s, avg_mbps).
        The local file is never removed here — cleanup is via the delete UI.
        """
        dest = self.storage_dir / output.name
        total_bytes = output.stat().st_size
        size_mb = total_bytes / (1024 * 1024)
        start_t = time.monotonic()
        print(f"[transfer] {size_mb:.1f} MB: {output.name} → {dest} …")
        self._transfers[output.name] = {
            "state": "running",
            "source": str(output),
            "destination": str(dest),
            "pct": 0,
            "rate_mbps": 0.0,
        }
        try:
            proc = await asyncio.create_subprocess_exec(
                "rsync", "--inplace", str(output), str(self.storage_dir),
                stderr=asyncio.subprocess.PIPE,
            )

            async def _poll() -> None:
                history: list[tuple[float, int]] = []
                while True:
                    await asyncio.sleep(0.5)
                    try:
                        done = dest.stat().st_size
                    except FileNotFoundError:
                        done = 0
                    now = time.monotonic()
                    history.append((now, done))
                    cutoff = now - 3.0
                    history = [(t, b) for t, b in history if t >= cutoff]
                    if len(history) >= 2:
                        dt = history[-1][0] - history[0][0]
                        db = history[-1][1] - history[0][1]
                        rate = db / dt / (1024 * 1024) if dt > 0 else 0.0
                    else:
                        rate = 0.0
                    self._transfers[output.name].update({
                        "pct": int(done * 100 / total_bytes) if total_bytes else 100,
                        "rate_mbps": round(rate, 1),
                    })

            poll_task = asyncio.create_task(_poll())
            try:
                _, stderr_bytes = await asyncio.wait_for(proc.communicate(), timeout=300.0)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.communicate()
                raise RuntimeError("timed out after 300 s")
            finally:
                poll_task.cancel()

            if proc.returncode != 0:
                err = (stderr_bytes or b"").decode(errors="replace").strip()
                raise RuntimeError(f"rsync exited {proc.returncode}: {err}")

            elapsed = time.monotonic() - start_t
            avg_mbps = size_mb / elapsed if elapsed > 0 else 0.0
            self._transfers[output.name].update({
                "state": "done",
                "pct": 100,
                "size_mb": round(size_mb, 1),
                "elapsed_s": round(elapsed, 1),
                "avg_mbps": round(avg_mbps, 1),
            })
            print(f"[transfer] done — {size_mb:.1f} MB in {elapsed:.1f}s ({avg_mbps:.1f} MB/s)")
        except Exception as exc:
            self._transfers[output.name].update({"state": "failed", "error": str(exc)})
            print(f"[transfer] failed: {exc}", file=sys.stderr)

    # ---- capture lifecycle ----

    async def start_ffmpeg(self) -> None:
        """Probe signal, start ffmpeg, initialise session, → CAPTURING."""
        if self.synthetic:
            width, height, fps, interlaced = SYNTHETIC_SIGNAL
        else:
            width, height, fps, interlaced = probe_signal()
        self.signal_info = (width, height, fps, interlaced)
        self.sessions_dir.mkdir(parents=True, exist_ok=True)

        session_file = make_output_path(self.sessions_dir, prefix="session")
        self.session_file = session_file
        self.meta_path = session_file.with_suffix(".json")
        self.started_at = datetime.now()

        cmd = build_monitor_cmd(width, height, fps, interlaced, session_file, synthetic=self.synthetic)
        print(f"\n[monitor] session file: {session_file}")
        print(f"[monitor] cmd: {' '.join(cmd)}\n")

        self.proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        self.session = SessionState(time.monotonic())
        self._stopping_ffmpeg = False

        self._write_meta()

        self._pipe_task = asyncio.create_task(self._read_pipe())
        self._stderr_task = asyncio.create_task(self._log_stderr())
        self._stall_task = asyncio.create_task(self._stall_detector())
        self._ffmpeg_watcher_task = asyncio.create_task(self._watch_ffmpeg_exit())

        self.app_state = AppState.CAPTURING

    def _set_stop_phase(self, phase: str) -> None:
        if self._finalize_progress.get("step") == "stopping_ffmpeg":
            self._finalize_progress["stop_phase"] = phase

    async def stop_ffmpeg(self) -> None:
        """Send SIGINT, let _pipe_task drain stdout, wait for exit."""
        if self.proc is None or self.proc.returncode is not None:
            return

        # Set flag first: _read_pipe now drains without broadcasting, so there is
        # no gap where stdout goes unread between pipe_task cancel and a drain task
        # start — that gap could fill the pipe buffer and block ffmpeg from exiting.
        self._stopping_ffmpeg = True
        self._set_stop_phase("sigint")

        pid = self.proc.pid
        try:
            self.proc.send_signal(signal.SIGINT)
        except ProcessLookupError:
            pass  # already exited between the returncode check and here

        print(f"[monitor] sent SIGINT to ffmpeg (pid {pid}), waiting for finalization...",
              file=sys.stderr)
        t0 = time.monotonic()
        try:
            await asyncio.wait_for(self.proc.wait(), timeout=self._SIGINT_TIMEOUT)
            elapsed = time.monotonic() - t0
            rc = self.proc.returncode
            print(f"[monitor] ffmpeg exited (rc={rc}) in {elapsed:.1f} s", file=sys.stderr)
        except asyncio.TimeoutError:
            elapsed = time.monotonic() - t0
            print(f"[monitor] ffmpeg (pid {pid}) did not exit after {elapsed:.0f} s, killing...",
                  file=sys.stderr)
            self._set_stop_phase("sigkill")
            self.proc.kill()
            try:
                await asyncio.wait_for(self.proc.wait(), timeout=self._SIGKILL_TIMEOUT)
            except asyncio.TimeoutError:
                # Process is stuck in an unkillable kernel state (e.g. V4L2/ALSA close).
                # Proceed with finalization — the orphaned process will be cleaned up
                # when the kernel call eventually unblocks or the device is reset.
                self._set_stop_phase("zombie")
                print(f"[monitor] ffmpeg (pid {pid}) still running after SIGKILL; proceeding",
                      file=sys.stderr)
        finally:
            # _pipe_task exits naturally on EOF when ffmpeg closes stdout.
            # Cancel it here to handle abnormal exits (kill, crash) where EOF may
            # not arrive promptly.
            if self._pipe_task:
                self._pipe_task.cancel()
                await asyncio.gather(self._pipe_task, return_exceptions=True)
                self._pipe_task = None

        for task in [self._stderr_task, self._stall_task, self._ffmpeg_watcher_task]:
            if task:
                task.cancel()
        tasks = [t for t in [self._stderr_task, self._stall_task, self._ffmpeg_watcher_task] if t]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._stderr_task = self._stall_task = self._ffmpeg_watcher_task = None

    async def complete_capture(self) -> dict:
        """Close any open segment, → FINALIZING. Raises ValueError if no segments."""
        assert self.app_state is AppState.CAPTURING and self.session
        self.session.finalize(self.session.elapsed)
        if not self.session.segments:
            raise ValueError("no recordings marked")

        total = len(self.session.segments)
        try:
            session_mb = round(self.session_file.stat().st_size / (1024 * 1024))
        except OSError:
            session_mb = None

        w, h, fps, _ = self.signal_info
        signal_str = f"{w}×{h} @ {fps:.0f} fps"
        recordings_init = [
            {"index": i, "status": "pending",
             "start": round(s, 1), "end": round(e, 1), "duration": round(e - s, 1)}
            for i, (s, e) in enumerate(self.session.segments)
        ]
        self._finalize_progress = {
            "step": "stopping_ffmpeg",
            "total": total,
            "started_at": time.time(),
            "session_mb": session_mb,
            "session": self.session_file.name,
            "signal": signal_str,
            "recordings": recordings_init,
        }
        self.app_state = AppState.FINALIZING
        self.finalize_task = asyncio.create_task(self._run_finalization())
        return self.status_dict()

    async def abort_capture(self) -> None:
        """Stop ffmpeg immediately, → INDEX. Session files kept on disk."""
        assert self.app_state is AppState.CAPTURING
        await self.stop_ffmpeg()
        try:
            self._write_meta(aborted=True)
        except Exception:
            pass
        self._reset_capture()
        self.app_state = AppState.INDEX

    async def _run_finalization(self) -> None:
        """Background task: stop ffmpeg, extract all segments, stay in FINALIZING."""
        assert self.session and self.session_file and self.signal_info and self.meta_path

        segments = list(self.session.segments)
        session_file = self.session_file

        await self.stop_ffmpeg()

        extractions = [
            {"in": round(s, 3), "out": round(e, 3),
             "output": None, "status": "pending"}
            for s, e in segments
        ]
        self._write_meta(extractions=extractions)

        total = len(segments)
        # Live per-recording list seeded by complete_capture(); update it in-place.
        recordings = self._finalize_progress.get("recordings", [])

        # Verify the session file is readable before attempting extraction.
        # The session file is written as fragmented MP4 (empty_moov + moof/mdat
        # per keyframe), so it is always valid even after SIGKILL.  A non-zero
        # ffprobe result here indicates a truly corrupt file: zero bytes written
        # (disk full, immediate crash), or a wrong path.
        _probe = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1", str(session_file),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        _, _probe_err = await _probe.communicate()
        if _probe.returncode != 0:
            print(
                f"[monitor] session file is unreadable — extraction skipped: "
                f"{_probe_err.decode(errors='replace').strip()}",
                file=sys.stderr,
            )
            for extraction in extractions:
                extraction["status"] = "failed"
            for rec in recordings:
                rec["status"] = "failed"
            self._write_meta(extractions=extractions)
            self._finalize_progress.update({
                "step": "error",
                "error": "Session file is unreadable (possibly zero bytes — disk full?). "
                         "Recordings cannot be extracted.",
            })
            return

        self._finalize_progress.update({"step": "extracting"})

        for i, (start, end) in enumerate(segments):
            rec_duration = end - start
            rec = recordings[i] if i < len(recordings) else {}
            rec["status"] = "extracting"
            self._finalize_progress.update({
                "recording": i + 1,
                "pct": 0, "speed": None, "eta_s": None,
            })
            output = make_recording_path(session_file, i + 1, start)
            print(f"[monitor] extracting {i+1}/{total}: "
                  f"{start:.1f}s – {end:.1f}s ({rec_duration:.1f}s) → {output.name}")

            cmd = build_extract_cmd(session_file, start, end, output)
            cmd = cmd[:-1] + ["-progress", "pipe:1"] + [cmd[-1]]

            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stderr_bytes, _ = await asyncio.gather(
                proc.stderr.read(),
                _parse_ffmpeg_progress(proc.stdout, rec_duration,
                                        self._finalize_progress, rec),
            )
            await proc.wait()

            if proc.returncode == 0:
                size_mb = output.stat().st_size / (1024 * 1024)
                print(f"  → {output.name} ({size_mb:.1f} MB)")
                extractions[i].update({"output": output.name, "status": "done"})
                rec.update({"status": "done", "pct": 100,
                             "speed": None, "eta_s": None, "output": output.name})
                try:
                    self._write_recording_meta(output, start, end)
                except Exception:
                    pass
            else:
                print(f"  → extraction failed (rc={proc.returncode})", file=sys.stderr)
                if stderr_bytes:
                    print(f"  {stderr_bytes.decode(errors='replace').strip()}", file=sys.stderr)
                extractions[i]["status"] = "failed"
                rec["status"] = "failed"

            self._write_meta(extractions=extractions)

            if i < total - 1:
                await asyncio.sleep(1.1)

        # Preserve session/signal context already in _finalize_progress; just flip step.
        for key in ("pct", "speed", "eta_s", "recording"):
            self._finalize_progress.pop(key, None)

        # Remux session file from fragmented MP4 to regular MP4 so browsers can seek
        # it like a recording file.  The fragmented format is only needed during
        # capture (SIGKILL resilience); once ffmpeg has exited we don't need it.
        # Peak disk usage: 2× session file size during the copy; drops back to 1× on rename.
        self._finalize_progress["step"] = "remuxing_session"
        remux_tmp = session_file.with_suffix(".remux.mp4")
        try:
            remux_proc = await asyncio.create_subprocess_exec(
                "ffmpeg", "-hide_banner",
                "-i", str(session_file),
                "-c", "copy",
                str(remux_tmp),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, remux_err = await remux_proc.communicate()
            if remux_proc.returncode == 0:
                remux_tmp.replace(session_file)
                print(f"[monitor] session file remuxed to regular MP4: {session_file.name}",
                      file=sys.stderr)
            else:
                remux_tmp.unlink(missing_ok=True)
                print(f"[monitor] session remux failed (rc={remux_proc.returncode}); "
                      f"keeping original fragmented file", file=sys.stderr)
                if remux_err:
                    print(f"  {remux_err.decode(errors='replace').strip()}", file=sys.stderr)
        except Exception as exc:
            remux_tmp.unlink(missing_ok=True)
            print(f"[monitor] session remux error: {exc}; keeping original", file=sys.stderr)

        self._finalize_progress["step"] = "done"
        # State stays FINALIZING; user dismisses via /api/dismiss to return to INDEX.

    def _reset_capture(self) -> None:
        self.session = None
        self.session_file = None
        self.meta_path = None
        self.signal_info = None
        self.started_at = None
        self.proc = None
        self._pipe_task = None
        self._stderr_task = None
        self._stall_task = None
        self._ffmpeg_watcher_task = None
        self._stopping_ffmpeg = False
        self.finalize_task = None

    # ---- background task coroutines ----

    async def _read_pipe(self) -> None:
        assert self.proc and self.proc.stdout
        try:
            while True:
                chunk = await self.proc.stdout.read(65536)
                if not chunk:
                    break
                if not self._stopping_ffmpeg:
                    await self.broadcaster.feed(chunk)
        except asyncio.CancelledError:
            pass

    async def _log_stderr(self) -> None:
        assert self.proc and self.proc.stderr
        try:
            while True:
                try:
                    line = await self.proc.stderr.readline()
                except asyncio.LimitOverrunError:
                    # ffmpeg progress updates use \r (not \n); they accumulate in
                    # the 64 KB StreamReader buffer and overflow it on long captures.
                    # Drain in chunks until we find \n or EOF, then resume readline().
                    while True:
                        chunk = await self.proc.stderr.read(65536)
                        if not chunk:
                            return  # EOF while draining
                        if b'\n' in chunk:
                            break
                    continue
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").rstrip()
                if not text:
                    continue
                print(f"[ffmpeg] {text}")
                if self.session:
                    for pat in _WARN_PATTERNS:
                        if pat.search(text):
                            self.session.add_warning(text)
                            break
        except asyncio.CancelledError:
            pass

    async def _stall_detector(self) -> None:
        try:
            await asyncio.sleep(5.0)
            assert self.session_file
            try:
                prev_size = self.session_file.stat().st_size
            except OSError:
                prev_size = 0
            if prev_size == 0 and self.session:
                self.session.add_warning(
                    "session file not growing after 5s settling — "
                    "possible encoder or disk error"
                )
            stall_warned = False
            disk_warned = False
            while True:
                await asyncio.sleep(10.0)
                try:
                    current_size = self.session_file.stat().st_size
                except OSError:
                    current_size = 0
                if current_size > prev_size:
                    prev_size = current_size
                    stall_warned = False
                elif not stall_warned and self.session:
                    stall_warned = True
                    self.session.add_warning(
                        f"session file stalled at "
                        f"{current_size // (1024 * 1024)} MB — "
                        "possible encoder or mux error"
                    )
                if not disk_warned and self.session:
                    try:
                        usage = shutil.disk_usage(self.sessions_dir)
                        hours_remaining = usage.free / _HEVC_BYTES_PER_HOUR
                        if hours_remaining < 4.0:
                            disk_warned = True
                            self.session.add_warning(
                                f"low disk space: ~{hours_remaining:.1f} h remaining "
                                f"(HEVC @20 Mbps)"
                            )
                    except OSError:
                        pass
        except asyncio.CancelledError:
            pass

    async def _watch_ffmpeg_exit(self) -> None:
        """Detect unexpected ffmpeg exit and transition to INDEX."""
        assert self.proc
        try:
            await self.proc.wait()
        except asyncio.CancelledError:
            return
        if self.app_state is AppState.CAPTURING and not self._stopping_ffmpeg:
            rc = self.proc.returncode
            print(f"[monitor] ffmpeg exited unexpectedly (rc={rc})", file=sys.stderr)
            if self.session:
                self.session.add_warning(f"ffmpeg exited unexpectedly (rc={rc})")
                try:
                    self._write_meta(aborted=True)
                except Exception:
                    pass
            for task in [self._pipe_task, self._stderr_task, self._stall_task]:
                if task:
                    task.cancel()
            tasks = [t for t in [self._pipe_task, self._stderr_task, self._stall_task] if t]
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            self._reset_capture()
            self.app_state = AppState.INDEX


# ---------------------------------------------------------------------------
# Stream broadcaster
# ---------------------------------------------------------------------------

class StreamBroadcaster:
    """Reads fMP4 data from ffmpeg pipe and fans out to WebSocket clients."""

    def __init__(self) -> None:
        self._clients: set[ServerConnection] = set()
        self._init_segment: bytes | None = None

    def add_client(self, ws: ServerConnection) -> None:
        self._clients.add(ws)
        if self._init_segment:
            asyncio.ensure_future(self._send_to_client(ws, self._init_segment))

    def remove_client(self, ws: ServerConnection) -> None:
        self._clients.discard(ws)

    async def feed(self, data: bytes) -> None:
        if self._init_segment is None:
            self._init_segment = data
        dead: list[ServerConnection] = []
        for client in list(self._clients):
            try:
                await asyncio.wait_for(client.send(data), timeout=2.0)
            except Exception:
                dead.append(client)
        for c in dead:
            self._clients.discard(c)

    async def _send_to_client(self, ws: ServerConnection, data: bytes) -> None:
        try:
            await ws.send(data)
        except Exception:
            self._clients.discard(ws)

    @property
    def client_count(self) -> int:
        return len(self._clients)


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------

_MIME_TYPES = {
    ".html": "text/html",
    ".js":   "application/javascript",
    ".css":  "text/css",
    ".json": "application/json",
    ".mp4":  "video/mp4",
    ".png":  "image/png",
    ".svg":  "image/svg+xml",
    ".ico":  "image/x-icon",
}


class HTTPServer:
    def __init__(self, ctx: AppContext, port: int, ws_port: int) -> None:
        self.ctx = ctx
        self.port = port
        self.ws_port = ws_port

    async def start(self) -> asyncio.Server:
        return await asyncio.start_server(self._handle, "0.0.0.0", self.port)

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            request_line = await asyncio.wait_for(reader.readline(), timeout=5.0)
            if not request_line:
                return
            parts = request_line.decode("utf-8", errors="replace").strip().split()
            if len(parts) < 2:
                return
            method, path_raw = parts[0], parts[1]

            headers: dict[str, str] = {}
            while True:
                line = await asyncio.wait_for(reader.readline(), timeout=5.0)
                if line.strip() == b"":
                    break
                if b":" in line:
                    k, _, v = line.decode("utf-8", errors="replace").partition(":")
                    headers[k.strip().lower()] = v.strip()

            clean = path_raw.split("?")[0]
            ctx = self.ctx

            if clean == "/api/status":
                await self._respond(writer, 200, json.dumps(ctx.status_dict()).encode())

            elif clean == "/api/start" and method == "POST":
                if ctx.app_state is not AppState.INDEX:
                    await self._respond(writer, 409, b'{"error":"capture already active"}')
                else:
                    try:
                        await ctx.start_ffmpeg()
                        await self._respond(writer, 200, json.dumps(ctx.status_dict()).encode())
                    except Exception as exc:
                        await self._respond(
                            writer, 500, json.dumps({"error": str(exc)}).encode()
                        )

            elif clean == "/api/complete" and method == "POST":
                if ctx.app_state is not AppState.CAPTURING:
                    await self._respond(writer, 409, b'{"error":"not CAPTURING"}')
                else:
                    try:
                        result = await ctx.complete_capture()
                        await self._respond(writer, 200, json.dumps(result).encode())
                    except ValueError as exc:
                        await self._respond(
                            writer, 409, json.dumps({"error": str(exc)}).encode()
                        )

            elif clean == "/api/abort" and method == "POST":
                if ctx.app_state is not AppState.CAPTURING:
                    await self._respond(writer, 409, b'{"error":"not CAPTURING"}')
                else:
                    await ctx.abort_capture()
                    await self._respond(writer, 200, json.dumps(ctx.status_dict()).encode())

            elif clean == "/api/dismiss" and method == "POST":
                if ctx.app_state is not AppState.FINALIZING:
                    await self._respond(writer, 409, b'{"error":"not FINALIZING"}')
                elif ctx._finalize_progress.get("step") not in ("done", "error"):
                    await self._respond(writer, 409, b'{"error":"finalization still in progress"}')
                else:
                    ctx._reset_capture()
                    ctx.app_state = AppState.INDEX
                    await self._respond(writer, 200, json.dumps(ctx.status_dict()).encode())

            elif clean in ("/api/mark-in", "/api/mark-out"):
                if ctx.app_state is not AppState.CAPTURING:
                    await self._respond(writer, 409, b'{"error":"not CAPTURING"}')
                else:
                    direction = "in" if clean == "/api/mark-in" else "out"
                    await self._handle_mark(writer, path_raw, direction)

            elif clean == "/view":
                await self._serve_file(writer, WEB_DIR / "view.html")

            elif clean.startswith("/api/recording/") and clean.endswith("/transfer"):
                name = clean.removeprefix("/api/recording/").removesuffix("/transfer")
                if not _NAME_RE.match(name):
                    await self._respond(writer, 400, b'{"error":"invalid filename"}')
                elif method == "GET":
                    state = ctx._transfers.get(name, {"state": "idle"})
                    await self._respond(writer, 200, json.dumps(state).encode())
                elif method == "POST":
                    if not ctx.storage_dir.exists():
                        await self._respond(writer, 412, b'{"error":"storage/ not found: create the directory or symlink it to your storage target"}')
                    elif ctx._transfers.get(name, {}).get("state") == "running":
                        await self._respond(writer, 409, b'{"error":"transfer already running"}')
                    else:
                        output = ctx.sessions_dir / name
                        if not output.exists():
                            await self._respond(writer, 404, b'{"error":"file not found"}')
                        else:
                            asyncio.create_task(ctx._transfer_recording(output))
                            await self._respond(writer, 200, json.dumps(
                                ctx._transfers.get(name, {"state": "idle"})
                            ).encode())
                else:
                    await self._respond(writer, 405, b'{"error":"method not allowed"}')

            elif clean.startswith("/api/meta/") and method == "GET":
                name = clean.removeprefix("/api/meta/")
                await self._handle_get_meta(writer, name)

            elif clean.startswith("/api/session/") and method == "DELETE":
                name = clean.removeprefix("/api/session/")
                await self._handle_delete_session(writer, name)

            elif clean == "/api/sessions" and method == "DELETE":
                await self._handle_delete_all_sessions(writer)

            elif clean.startswith("/api/recording/") and method == "DELETE":
                name = clean.removeprefix("/api/recording/")
                await self._handle_delete_recording(writer, name)

            elif clean == "/api/recordings" and method == "DELETE":
                await self._handle_delete_all_recordings(writer)

            elif clean.startswith("/files/"):
                name = clean.removeprefix("/files/")
                await self._handle_file(writer, name, headers)

            elif clean in ("/", "/index.html"):
                await self._serve_file(writer, WEB_DIR / "index.html")

            else:
                filepath = WEB_DIR / clean.lstrip("/")
                if filepath.is_file():
                    await self._serve_file(writer, filepath)
                else:
                    await self._respond(writer, 404, b"Not found", "text/plain")

        except (asyncio.TimeoutError, ConnectionResetError, BrokenPipeError):
            pass
        finally:
            try:
                writer.close()
                writer.transport.abort()  # discard stuck send buffer; schedules connection_lost()
                await writer.wait_closed()
            except Exception:
                pass

    async def _handle_mark(
        self, writer: asyncio.StreamWriter, path_raw: str, direction: str
    ) -> None:
        assert self.ctx.session
        stream_time = self._parse_time(path_raw)
        if stream_time is None:
            await self._respond(writer, 400, b'{"error":"stream_time required"}')
            return

        session = self.ctx.session
        if direction == "in":
            ok = session.mark_in(stream_time)
            if ok:
                print(f"[monitor] RECORD IN at {stream_time:.3f}s")
        else:
            ok = session.mark_out(stream_time)
            if ok:
                seg = session.segments[-1]
                print(f"[monitor] RECORD OUT at {stream_time:.3f}s "
                      f"(segment: {seg[0]:.3f}s – {seg[1]:.3f}s)")
                try:
                    self.ctx._write_meta()
                except Exception:
                    pass

        body = json.dumps({"ok": ok, **self.ctx.status_dict()}).encode()
        await self._respond(writer, 200 if ok else 409, body)

    async def _handle_file(
        self, writer: asyncio.StreamWriter, name: str, headers: dict
    ) -> None:
        if not _NAME_RE.fullmatch(name):
            await self._respond(writer, 403, b"Forbidden", "text/plain")
            return
        path = self.ctx.sessions_dir / name
        if not path.is_file():
            await self._respond(writer, 404, b"Not found", "text/plain")
            return

        total = path.stat().st_size
        range_header = headers.get("range", "")

        if range_header.startswith("bytes="):
            spec = range_header[6:].split(",")[0].strip()  # first range only (ignore multi-range)
            try:
                start_s, _, end_s = spec.partition("-")
                start = int(start_s) if start_s else 0
                end = int(end_s) if end_s else total - 1
            except ValueError:
                await self._respond(writer, 416, b"Range Not Satisfiable", "text/plain")
                return
            end = min(end, total - 1)
            length = end - start + 1
            status_line = "206 Partial Content"
            cr_header = f"Content-Range: bytes {start}-{end}/{total}\r\n"
        else:
            start, length = 0, total
            status_line = "200 OK"
            cr_header = ""

        writer.write((
            f"HTTP/1.1 {status_line}\r\n"
            f"Content-Type: video/mp4\r\n"
            f"Content-Length: {length}\r\n"
            f"{cr_header}"
            f"Accept-Ranges: bytes\r\n"
            f"Access-Control-Allow-Origin: *\r\n"
            f"Connection: close\r\n"
            f"\r\n"
        ).encode())

        remaining = length
        with open(path, "rb") as f:
            f.seek(start)
            while remaining > 0:
                chunk = f.read(min(262144, remaining))
                if not chunk:
                    break
                writer.write(chunk)
                await writer.drain()
                remaining -= len(chunk)

    async def _handle_get_meta(
        self, writer: asyncio.StreamWriter, name: str
    ) -> None:
        if not _NAME_RE.fullmatch(name):
            await self._respond(writer, 400, b'{"error":"invalid name"}')
            return
        meta = self.ctx.sessions_dir / Path(name).with_suffix(".json").name
        if not meta.is_file():
            await self._respond(writer, 404, b'{"error":"not found"}')
            return
        await self._respond(writer, 200, meta.read_bytes())

    async def _handle_delete_session(
        self, writer: asyncio.StreamWriter, name: str
    ) -> None:
        if not _SESSION_NAME_RE.fullmatch(name):
            await self._respond(writer, 400, b'{"error":"invalid name"}')
            return
        mp4 = self.ctx.sessions_dir / name
        if not mp4.exists():
            await self._respond(writer, 404, b'{"error":"not found"}')
            return
        deleted: list[str] = []
        for p in [mp4, mp4.with_suffix(".json")]:
            try:
                p.unlink()
                deleted.append(p.name)
            except OSError:
                pass
        await self._respond(writer, 200, json.dumps({"deleted": deleted}).encode())

    async def _handle_delete_all_sessions(self, writer: asyncio.StreamWriter) -> None:
        deleted: list[str] = []
        freed = 0
        for mp4 in [p for p in self.ctx.sessions_dir.glob("*.mp4") if _SESSION_NAME_RE.fullmatch(p.name)]:
            try:
                freed += mp4.stat().st_size
            except OSError:
                pass
            for p in [mp4, mp4.with_suffix(".json")]:
                try:
                    p.unlink()
                    deleted.append(p.name)
                except OSError:
                    pass
        await self._respond(writer, 200, json.dumps({"deleted": deleted, "freed_bytes": freed}).encode())

    async def _handle_delete_recording(
        self, writer: asyncio.StreamWriter, name: str
    ) -> None:
        if not _RECORDING_NAME_RE.fullmatch(name):
            await self._respond(writer, 400, b'{"error":"invalid name"}')
            return
        path = self.ctx.sessions_dir / name
        if not path.exists():
            await self._respond(writer, 404, b'{"error":"not found"}')
            return
        try:
            path.unlink()
            await self._respond(writer, 200, json.dumps({"deleted": [name]}).encode())
        except OSError as exc:
            await self._respond(writer, 500, json.dumps({"error": str(exc)}).encode())

    async def _handle_delete_all_recordings(self, writer: asyncio.StreamWriter) -> None:
        deleted: list[str] = []
        freed = 0
        for p in [p for p in self.ctx.sessions_dir.glob("*.mp4") if _RECORDING_NAME_RE.fullmatch(p.name)]:
            try:
                freed += p.stat().st_size
            except OSError:
                pass
            try:
                p.unlink()
                deleted.append(p.name)
            except OSError:
                pass
        await self._respond(writer, 200, json.dumps({"deleted": deleted, "freed_bytes": freed}).encode())

    async def _serve_file(
        self, writer: asyncio.StreamWriter, filepath: Path
    ) -> None:
        content_type = _MIME_TYPES.get(filepath.suffix, "application/octet-stream")
        data = filepath.read_bytes()
        await self._respond(writer, 200, data, content_type)

    @staticmethod
    async def _respond(
        writer: asyncio.StreamWriter,
        status: int,
        body: bytes,
        content_type: str = "application/json",
    ) -> None:
        reason = HTTPStatus(status).phrase
        writer.write((
            f"HTTP/1.1 {status} {reason}\r\n"
            f"Content-Type: {content_type}\r\n"
            f"Content-Length: {len(body)}\r\n"
            f"Access-Control-Allow-Origin: *\r\n"
            f"Connection: close\r\n"
            f"\r\n"
        ).encode() + body)
        await writer.drain()

    @staticmethod
    def _parse_time(path: str) -> float | None:
        if "?" not in path:
            return None
        for param in path.split("?", 1)[1].split("&"):
            if "=" in param:
                k, v = param.split("=", 1)
                if k == "t":
                    try:
                        return float(v)
                    except ValueError:
                        return None
        return None


# ---------------------------------------------------------------------------
# Sidecar helper
# ---------------------------------------------------------------------------

def _atomic_write_json(path: Path, data: dict) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(path)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def async_main(
    sessions_dir: Path, port: int, storage_dir: Path | None = None, synthetic: bool = False,
) -> int:
    ws_port = port + WS_PORT_OFFSET
    ctx = AppContext(sessions_dir, storage_dir=storage_dir, synthetic=synthetic)

    async def ws_handler(websocket: ServerConnection) -> None:
        ctx.broadcaster.add_client(websocket)
        print(f"[monitor] WS client connected ({ctx.broadcaster.client_count} total)")
        try:
            async for _ in websocket:
                pass
        except Exception:
            pass
        finally:
            ctx.broadcaster.remove_client(websocket)
            print(f"[monitor] WS client disconnected ({ctx.broadcaster.client_count} total)")

    logging.getLogger("websockets").setLevel(logging.ERROR)

    http_server = HTTPServer(ctx, port, ws_port)
    http_srv = await http_server.start()
    ws_server = await serve(ws_handler, "0.0.0.0", ws_port, close_timeout=1)

    _host = socket.gethostname()
    if "." not in _host:
        try:
            with open("/etc/resolv.conf") as f:
                for line in f:
                    parts = line.split()
                    if len(parts) >= 2 and parts[0] in ("domain", "search"):
                        _host = f"{_host}.{parts[1]}"
                        break
        except OSError:
            pass
    print(f"[monitor] HTTP: http://{_host}:{port}")
    print(f"[monitor] WS:   ws://0.0.0.0:{ws_port}")
    print("[monitor] press Ctrl-C to stop\n")

    shutdown_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, shutdown_event.set)

    await shutdown_event.wait()
    print("\n[monitor] shutting down...")

    if ctx.app_state is AppState.CAPTURING:
        await ctx.abort_capture()
    elif ctx.app_state is AppState.FINALIZING:
        if ctx.finalize_task:
            ctx.finalize_task.cancel()
            await asyncio.gather(ctx.finalize_task, return_exceptions=True)
        await ctx.stop_ffmpeg()

    http_srv.close()
    ws_server.close()
    try:
        async with asyncio.timeout(3.0):
            await asyncio.gather(http_srv.wait_closed(), ws_server.wait_closed())
    except TimeoutError:
        pass

    # Cancel any tasks that outlived the timeout (e.g. ws close-handshake tasks
    # that asyncio.run() cleanup would otherwise block on indefinitely).
    for _ in range(3):
        remaining = {t for t in asyncio.all_tasks() if t is not asyncio.current_task()}
        if not remaining:
            break
        for t in remaining:
            t.cancel()
        await asyncio.gather(*remaining, return_exceptions=True)

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Monitored HDMI capture with browser preview and record control."
    )
    parser.add_argument(
        "-o", "--sessions-dir", type=Path, default=None,
        help=f"Scratch directory for sessions and recordings "
             f"(default: {_DEFAULT_SESSIONS_DIR}).",
    )
    parser.add_argument(
        "-p", "--port", type=int, default=DEFAULT_PORT,
        help=f"HTTP server port (default: {DEFAULT_PORT}).",
    )
    parser.add_argument(
        "--storage-dir", type=Path, default=None, metavar="DIR",
        help=f"Override storage destination for transfers (default: {_DEFAULT_STORAGE_DIR}).",
    )
    parser.add_argument(
        "--synthetic", action="store_true",
        help="Use lavfi sources + libx264 instead of real hardware (testing only).",
    )
    args = parser.parse_args()

    sessions_dir = args.sessions_dir or _DEFAULT_SESSIONS_DIR
    storage_dir = args.storage_dir or _DEFAULT_STORAGE_DIR

    print(f"[monitor] sessions dir:  {sessions_dir}")
    print(f"[monitor] storage dir:   {storage_dir}")

    return asyncio.run(async_main(sessions_dir, args.port, storage_dir, synthetic=args.synthetic))


if __name__ == "__main__":
    raise SystemExit(main())
