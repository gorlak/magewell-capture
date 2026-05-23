"""Tier 1 tests for HTTP server, WebSocket, and API.

No hardware required. Runs in CI.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time

import pytest
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve

from monitor import HTTPServer, SessionState, StreamBroadcaster

logging.getLogger("websockets").setLevel(logging.ERROR)


@pytest.fixture()
async def http_server():
    """Start the HTTP server on a random port."""
    session = SessionState(time.monotonic())
    srv = HTTPServer(session, 0, 0)
    tcp_server = await srv.start()
    port = tcp_server.sockets[0].getsockname()[1]
    yield session, port
    tcp_server.close()
    await tcp_server.wait_closed()


@pytest.fixture()
async def ws_server():
    """Start the WebSocket server on a random port."""
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


async def _http_get(port: int, path: str) -> tuple[int, bytes]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(f"GET {path} HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n".encode())
    await writer.drain()
    response = await asyncio.wait_for(reader.read(65536), timeout=2.0)
    writer.close()
    await writer.wait_closed()
    first_line = response.split(b"\r\n")[0]
    status = int(first_line.split()[1])
    body = response.split(b"\r\n\r\n", 1)[1] if b"\r\n\r\n" in response else b""
    return status, body


# ---- HTTP server tests ----

async def test_http_get_index(http_server):
    _, port = http_server
    status, body = await _http_get(port, "/")
    assert status == 200
    assert b"Capture Monitor" in body


async def test_http_get_status(http_server):
    _, port = http_server
    status, body = await _http_get(port, "/api/status")
    assert status == 200
    data = json.loads(body)
    assert data["state"] == "STANDBY"


async def test_mark_in_via_get(http_server):
    session, port = http_server
    status, body = await _http_get(port, "/api/mark-in?t=10.5")
    assert status == 200
    data = json.loads(body)
    assert data["ok"] is True
    assert session.state.value == "RECORDING"


async def test_mark_in_missing_stream_time(http_server):
    _, port = http_server
    status, body = await _http_get(port, "/api/mark-in")
    assert status == 400
    assert b"stream_time required" in body


async def test_mark_out_via_get(http_server):
    session, port = http_server
    await _http_get(port, "/api/mark-in?t=10.0")
    status, body = await _http_get(port, "/api/mark-out?t=20.0")
    assert status == 200
    data = json.loads(body)
    assert data["ok"] is True
    assert len(data["segments"]) == 1


async def test_http_404(http_server):
    _, port = http_server
    status, _ = await _http_get(port, "/nonexistent")
    assert status == 404


# ---- WebSocket tests ----

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
