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
    .venv/bin/python scripts/monitor.py [--output-dir DIR] [--port PORT]
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
    DEFAULT_OUTPUT_DIR,
    build_extract_cmd,
    build_monitor_cmd,
    make_output_path,
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

_NAME_RE = re.compile(r"(session|recording)_[\w.]+\.mp4")
_SESSION_NAME_RE = re.compile(r"session_[\w.]+\.mp4")
_RECORDING_NAME_RE = re.compile(r"recording_[\w.]+\.mp4")

# HEVC Main10 CQ21 @1080p60 ≈ 20 Mbps ≈ 10 GB/hr (measured; see DECISIONS.md)
_HEVC_BYTES_PER_HOUR = 10 * 1_000_000_000


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


# ---------------------------------------------------------------------------
# AppContext — global server state and capture lifecycle
# ---------------------------------------------------------------------------

class AppContext:
    """Shared state for the entire server lifetime."""

    def __init__(self, output_dir: Path) -> None:
        self.output_dir = output_dir
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
            for p in sorted(self.output_dir.glob("*.mp4"), reverse=True):
                size = p.stat().st_size
                if p.name.startswith("session_"):
                    sessions.append({
                        "name": p.name,
                        "size": size,
                        "has_meta": p.with_suffix(".json").exists(),
                    })
                elif p.name.startswith("recording_"):
                    recordings.append({"name": p.name, "size": size})
        except OSError:
            pass
        disk: dict = {}
        try:
            usage = shutil.disk_usage(self.output_dir)
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

    # ---- capture lifecycle ----

    async def start_ffmpeg(self) -> None:
        """Probe signal, start ffmpeg, initialise session, → CAPTURING."""
        width, height, fps, interlaced = probe_signal()
        self.signal_info = (width, height, fps, interlaced)
        self.output_dir.mkdir(parents=True, exist_ok=True)

        session_file = make_output_path(
            self.output_dir, width, height, fps, interlaced, prefix="session"
        )
        self.session_file = session_file
        self.meta_path = session_file.with_suffix(".json")
        self.started_at = datetime.now()

        cmd = build_monitor_cmd(width, height, fps, interlaced, session_file)
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

    async def stop_ffmpeg(self) -> None:
        """Send SIGINT, drain stdout, wait for exit. Does not change app_state."""
        if self.proc is None or self.proc.returncode is not None:
            return

        self._stopping_ffmpeg = True

        if self._pipe_task:
            self._pipe_task.cancel()
            await asyncio.gather(self._pipe_task, return_exceptions=True)
            self._pipe_task = None

        drain_task = asyncio.create_task(self._drain_stdout())

        try:
            self.proc.send_signal(signal.SIGINT)
        except ProcessLookupError:
            pass  # already exited between the returncode check and here
        print("[monitor] waiting for ffmpeg to finalize session file...", file=sys.stderr)
        try:
            await asyncio.wait_for(self.proc.wait(), timeout=120.0)
        except asyncio.TimeoutError:
            print("[monitor] ffmpeg not responding, killing...", file=sys.stderr)
            self.proc.kill()
            await self.proc.wait()
        finally:
            drain_task.cancel()
            await asyncio.gather(drain_task, return_exceptions=True)

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
            raise ValueError("no segments marked")

        total = len(self.session.segments)
        self._finalize_progress = {"step": "stopping_ffmpeg", "recording": None, "total": total}
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
        """Background task: stop ffmpeg, extract all segments, → REPORT."""
        assert self.session and self.session_file and self.signal_info and self.meta_path

        w, h, fps, interlaced = self.signal_info
        segments = list(self.session.segments)
        session_file = self.session_file
        output_dir = self.output_dir

        await self.stop_ffmpeg()

        extractions = [
            {"in": round(s, 3), "out": round(e, 3),
             "output": None, "transferred": None, "status": "pending"}
            for s, e in segments
        ]
        self._write_meta(extractions=extractions)

        total = len(segments)
        self._finalize_progress = {"step": "extracting", "recording": 0, "total": total}

        for i, (start, end) in enumerate(segments):
            self._finalize_progress["recording"] = i + 1
            output = make_output_path(output_dir, w, h, fps, interlaced, prefix="recording")
            print(f"[monitor] extracting {i+1}/{total}: "
                  f"{start:.1f}s – {end:.1f}s ({end-start:.1f}s) → {output.name}")

            cmd = build_extract_cmd(session_file, start, end, output)
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr_bytes = await proc.communicate()

            if proc.returncode == 0:
                size_mb = output.stat().st_size / (1024 * 1024)
                print(f"  → {output.name} ({size_mb:.1f} MB)")
                extractions[i].update({"output": output.name, "status": "done"})
                try:
                    self._write_recording_meta(output, start, end)
                except Exception:
                    pass
            else:
                print(f"  → extraction failed (rc={proc.returncode})", file=sys.stderr)
                if stderr_bytes:
                    print(f"  {stderr_bytes.decode(errors='replace').strip()}", file=sys.stderr)
                extractions[i]["status"] = "failed"

            self._write_meta(extractions=extractions)

            if i < total - 1:
                await asyncio.sleep(1.1)

        self._finalize_progress = {"step": "done", "recording": total, "total": total}
        self.app_state = AppState.INDEX
        print("[monitor] FINALIZING complete → INDEX")
        self._reset_capture()

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
                await self.broadcaster.feed(chunk)
        except asyncio.CancelledError:
            pass

    async def _drain_stdout(self) -> None:
        if not self.proc or not self.proc.stdout:
            return
        try:
            while True:
                chunk = await self.proc.stdout.read(65536)
                if not chunk:
                    break
        except asyncio.CancelledError:
            pass

    async def _log_stderr(self) -> None:
        assert self.proc and self.proc.stderr
        try:
            while True:
                line = await self.proc.stderr.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").rstrip()
                if text:
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
                        usage = shutil.disk_usage(self.output_dir)
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
        for client in self._clients:
            try:
                await client.send(data)
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

            elif clean in ("/api/mark-in", "/api/mark-out"):
                if ctx.app_state is not AppState.CAPTURING:
                    await self._respond(writer, 409, b'{"error":"not CAPTURING"}')
                else:
                    direction = "in" if clean == "/api/mark-in" else "out"
                    await self._handle_mark(writer, path_raw, direction)

            elif clean == "/view":
                await self._serve_file(writer, WEB_DIR / "view.html")

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
        path = self.ctx.output_dir / name
        if not path.is_file():
            await self._respond(writer, 404, b"Not found", "text/plain")
            return

        total = path.stat().st_size
        range_header = headers.get("range", "")

        if range_header.startswith("bytes="):
            spec = range_header[6:]
            start_s, _, end_s = spec.partition("-")
            start = int(start_s) if start_s else 0
            end = int(end_s) if end_s else total - 1
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
        meta = self.ctx.output_dir / Path(name).with_suffix(".json").name
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
        mp4 = self.ctx.output_dir / name
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
        for mp4 in list(self.ctx.output_dir.glob("session_*.mp4")):
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
        path = self.ctx.output_dir / name
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
        for p in list(self.ctx.output_dir.glob("recording_*.mp4")):
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

async def async_main(output_dir: Path, port: int) -> int:
    ws_port = port + WS_PORT_OFFSET
    ctx = AppContext(output_dir)

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
        "-o", "--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR,
        help=f"Directory for output files (default: {DEFAULT_OUTPUT_DIR}).",
    )
    parser.add_argument(
        "-p", "--port", type=int, default=DEFAULT_PORT,
        help=f"HTTP server port (default: {DEFAULT_PORT}).",
    )
    args = parser.parse_args()
    return asyncio.run(async_main(args.output_dir, args.port))


if __name__ == "__main__":
    raise SystemExit(main())
