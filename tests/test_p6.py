"""
Tests for P6 surface families in the Python SDK:
  - client.studio.workflows.*
  - client.studio.dashboards.*
  - client.compute.llms.* + client.org.default_llm
  - client.realtime.subscribe() + replay()

Async surfaces (invoke_stream, subscribe) are driven via asyncio.run to
avoid an asyncio test-runner dependency.
"""

import asyncio
import json

import httpx
import pytest

from k9o import KError, create_client, realtime_topic


def _client(handler, **kw):
    c = create_client(base_url="https://acme.api.k9o.io", project_slug="portal", **kw)
    c._config.transport = httpx.MockTransport(handler)
    return c


def _frame(eid, etype="row_inserted", rid="rec_1"):
    data = {"topic": "collections.orders.row_inserted", "eventType": etype,
            "payload": {"collection": "orders", "id": rid}, "occurredAt": "2026-10-27T10:20:38.969Z", "eventId": eid}
    return f"id: {eid}\nevent: {etype}\ndata: {json.dumps(data)}\n\n"


def _sse(body):
    return httpx.Response(200, content=body.encode(), headers={"content-type": "text/event-stream"})


def _ok(data):
    def handler(request: httpx.Request) -> httpx.Response:
        handler.url = str(request.url)
        handler.method = request.method
        handler.body = request.content.decode() if request.content else ""
        return httpx.Response(200, json={"data": data, "meta": {}})

    handler.url = handler.method = handler.body = None
    return handler


class TestStudioWorkflows:
    def test_list_and_filters(self):
        h = _ok([])
        c = _client(h)
        c.studio.workflows.list(status="active", limit=10)
        assert h.method == "GET"
        assert "https://acme.api.k9o.io/@portal/workflows" in h.url
        assert "status=active" in h.url and "limit=10" in h.url

    def test_create_read_update_delete(self):
        h = _ok({})
        c = _client(h)
        c.studio.workflows.create({"name": "onboard", "triggerType": "manual", "triggerConfig": {}, "actions": []})
        assert h.method == "POST" and h.url == "https://acme.api.k9o.io/@portal/workflows"
        c.studio.workflows.read("wfl_x")
        assert h.url == "https://acme.api.k9o.io/@portal/workflows/wfl_x"
        c.studio.workflows.update("wfl_x", {"status": "archived"})
        assert h.method == "PATCH"
        c.studio.workflows.delete("wfl_x")
        assert h.method == "DELETE"

    def test_trigger_and_runs(self):
        h = _ok({})
        c = _client(h)
        c.studio.workflows.trigger("wfl_x", {"foo": "bar"})
        assert h.method == "POST" and h.url == "https://acme.api.k9o.io/@portal/workflows/wfl_x/runs"
        assert json.loads(h.body) == {"triggerContext": {"foo": "bar"}}
        c.studio.workflows.runs.list("wfl_x", limit=5)
        assert "workflows/wfl_x/runs" in h.url and "limit=5" in h.url
        c.studio.workflows.runs.read("wfl_x", "wfr_y")
        assert h.url == "https://acme.api.k9o.io/@portal/workflows/wfl_x/runs/wfr_y"
        c.studio.workflows.runs.cancel("wfl_x", "wfr_y")
        assert h.url == "https://acme.api.k9o.io/@portal/workflows/wfl_x/runs/wfr_y/cancel"


class TestStudioDashboards:
    def test_crud_pin_unpin_data(self):
        h = _ok({})
        c = _client(h)
        c.studio.dashboards.list()
        assert h.url == "https://acme.api.k9o.io/@portal/dashboards"
        c.studio.dashboards.create({"name": "ops"})
        assert h.method == "POST"
        c.studio.dashboards.pin("dsh_x")
        assert h.url == "https://acme.api.k9o.io/@portal/dashboards/dsh_x/pin"
        c.studio.dashboards.unpin("dsh_x")
        assert h.url == "https://acme.api.k9o.io/@portal/dashboards/dsh_x/unpin"


class TestComputeLlms:
    def test_list_read_invoke(self):
        h = _ok({"content": "hi", "usage": {"totalTokens": 2}})
        c = _client(h)
        c.compute.llms.list()
        assert h.url == "https://acme.api.k9o.io/@portal/compute/llms"
        c.compute.llms.read("gpt-4o")
        assert h.url == "https://acme.api.k9o.io/@portal/compute/llms/gpt-4o"
        env = c.compute.llms.invoke("mistral-7b", prompt="hello", params={"maxTokens": 16})
        assert h.method == "POST" and h.url == "https://acme.api.k9o.io/@portal/compute/llms/mistral-7b/invoke"
        assert json.loads(h.body) == {"prompt": "hello", "maxTokens": 16}
        assert env.data["content"] == "hi"

    def test_invoke_stream_yields_chunks_until_done(self):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            sse = (
                'data: {"delta": "Hel"}\n\n'
                'data: {"delta": "lo", "done": true, "tokensUsed": 1}\n\n'
                "data: [DONE]\n\n"
            )
            return httpx.Response(200, content=sse.encode(), headers={"content-type": "text/event-stream"})

        c = _client(handler)

        async def collect():
            return [chunk async for chunk in c.compute.llms.invoke_stream("gpt-4o", prompt="hi")]

        chunks = asyncio.run(collect())
        assert captured["url"] == "https://acme.api.k9o.io/@portal/compute/llms/gpt-4o/invoke-stream"
        assert "".join(ch["delta"] for ch in chunks) == "Hello"
        assert chunks[1]["done"] is True


class TestOrgDefaultLlm:
    def test_get_and_set(self):
        h = _ok("mistral-7b")
        c = create_client(base_url="https://acme.api.k9o.io")
        c._config.transport = httpx.MockTransport(h)
        c.org.default_llm.get()
        assert h.url == "https://acme.api.k9o.io/default-llm"
        c.org.default_llm.set(model_id="claude-3-5-sonnet")
        assert h.method == "PATCH"
        assert json.loads(h.body) == {"modelId": "claude-3-5-sonnet"}


class TestRealtime:
    def test_topic_builders(self):
        assert realtime_topic.collection("orders") == "collections.orders.*"
        assert realtime_topic.workflow_runs() == "studio.workflows.runs.*"
        assert realtime_topic.dashboard_refresh("dsh_x") == "studio.dashboards.refresh.dsh_x"
        assert realtime_topic.function_invocations("fn_x") == "forge.functions.invocations.fn_x"
        assert realtime_topic.audit_org("role_granted") == "audit.org.role_granted"

    def test_subscribe_sse_yields_events(self):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            sse = _frame("evt_a", "row_inserted") + _frame("evt_b", "row_updated")
            return httpx.Response(200, content=sse.encode(), headers={"content-type": "text/event-stream"})

        c = _client(handler)

        async def collect():
            out = []
            async for e in c.realtime.subscribe(realtime_topic.collection("orders")):
                out.append(e)
                if len(out) == 2:
                    break
            return out

        seen = asyncio.run(collect())
        assert "realtime/subscribe" in captured["url"] and "topic=collections.orders" in captured["url"]
        assert [e["eventType"] for e in seen] == ["row_inserted", "row_updated"]
        assert [e["eventId"] for e in seen] == ["evt_a", "evt_b"]



class TestRealtimeResume:
    @pytest.fixture(autouse=True)
    def _fast(self, monkeypatch):
        import k9o.realtime as rt
        monkeypatch.setattr(rt, "_DEFAULT_RETRY_S", 0.01)

    @staticmethod
    def _run(c, n, **kw):
        async def go():
            out = []
            async for e in c.realtime.subscribe("collections.orders.*", **kw):
                out.append(e)
                if len(out) >= n:
                    break
            return out
        return asyncio.run(asyncio.wait_for(go(), 10))

    @staticmethod
    def _scripted(responses, seen):
        it = iter(responses)

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "HEAD":  # regional-origin probe
                return httpx.Response(200)
            seen.append(request.headers.get("last-event-id"))
            r = next(it)
            return r(request) if callable(r) else r
        return handler

    def test_reconnects_with_last_event_id_and_drops_duplicates_and_heartbeats(self):
        seen = []
        h = self._scripted([
            _sse("retry: 10\n:ok\n\n" + _frame("evt_a") + ":hb\n\n" + _frame("evt_b")),
            _sse(_frame("evt_a") + _frame("evt_b") + ":hb\n\n" + _frame("evt_c")),
        ], seen)
        events = self._run(_client(h), 3)
        assert [e["eventId"] for e in events] == ["evt_a", "evt_b", "evt_c"]
        assert seen == [None, "evt_b"]

    def test_resync_calls_handler_is_not_yielded_and_next_connection_is_fresh(self):
        seen = []
        infos = []
        resync = "event: resync\ndata: " + json.dumps({"reason": "cursor-unknown", "action": "reread-source"}) + "\n\n"
        h = self._scripted([
            _sse(_frame("evt_a") + resync + _frame("evt_zzz")),
            _sse(_frame("evt_a")),
        ], seen)

        async def on_resync(info):
            infos.append(info)

        events = self._run(_client(h), 2, on_resync=on_resync)
        assert [e["eventId"] for e in events] == ["evt_a", "evt_a"]  # dedupe set cleared
        assert infos == [{"reason": "cursor-unknown", "action": "reread-source"}]
        assert seen == [None, None]

    OPENING = ("retry: 10\n:ok\n\nevent: resync\ndata: "
               + json.dumps({"reason": "cursor-evicted", "head": 7, "action": "reread-source"}) + "\n\n")

    def test_opening_resync_on_a_fresh_connection_is_informational_and_events_follow_on_the_same_connection(self):
        seen = []
        infos = []
        h = self._scripted([_sse(self.OPENING + _frame("evt_a") + _frame("evt_b"))], seen)
        events = self._run(_client(h), 2, on_resync=infos.append)
        assert [e["eventId"] for e in events] == ["evt_a", "evt_b"]
        assert infos == []
        assert seen == [None]  # one connection, no reconnect

    def test_resync_after_an_event_is_a_real_resync(self):
        seen = []
        infos = []
        h = self._scripted([_sse(self.OPENING + _frame("evt_a") + self.OPENING), _sse(_frame("evt_c"))], seen)
        events = self._run(_client(h), 2, on_resync=infos.append)
        assert [e["eventId"] for e in events] == ["evt_a", "evt_c"]
        assert infos == [{"reason": "cursor-evicted", "head": 7, "action": "reread-source"}]
        assert seen == [None, None]

    def test_resync_on_a_connection_that_sent_last_event_id_is_a_real_resync(self):
        seen = []
        infos = []
        h = self._scripted([_sse(_frame("evt_a")), _sse(self.OPENING), _sse(_frame("evt_c"))], seen)
        events = self._run(_client(h), 2, on_resync=infos.append)
        assert [e["eventId"] for e in events] == ["evt_a", "evt_c"]
        assert infos == [{"reason": "cursor-evicted", "head": 7, "action": "reread-source"}]
        assert seen == [None, "evt_a", None]

    def test_client_error_is_thrown_once(self):
        seen = []
        body = {"code": "K9O_UNAUTHORIZED", "message": "no"}
        h = self._scripted([httpx.Response(401, json=body), _sse(_frame("evt_a"))], seen)
        with pytest.raises(KError):
            self._run(_client(h), 1)
        assert len(seen) == 1

    def test_server_error_is_retried(self):
        seen = []
        h = self._scripted([httpx.Response(503, text="down"), _sse(_frame("evt_a"))], seen)
        events = self._run(_client(h), 1)
        assert [e["eventId"] for e in events] == ["evt_a"]
        assert len(seen) == 2

    def test_transport_error_is_retried(self):
        seen = []

        def boom(request):
            raise httpx.ConnectError("refused")
        h = self._scripted([boom, _sse(_frame("evt_a"))], seen)
        assert len(self._run(_client(h), 1)) == 1
        assert len(seen) == 2

    def test_closing_the_iterator_stops_reconnecting(self):
        seen = []
        h = self._scripted([_sse(_frame("evt_a")) for _ in range(5)], seen)
        events = self._run(_client(h), 1)
        assert len(events) == 1
        asyncio.run(asyncio.sleep(0.1))
        assert len(seen) == 1

    def test_idle_connection_is_abandoned_and_reconnected(self):
        seen = []

        class Hang(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b":ok\n\n"
                await asyncio.sleep(30)

        h = self._scripted([
            lambda r: httpx.Response(200, stream=Hang(), headers={"content-type": "text/event-stream"}),
            _sse(_frame("evt_a")),
        ], seen)
        events = self._run(_client(h), 1, idle_timeout=0.1)
        assert [e["eventId"] for e in events] == ["evt_a"]
        assert len(seen) == 2

    def test_frame_without_event_type_is_skipped(self):
        seen = []
        h = self._scripted([_sse('data: {"topic":"t","eventId":"evt_x"}\n\n' + _frame("evt_a"))], seen)
        assert [e["eventId"] for e in self._run(_client(h), 1)] == ["evt_a"]
