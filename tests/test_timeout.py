"""Network timeout option: defaults reach the transport, per-call override, idle (per-read) stream timeout."""

import asyncio

import httpx
import pytest

from k9o import KError, create_client
from k9o.http import resolve_timeout

OK = {"data": {"content": "hi"}, "meta": {}}


def _client(cap, **kw):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "HEAD":  # the credential-free region probe carries the client timeout too; not under test
            return httpx.Response(200)
        cap.setdefault("timeouts", []).append(request.extensions["timeout"])
        if request.headers.get("accept") == "text/event-stream":
            return httpx.Response(200, content=b'data: {"t":"a"}\n\ndata: [DONE]\n\n')
        return httpx.Response(200, json=OK)

    c = create_client(base_url="https://acme.api.k9o.io", project_slug="timeout-test", **kw)
    c._config.transport = httpx.MockTransport(handler)
    return c


class TestResolve:
    def test_defaults(self):
        t = resolve_timeout(None)
        assert (t.connect, t.read) == (10.0, 120.0)

    def test_number_sets_read_keeps_connect(self):
        t = resolve_timeout(45)
        assert (t.connect, t.read) == (10.0, 45.0)

    def test_pair(self):
        t = resolve_timeout((3, 200))
        assert (t.connect, t.read) == (3, 200)

    def test_httpx_timeout_passthrough(self):
        t = httpx.Timeout(1, read=2)
        assert resolve_timeout(t) is t

    def test_rejects_junk(self):
        with pytest.raises(TypeError):
            resolve_timeout("fast")


class TestSyncTimeout:
    def test_default_reaches_transport(self):
        cap = {}
        _client(cap).ai.generate("hi")
        t = cap["timeouts"][-1]
        assert (t["connect"], t["read"]) == (10.0, 120.0)

    def test_client_option_reaches_transport(self):
        cap = {}
        _client(cap, timeout=(2, 300)).ai.generate("hi")
        t = cap["timeouts"][-1]
        assert (t["connect"], t["read"]) == (2, 300)

    def test_per_call_override(self):
        cap = {}
        c = _client(cap, timeout=60)
        c.ai.generate("hi", timeout=5)
        c.ai.generate("hi")
        assert cap["timeouts"][0]["read"] == 5 and cap["timeouts"][0]["connect"] == 10.0
        assert cap["timeouts"][1]["read"] == 60

    def test_with_options_any_method(self):
        cap = {}
        c = _client(cap)
        c.with_options(timeout=7).compute.llms.list()
        c.compute.llms.invoke("m", prompt="x", timeout=9)
        assert cap["timeouts"][0]["read"] == 7
        assert cap["timeouts"][1]["read"] == 9


class TestAsyncTimeout:
    async def test_invoke_stream_default_and_override(self):
        cap = {}
        c = _client(cap)
        assert [x async for x in c.compute.llms.invoke_stream("m", prompt="x")] == [{"t": "a"}]
        assert [x async for x in c.compute.llms.invoke_stream("m", prompt="x", timeout=(1, 33))] == [{"t": "a"}]
        assert (cap["timeouts"][0]["connect"], cap["timeouts"][0]["read"]) == (10.0, 120.0)
        assert (cap["timeouts"][1]["connect"], cap["timeouts"][1]["read"]) == (1, 33)

    async def test_async_request_uses_client_timeout(self):
        from k9o.http import request_async

        cap = {}
        c = _client(cap, timeout=42)
        await request_async(c._config, method="GET", path="/x")
        assert cap["timeouts"][0]["read"] == 42


async def _serve(handler):
    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1]


def _live_client(port, timeout):
    c = create_client(base_url=f"http://127.0.0.1:{port}", project_slug="timeout-test", timeout=timeout)
    return c


async def _read_request(reader):
    await reader.readuntil(b"\r\n\r\n")


class TestStreamIdleTimeout:
    async def test_stream_longer_than_timeout_survives_when_data_keeps_flowing(self):
        async def handler(reader, writer):
            await _read_request(reader)
            writer.write(b"HTTP/1.1 200 OK\r\ncontent-type: text/event-stream\r\nconnection: close\r\n\r\n")
            for i in range(5):
                writer.write(f'data: {{"n":{i}}}\n\n'.encode())
                await writer.drain()
                await asyncio.sleep(0.3)  # 1.5 s total, each gap well under the 0.8 s read timeout
            writer.write(b"data: [DONE]\n\n")
            await writer.drain()
            writer.close()

        server, port = await _serve(handler)
        try:
            c = _live_client(port, (2, 0.8))
            got = [x async for x in c.compute.llms.invoke_stream("m", prompt="x")]
            assert [g["n"] for g in got] == [0, 1, 2, 3, 4]
        finally:
            server.close()

    async def test_silent_stream_hits_idle_timeout(self):
        async def handler(reader, writer):
            await _read_request(reader)
            writer.write(b"HTTP/1.1 200 OK\r\ncontent-type: text/event-stream\r\nconnection: close\r\n\r\n")
            writer.write(b'data: {"n":0}\n\n')
            await writer.drain()
            await asyncio.sleep(3)  # idle longer than the read timeout
            writer.close()

        server, port = await _serve(handler)
        try:
            c = _live_client(port, (2, 0.5))
            got = []
            with pytest.raises(KError) as ei:
                async for x in c.compute.llms.invoke_stream("m", prompt="x"):
                    got.append(x)
            assert got == [{"n": 0}]
            assert ei.value.code == "K9O_UPSTREAM_UNAVAILABLE"
        finally:
            server.close()
