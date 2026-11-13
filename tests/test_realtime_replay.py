"""realtime.replay: POST /@<project>/realtime/replay (mocked transport)."""

from __future__ import annotations

import json

import httpx
import pytest

from k9o import KError, RealtimeReplayPage, create_client

BASE = "https://acme.api.k9o.io"
P = "/@portal"


def _event(seq: int) -> dict:
    return {"sequence": str(seq), "topic": "audit.project.data.write", "eventType": "data.write",
            "payload": {"auditEventId": f"evt_{seq}"}, "occurredAt": "2026-10-27T10:00:00Z"}


def _client(answer, **kw):
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "POST":
            seen.append(req)
        return answer(req) if callable(answer) else answer

    c = create_client(base_url=BASE, api_key="k", **({"project_slug": "portal"} | kw))
    c.config.transport = httpx.MockTransport(handler)
    return c, seen


def _ok(data: dict) -> httpx.Response:
    return httpx.Response(200, json={"data": data, "meta": {}})


def test_replay_sends_topic_only_by_default_and_decodes_the_page():
    c, seen = _client(_ok({"topic": "audit.project.*", "events": [_event(1), _event(2)], "nextCursor": "2", "hasMore": True}))
    page = c.realtime.replay("audit.project.*")
    assert isinstance(page, RealtimeReplayPage)
    assert (seen[0].method, seen[0].url.path, json.loads(seen[0].content)) == ("POST", f"{P}/realtime/replay", {"topic": "audit.project.*"})
    assert page.topic == "audit.project.*" and page.next_cursor == "2" and page.has_more is True and page.resync is None
    assert [e["sequence"] for e in page.events] == ["1", "2"] and page.events[0]["eventType"] == "data.write"


def test_replay_sends_every_option():
    c, seen = _client(_ok({"topic": "audit.project.*", "events": [], "nextCursor": None, "hasMore": False}))
    c.realtime.replay("audit.project.*", from_sequence=0, until_sequence=9, limit=10000)
    assert json.loads(seen[0].content) == {"topic": "audit.project.*", "fromSequence": 0, "untilSequence": 9, "limit": 10000}


def test_replay_of_a_topic_without_a_log_reports_reread_source():
    c, _ = _client(_ok({"topic": "collections.orders.*", "events": [], "nextCursor": None, "hasMore": False, "resync": "reread-source"}))
    page = c.realtime.replay("collections.orders.*")
    assert page.events == [] and page.resync == "reread-source" and page.has_more is False and page.next_cursor is None


@pytest.mark.parametrize("args,kwargs", [
    ((), {"topic": ""}), (("x" * 201,), {}), ((5,), {}),
    (("t",), {"from_sequence": -1}), (("t",), {"from_sequence": True}), (("t",), {"until_sequence": 1.5}),
    (("t",), {"limit": 0}), (("t",), {"limit": 10001}), (("t",), {"limit": True}),
])
def test_replay_validates_before_sending(args, kwargs):
    c, seen = _client(_ok({}))
    with pytest.raises(KError) as e:
        c.realtime.replay(*args, **kwargs)
    assert e.value.code == "K9O_INVALID_FIELD_VALUE" and seen == []


def test_replay_needs_a_bound_project():
    c, seen = _client(_ok({}), project_slug=None)
    with pytest.raises(ValueError):
        c.realtime.replay("audit.project.*")
    assert seen == []


@pytest.mark.parametrize("status,code", [(400, "K9O_INVALID_FILTER"), (403, "K9O_RBAC_DENY"), (400, "K9O_INVALID_CURSOR")])
def test_replay_raises_gateway_errors(status, code):
    c, _ = _client(httpx.Response(status, json={"code": code, "message": "no", "context": {}}))
    with pytest.raises(KError) as e:
        c.realtime.replay("audit.project.*")
    assert e.value.code == code and e.value.status == status
