"""Tier 1 tests for HTTP server, WebSocket, and API.

No hardware required. Runs in CI.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime
from pathlib import Path

import pytest
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve

from monitor import AppContext, AppState, HTTPServer, SessionState, State, StreamBroadcaster

logging.getLogger("websockets").setLevel(logging.ERROR)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
async def http_server(tmp_path):
    """HTTP server in INDEX state (no capture running)."""
    ctx = AppContext(tmp_path)
    srv = HTTPServer(ctx, 0, 0)
    tcp = await srv.start()
    port = tcp.sockets[0].getsockname()[1]
    yield ctx, port
    tcp.close()
    await tcp.wait_closed()


@pytest.fixture()
async def capturing_http_server(tmp_path):
    """HTTP server pre-set to CAPTURING state (no real ffmpeg)."""
    ctx = AppContext(tmp_path)
    ctx.app_state = AppState.CAPTURING
    ctx.session = SessionState(time.monotonic())
    ctx.session_file = tmp_path / "session_test.mp4"
    ctx.meta_path = tmp_path / "session_test.json"
    ctx.signal_info = (1920, 1080, 60.0, False)
    ctx.started_at = datetime.now()
    srv = HTTPServer(ctx, 0, 0)
    tcp = await srv.start()
    port = tcp.sockets[0].getsockname()[1]
    yield ctx, port
    tcp.close()
    await tcp.wait_closed()


@pytest.fixture()
async def ws_server():
    """Standalone WebSocket server for broadcaster tests."""
    broadcaster = StreamBroadcaster()

    async def handler(websocket):
        broadcaster.add_client(websocket)
        try:
            async for _ in websocket:
                pass
        except Exception:
            pass
        finally:
            broadcaster.remove_client(websocket)

    server = await serve(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    yield broadcaster, port
    server.close()
    await server.wait_closed()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _http_get(port: int, path: str) -> tuple[int, bytes]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(
        f"GET {path} HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n".encode()
    )
    await writer.drain()
    response = await asyncio.wait_for(reader.read(65536), timeout=2.0)
    writer.close()
    await writer.wait_closed()
    first_line = response.split(b"\r\n")[0]
    status = int(first_line.split()[1])
    body = response.split(b"\r\n\r\n", 1)[1] if b"\r\n\r\n" in response else b""
    return status, body


async def _http_post(port: int, path: str) -> tuple[int, bytes]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(
        f"POST {path} HTTP/1.1\r\nHost: localhost\r\nContent-Length: 0\r\nConnection: close\r\n\r\n".encode()
    )
    await writer.drain()
    response = await asyncio.wait_for(reader.read(65536), timeout=2.0)
    writer.close()
    await writer.wait_closed()
    first_line = response.split(b"\r\n")[0]
    status = int(first_line.split()[1])
    body = response.split(b"\r\n\r\n", 1)[1] if b"\r\n\r\n" in response else b""
    return status, body


# ---------------------------------------------------------------------------
# INDEX state tests
# ---------------------------------------------------------------------------

async def test_http_get_index(http_server):
    _, port = http_server
    status, body = await _http_get(port, "/")
    assert status == 200
    assert b"Capture Monitor" in body


async def test_http_get_status_index(http_server):
    _, port = http_server
    status, body = await _http_get(port, "/api/status")
    assert status == 200
    data = json.loads(body)
    assert data["state"] == "INDEX"


async def test_status_includes_file_listing(http_server):
    ctx, port = http_server
    # Create a fake session file in the output dir
    (ctx.output_dir / "session_20260615_120000_1920x1080p60.mp4").write_bytes(b"")
    status, body = await _http_get(port, "/api/status")
    assert status == 200
    data = json.loads(body)
    assert len(data["sessions"]) == 1
    assert data["sessions"][0]["name"] == "session_20260615_120000_1920x1080p60.mp4"


async def test_start_in_non_index_state_returns_409(capturing_http_server):
    _, port = capturing_http_server
    status, body = await _http_post(port, "/api/start")
    assert status == 409


async def test_complete_in_index_state_returns_409(http_server):
    _, port = http_server
    status, _ = await _http_post(port, "/api/complete")
    assert status == 409


async def test_abort_in_index_state_returns_409(http_server):
    _, port = http_server
    status, _ = await _http_post(port, "/api/abort")
    assert status == 409


async def test_mark_in_in_index_state_returns_409(http_server):
    _, port = http_server
    status, _ = await _http_get(port, "/api/mark-in?t=10.0")
    assert status == 409


async def test_http_404(http_server):
    _, port = http_server
    status, _ = await _http_get(port, "/nonexistent")
    assert status == 404


# ---------------------------------------------------------------------------
# CAPTURING state tests
# ---------------------------------------------------------------------------

async def test_http_get_status_capturing(capturing_http_server):
    _, port = capturing_http_server
    status, body = await _http_get(port, "/api/status")
    assert status == 200
    data = json.loads(body)
    assert data["state"] == "CAPTURING"
    assert data["session_state"] == "STANDBY"


async def test_mark_in_via_get(capturing_http_server):
    ctx, port = capturing_http_server
    status, body = await _http_get(port, "/api/mark-in?t=10.5")
    assert status == 200
    data = json.loads(body)
    assert data["ok"] is True
    assert ctx.session.state is State.RECORDING


async def test_mark_in_missing_stream_time(capturing_http_server):
    _, port = capturing_http_server
    status, body = await _http_get(port, "/api/mark-in")
    assert status == 400
    assert b"stream_time required" in body


async def test_mark_out_via_get(capturing_http_server):
    ctx, port = capturing_http_server
    await _http_get(port, "/api/mark-in?t=10.0")
    status, body = await _http_get(port, "/api/mark-out?t=20.0")
    assert status == 200
    data = json.loads(body)
    assert data["ok"] is True
    assert len(data["segments"]) == 1


async def test_complete_with_no_segments_returns_409(capturing_http_server):
    _, port = capturing_http_server
    status, body = await _http_post(port, "/api/complete")
    assert status == 409
    data = json.loads(body)
    assert "no segments" in data["error"]


# ---------------------------------------------------------------------------
# WebSocket / broadcaster tests
# ---------------------------------------------------------------------------

async def test_websocket_connects(ws_server):
    broadcaster, port = ws_server
    async with connect(f"ws://127.0.0.1:{port}") as ws:
        await asyncio.sleep(0.1)
        assert broadcaster.client_count == 1
    await asyncio.sleep(0.1)
    assert broadcaster.client_count == 0


async def test_websocket_receives_broadcast(ws_server):
    broadcaster, port = ws_server
    async with connect(f"ws://127.0.0.1:{port}") as ws:
        await asyncio.sleep(0.1)
        test_data = b"\x00\x00\x00\x1cftyp" + b"\x00" * 20
        await broadcaster.feed(test_data)
        msg = await asyncio.wait_for(ws.recv(), timeout=2.0)
        assert msg == test_data


async def test_multiple_clients(ws_server):
    broadcaster, port = ws_server
    async with connect(f"ws://127.0.0.1:{port}") as ws1:
        async with connect(f"ws://127.0.0.1:{port}") as ws2:
            await asyncio.sleep(0.1)
            assert broadcaster.client_count == 2
            test_data = b"test_chunk"
            await broadcaster.feed(test_data)
            msg1 = await asyncio.wait_for(ws1.recv(), timeout=2.0)
            msg2 = await asyncio.wait_for(ws2.recv(), timeout=2.0)
            assert msg1 == test_data
            assert msg2 == test_data
    await asyncio.sleep(0.1)
    assert broadcaster.client_count == 0
