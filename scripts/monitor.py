#!/home/geoff/Projects/magewell-capture/.venv/bin/python
"""monitor.py — monitored HDMI capture with browser preview and record control.

Runs a continuous ffmpeg capture (HEVC NVENC dual output), streams the preview
to browsers over WebSocket+MSE, and provides record in/out control. In/out
points use the browser's video.currentTime, so cuts match what the user saw.

Two servers:
  - HTTP (:8090) — web UI, static files, JSON API
  - WebSocket (:8091) — fMP4 video stream via websockets library

See DECISIONS.md § "Phase 2: Monitored capture" for the design rationale.

Usage:
    .venv/bin/python scripts/monitor.py [--output-dir DIR] [--port PORT]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import signal
import socket
import subprocess
import sys
import time
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
WS_PORT_OFFSET = 1  # WebSocket on port+1

WEB_DIR = Path(__file__).parent / "web"


# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------

class State(Enum):
    STANDBY = "STANDBY"
    RECORDING = "RECORDING"


class SessionState:
    """Tracks record in/out points for the current capture session."""

    def __init__(self, start_time: float):
        self.start_time = start_time
        self.state = State.STANDBY
        self.segments: list[tuple[float, float]] = []
        self._current_in: float | None = None
        self._current_in_wall: float | None = None

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
        }


# ---------------------------------------------------------------------------
# Stream broadcaster
# ---------------------------------------------------------------------------

class StreamBroadcaster:
    """Reads fMP4 data from ffmpeg pipe and fans out to WebSocket clients."""

    def __init__(self):
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
# HTTP server (asyncio, stdlib — reliable for regular HTTP requests)
# ---------------------------------------------------------------------------

_MIME_TYPES = {
    ".html": "text/html",
    ".js": "application/javascript",
    ".css": "text/css",
    ".json": "application/json",
    ".mp4": "video/mp4",
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
}


class HTTPServer:
    """Simple async HTTP server for API and static files."""

    def __init__(self, session: SessionState, port: int, ws_port: int):
        self.session = session
        self.port = port
        self.ws_port = ws_port

    async def start(self) -> asyncio.Server:
        server = await asyncio.start_server(
            self._handle, "0.0.0.0", self.port
        )
        return server

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            request_line = await asyncio.wait_for(
                reader.readline(), timeout=5.0
            )
            if not request_line:
                return

            parts = request_line.decode("utf-8", errors="replace").strip().split()
            if len(parts) < 2:
                return
            path = parts[1]

            # consume headers (we don't need them for GET)
            while True:
                line = await asyncio.wait_for(reader.readline(), timeout=5.0)
                if line.strip() == b"":
                    break

            clean_path = path.split("?")[0]

            # ---- API ----
            if clean_path == "/api/status":
                body = json.dumps(self.session.to_dict()).encode()
                await self._respond(writer, 200, body)
            elif clean_path == "/api/mark-in":
                await self._handle_mark(writer, path, "in")
            elif clean_path == "/api/mark-out":
                await self._handle_mark(writer, path, "out")
            # ---- static files ----
            elif clean_path == "/" or clean_path == "/index.html":
                await self._serve_file(writer, WEB_DIR / "index.html")
            else:
                filepath = WEB_DIR / clean_path.lstrip("/")
                if filepath.is_file():
                    await self._serve_file(writer, filepath)
                else:
                    await self._respond(writer, 404, b"Not found", "text/plain")

        except (asyncio.TimeoutError, ConnectionResetError, BrokenPipeError):
            pass
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def _handle_mark(
        self, writer: asyncio.StreamWriter, path: str, direction: str
    ) -> None:
        stream_time = self._parse_time(path)
        if stream_time is None:
            await self._respond(writer, 400, b'{"error":"stream_time required"}')
            return

        if direction == "in":
            ok = self.session.mark_in(stream_time)
            if ok:
                print(f"[monitor] RECORD IN at stream time {stream_time:.3f}s")
        else:
            ok = self.session.mark_out(stream_time)
            if ok:
                seg = self.session.segments[-1]
                print(
                    f"[monitor] RECORD OUT at stream time {stream_time:.3f}s "
                    f"(segment: {seg[0]:.3f}s – {seg[1]:.3f}s)"
                )

        body = json.dumps({"ok": ok, **self.session.to_dict()}).encode()
        await self._respond(writer, 200 if ok else 409, body)

    @staticmethod
    def _parse_time(path: str) -> float | None:
        if "?" not in path:
            return None
        query = path.split("?", 1)[1]
        for param in query.split("&"):
            if "=" in param:
                key, val = param.split("=", 1)
                if key == "t":
                    try:
                        return float(val)
                    except ValueError:
                        return None
        return None

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
        header = (
            f"HTTP/1.1 {status} {reason}\r\n"
            f"Content-Type: {content_type}\r\n"
            f"Content-Length: {len(body)}\r\n"
            f"Access-Control-Allow-Origin: *\r\n"
            f"Connection: close\r\n"
            f"\r\n"
        )
        writer.write(header.encode() + body)
        await writer.drain()


# ---------------------------------------------------------------------------
# Segment extraction
# ---------------------------------------------------------------------------

def extract_segments(
    source: Path,
    segments: list[tuple[float, float]],
    output_dir: Path,
    width: int,
    height: int,
    fps: float,
    interlaced: bool,
) -> list[Path]:
    if not segments:
        print("[monitor] no segments to extract")
        return []

    outputs: list[Path] = []
    for i, (start, end) in enumerate(segments, 1):
        output = make_output_path(
            output_dir, width, height, fps, interlaced, prefix="recording"
        )
        duration = end - start
        print(
            f"[monitor] extracting segment {i}/{len(segments)}: "
            f"{start:.1f}s – {end:.1f}s ({duration:.1f}s) → {output.name}"
        )

        cmd = build_extract_cmd(source, start, end, output)
        rc = subprocess.run(cmd, capture_output=True).returncode
        if rc == 0:
            size_mb = output.stat().st_size / (1024 * 1024)
            print(f"  → {output} ({size_mb:.1f} MB)")
            outputs.append(output)
        else:
            print(f"  → extraction failed (exit {rc})", file=sys.stderr)

        time.sleep(1.1)

    return outputs


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def async_main(output_dir: Path, port: int) -> int:
    ws_port = port + WS_PORT_OFFSET

    # ---- probe ----
    width, height, fps, interlaced = probe_signal()

    # ---- paths ----
    output_dir.mkdir(parents=True, exist_ok=True)
    session_file = make_output_path(
        output_dir, width, height, fps, interlaced, prefix="session"
    )

    # ---- build ffmpeg command ----
    cmd = build_monitor_cmd(width, height, fps, interlaced, session_file)
    print(f"\n[monitor] session file: {session_file}")
    print(f"[monitor] cmd: {' '.join(cmd)}\n")

    # ---- start ffmpeg ----
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    # ---- start session + broadcaster ----
    session = SessionState(time.monotonic())
    broadcaster = StreamBroadcaster()

    # ---- WebSocket handler (video stream only) ----
    async def ws_handler(websocket: ServerConnection) -> None:
        broadcaster.add_client(websocket)
        print(f"[monitor] WebSocket client connected ({broadcaster.client_count} total)")
        try:
            async for _ in websocket:
                pass
        except Exception:
            pass
        finally:
            broadcaster.remove_client(websocket)
            print(f"[monitor] WebSocket client disconnected ({broadcaster.client_count} total)")

    # ---- start servers ----
    logging.getLogger("websockets").setLevel(logging.ERROR)

    http_server = HTTPServer(session, port, ws_port)
    http_srv = await http_server.start()

    ws_server = await serve(ws_handler, "0.0.0.0", ws_port)

    _host = socket.gethostname()
    if '.' not in _host:
        try:
            with open('/etc/resolv.conf') as _f:
                for _line in _f:
                    _parts = _line.split()
                    if len(_parts) >= 2 and _parts[0] in ('domain', 'search'):
                        _host = f"{_host}.{_parts[1]}"
                        break
        except OSError:
            pass
    print(f"[monitor] HTTP server on http://{_host}:{port}")
    print(f"[monitor] WebSocket on ws://0.0.0.0:{ws_port}")
    print("[monitor] press Ctrl-C to stop capture\n")

    # ---- shutdown handling ----
    shutdown_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    received_signal = None

    def _on_signal(signum: int) -> None:
        nonlocal received_signal
        received_signal = signum
        print("\n[monitor] shutting down...")
        http_srv.close()
        ws_server.close()
        shutdown_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda s=sig: _on_signal(s))

    # ---- pipe reader ----
    async def _read_pipe() -> None:
        assert proc.stdout is not None
        while True:
            chunk = await proc.stdout.read(65536)
            if not chunk:
                break
            if not shutdown_event.is_set():
                await broadcaster.feed(chunk)

    async def _drain_stdout() -> None:
        """Drain ffmpeg stdout without broadcasting — used during shutdown so
        ffmpeg's pipe write never blocks and it can finalize the session file."""
        if proc.stdout is None:
            return
        while True:
            chunk = await proc.stdout.read(65536)
            if not chunk:
                break

    # ---- ffmpeg stderr logger ----
    async def _log_stderr() -> None:
        assert proc.stderr is not None
        while True:
            line = await proc.stderr.readline()
            if not line:
                break
            text = line.decode("utf-8", errors="replace").rstrip()
            if text:
                print(f"[ffmpeg] {text}")

    pipe_task = asyncio.create_task(_read_pipe())
    stderr_task = asyncio.create_task(_log_stderr())
    ffmpeg_task = asyncio.create_task(proc.wait())
    shutdown_task = asyncio.create_task(shutdown_event.wait())

    done, _ = await asyncio.wait(
        [ffmpeg_task, shutdown_task],
        return_when=asyncio.FIRST_COMPLETED,
    )

    # ---- shutdown ----
    if shutdown_event.is_set() and proc.returncode is None:
        if received_signal != signal.SIGINT:
            proc.send_signal(signal.SIGINT)

        # Cancel the broadcaster pipe task and replace with a fast drain so
        # ffmpeg's stdout write never blocks. ffmpeg serialises all output I/O
        # in one thread: a blocked pipe:1 write prevents moov finalisation too.
        pipe_task.cancel()
        await asyncio.gather(pipe_task, return_exceptions=True)
        drain_task = asyncio.create_task(_drain_stdout())

        print("[monitor] waiting for ffmpeg to finalize session file...",
              file=sys.stderr)
        try:
            await asyncio.wait_for(proc.wait(), timeout=120.0)
        except asyncio.TimeoutError:
            print("[monitor] ffmpeg not responding, killing...",
                  file=sys.stderr)
            proc.kill()
            await proc.wait()
        finally:
            drain_task.cancel()
            await asyncio.gather(drain_task, return_exceptions=True)

    session.finalize(session.elapsed)

    http_srv.close()
    ws_server.close()
    pipe_task.cancel()
    stderr_task.cancel()
    await asyncio.gather(pipe_task, stderr_task, return_exceptions=True)
    try:
        async with asyncio.timeout(3.0):
            await asyncio.gather(
                http_srv.wait_closed(),
                ws_server.wait_closed(),
            )
    except TimeoutError:
        pass

    rc = proc.returncode or 0
    if rc == 255:
        rc = 0

    # ---- report ----
    if session_file.exists():
        size_mb = session_file.stat().st_size / (1024 * 1024)
        print(f"\n[monitor] session file: {session_file} ({size_mb:.1f} MB)")

    if session.segments:
        print(f"[monitor] {len(session.segments)} segment(s) marked")
        outputs = extract_segments(
            session_file, session.segments, output_dir,
            width, height, fps, interlaced,
        )
        if outputs:
            print(f"\n[monitor] {len(outputs)} recording(s) extracted:")
            for p in outputs:
                print(f"  {p}")
    else:
        print("[monitor] no segments were marked for extraction")

    print(f"[monitor] session file retained: {session_file}")
    return rc


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Monitored HDMI capture with browser preview and record control."
    )
    parser.add_argument(
        "-o", "--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR,
        help=f"Directory for output files (default: {DEFAULT_OUTPUT_DIR})."
    )
    parser.add_argument(
        "-p", "--port", type=int, default=DEFAULT_PORT,
        help=f"HTTP server port (default: {DEFAULT_PORT})."
    )
    args = parser.parse_args()
    return asyncio.run(async_main(args.output_dir, args.port))


if __name__ == "__main__":
    raise SystemExit(main())
