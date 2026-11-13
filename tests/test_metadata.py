"""client.metadata: method, path, query and body of every call, the metadata. prefix, validation that sends nothing,
error decoding, and subscribe over the shared change-feed reader. Fixtures follow the documented gateway answers."""

from __future__ import annotations

import asyncio
import json
from urllib.parse import parse_qsl

import httpx
import pytest

from k9o import KError, MetadataPage, create_client

BASE = "https://acme.api.k9o.io"
M = "/@portal/metadata"

P1 = {"surface": "collection:posts", "scope": "posts", "recordId": "p1",
      "metadata": {"title": "Quarterly report", "body": "...", "tags": ["pinned", "finance"], "priority": 3,
                   "embedding": "[1,0,0]"}}
TELEMETRY = {"surface": "telemetry:collection.metadata_index_declared", "scope": "collection.metadata_index_declared",
             "eventId": "aud_1", "metadata": {"id": "aud_1", "actorId": "usr_1"}}


def _client(status=200, body=None):
    seen: list[httpx.Request] = []

    def handler(req):
        if req.method == "HEAD":
            return httpx.Response(200)
        seen.append(req)
        return httpx.Response(status, json=body if body is not None else {"data": [P1], "meta": {"cursor": {"next": None}}})

    c = create_client(base_url=BASE, project_slug="portal", api_key="k")
    c.config.transport = httpx.MockTransport(handler)
    return c, seen


def _body(req):
    return json.loads(req.content) if req.content else None


def _raises(call) -> KError:
    with pytest.raises(KError) as info:
        call()
    return info.value


def test_requires_project_slug():
    c = create_client(base_url=BASE, api_key="k")
    with pytest.raises(ValueError):
        c.metadata.filter(["collection:posts"])


# filter -------------------------------------------------------------------------------------------------------------

def test_filter_prefixes_fields_and_returns_rows_with_cursor():
    c, seen = _client(body={"data": [P1], "meta": {"requestId": "r1", "cursor": {"next": "cur_1"}}})
    page = c.metadata.filter(["collection:posts"],
                             where=[{"field": "priority", "op": "gte", "value": 2},
                                    {"field": "metadata.title", "op": "startsWith", "value": "Q"}],
                             limit=1, cursor="cur_0")
    (req,) = seen
    assert (req.method, req.url.path) == ("POST", f"{M}/query")
    assert _body(req) == {
        "surfaces": ["collection:posts"],
        "where": [{"field": "metadata.priority", "op": "gte", "value": 2},
                  {"field": "metadata.title", "op": "startsWith", "value": "Q"}],
        "limit": 1, "cursor": "cur_0"}
    assert isinstance(page, MetadataPage)
    assert page.cursor == "cur_1" and page.meta.cursor.next == "cur_1"
    assert page.data[0]["recordId"] == "p1" and page.data[0]["metadata"]["priority"] == 3


def test_filter_sends_only_surfaces_and_reports_null_cursor():
    c, seen = _client(body={"data": [P1, TELEMETRY], "meta": {"cursor": {"next": None}}})
    page = c.metadata.filter(["collection:posts", "telemetry:collection.metadata_index_declared"])
    assert _body(seen[0]) == {"surfaces": ["collection:posts", "telemetry:collection.metadata_index_declared"]}
    assert page.cursor is None and page.data[1]["eventId"] == "aud_1"


def test_filter_without_cursor_in_meta_gives_none():
    c, _ = _client(body={"data": [], "meta": {}})
    assert c.metadata.filter(["collection:posts"]).cursor is None


def test_filter_keeps_telemetry_fields_bare():
    c, seen = _client()
    c.metadata.filter(["telemetry:collection."], where=[
        {"field": "createdAt", "op": "gt", "value": "2026-10-26T00:00:00Z"}, {"field": "actorId", "op": "eq", "value": "usr_1"}])
    assert _body(seen[0])["where"] == [
        {"field": "createdAt", "op": "gt", "value": "2026-10-26T00:00:00Z"}, {"field": "actorId", "op": "eq", "value": "usr_1"}]


def test_filter_prefixes_created_at_without_a_telemetry_surface():
    c, seen = _client()
    c.metadata.filter(["collection:posts"], where=[{"field": "createdAt", "op": "gt", "value": 1}])
    assert _body(seen[0])["where"][0]["field"] == "metadata.createdAt"


def test_filter_passes_storage_surface_and_decodes_501():
    c, seen = _client(501, {"code": "K9O_NOT_IMPLEMENTED", "message": "storage metadata is not available",
                            "context": {"surface": "storage:photos"}})
    e = _raises(lambda: c.metadata.filter(["storage:photos"]))
    assert _body(seen[0]) == {"surfaces": ["storage:photos"]}
    assert (e.code, e.status, e.context["surface"]) == ("K9O_NOT_IMPLEMENTED", 501, "storage:photos")


def test_filter_decodes_413_and_400_not_indexed():
    c, _ = _client(413, {"code": "K9O_QUERY_RESULT_TOO_LARGE", "message": "too many rows",
                         "context": {"cap": 10000, "surfaces": ["collection:posts"]}, "recommendation": "Add where."})
    e = _raises(lambda: c.metadata.filter(["collection:posts"]))
    assert (e.code, e.status, e.context["cap"], e.recommendation) == ("K9O_QUERY_RESULT_TOO_LARGE", 413, 10000, "Add where.")

    c, _ = _client(400, {"code": "K9O_METADATA_FIELD_NOT_INDEXED", "message": "not declared",
                         "context": {"surface": "collection:posts", "field": "metadata.secret"},
                         "recommendation": {"action": "declare it"}})
    e = _raises(lambda: c.metadata.filter(["collection:posts"], where=[{"field": "secret", "op": "eq", "value": 1}]))
    assert (e.code, e.status, e.context["field"], e.recommendation) == (
        "K9O_METADATA_FIELD_NOT_INDEXED", 400, "metadata.secret", "declare it")


def test_filter_decodes_extension_permission_denied_and_encrypted_column():
    c, _ = _client(403, {"code": "K9O_EXTENSION_PERMISSION_DENIED", "message": "no", "context": {}})
    assert _raises(lambda: c.metadata.filter(["collection:posts"])).code == "K9O_EXTENSION_PERMISSION_DENIED"
    c, _ = _client(400, {"code": "K9O_ENCRYPTED_COLUMN_OPERATION_UNSUPPORTED", "message": "x",
                         "context": {"column": "ssn", "op": "gt"}})
    assert _raises(lambda: c.metadata.filter(["collection:posts"])).context["op"] == "gt"


BAD_FILTERS = [
    dict(surfaces=[]),
    dict(surfaces="collection:posts"),
    dict(surfaces=["posts"]),
    dict(surfaces=["table:posts"]),
    dict(surfaces=["collection:"]),
    dict(surfaces=["collection:1posts"]),
    dict(surfaces=["collection:posts", "telemetry:x"], cursor="c"),
    dict(surfaces=["collection:posts"], cursor=""),
    dict(surfaces=["collection:posts"], limit=0),
    dict(surfaces=["collection:posts"], limit=1001),
    dict(surfaces=["collection:posts"], limit=1.5),
    dict(surfaces=["collection:posts"], limit=True),
    dict(surfaces=["collection:posts"], where=[{"field": "a", "op": "like"}]),
    dict(surfaces=["collection:posts"], where=[{"field": "", "op": "eq"}]),
    dict(surfaces=["collection:posts"], where=[{"op": "eq"}]),
    dict(surfaces=["collection:posts"], where=["x"]),
    dict(surfaces=["collection:posts"], where="x"),
]


@pytest.mark.parametrize("kwargs", BAD_FILTERS)
def test_filter_refuses_bad_input_before_sending(kwargs):
    c, seen = _client()
    e = _raises(lambda: c.metadata.filter(kwargs.pop("surfaces"), **kwargs))
    assert e.code in ("K9O_INVALID_FIELD_VALUE", "K9O_UNKNOWN_SURFACE")
    assert seen == []


def test_filter_unknown_surface_code():
    c, seen = _client()
    assert _raises(lambda: c.metadata.filter(["posts"])).code == "K9O_UNKNOWN_SURFACE"
    assert seen == []


# set ----------------------------------------------------------------------------------------------------------------

def test_set_patches_bare_columns_and_returns_the_record():
    row = {"id": "p2", "title": "Team offsite 2027", "priority": 9, "secret": "s2"}
    c, seen = _client(body={"data": row, "meta": {"requestId": "r2"}})
    env = c.metadata.set("posts", "p 2", {"title": "Team offsite 2027", "metadata.priority": 9})
    (req,) = seen
    assert req.method == "PATCH" and req.url.raw_path.decode() == f"{M}/collection/posts/p%202"
    assert _body(req) == {"metadata": {"title": "Team offsite 2027", "priority": 9}}
    assert env.data == row


def test_set_refuses_bad_input_before_sending():
    c, seen = _client()
    for call in (lambda: c.metadata.set("bad-name", "p1", {"a": 1}), lambda: c.metadata.set("posts", "", {"a": 1}),
                 lambda: c.metadata.set("posts", "p1", {}), lambda: c.metadata.set("posts", "p1", [("a", 1)]),
                 lambda: c.metadata.set("posts", None, {"a": 1})):
        assert _raises(call).code == "K9O_INVALID_FIELD_VALUE"
    assert seen == []


def test_set_decodes_not_declared():
    c, _ = _client(400, {"code": "K9O_METADATA_FIELD_NOT_INDEXED", "message": "x", "context": {"field": "metadata.nope"}})
    assert _raises(lambda: c.metadata.set("posts", "p1", {"nope": 1})).code == "K9O_METADATA_FIELD_NOT_INDEXED"


# search -------------------------------------------------------------------------------------------------------------

SEARCH = {"data": [{"surface": "collection:posts", "scope": "posts", "recordId": "p3",
                    "metadata": {"title": "Budget draft", "body": "draft budget, finance review"}}],
          "meta": {"cursor": {"next": None}}}


def test_search_posts_surface_prefixed_field_q_and_limit():
    c, seen = _client(body=SEARCH)
    page = c.metadata.search(surface="collection:posts", field="body", q="finance", limit=5)
    (req,) = seen
    assert (req.method, req.url.path) == ("POST", f"{M}/search")
    assert _body(req) == {"surface": "collection:posts", "field": "metadata.body", "q": "finance", "limit": 5}
    assert [r["recordId"] for r in page.data] == ["p3"] and page.cursor is None


def test_search_keeps_prefix_and_omits_limit():
    c, seen = _client(body=SEARCH)
    c.metadata.search(surface="collection:posts", field="metadata.body", q="x")
    assert _body(seen[0]) == {"surface": "collection:posts", "field": "metadata.body", "q": "x"}


def test_search_refuses_bad_input_before_sending():
    c, seen = _client(body=SEARCH)
    for kw in (dict(surface="telemetry:x", field="body", q="a"), dict(surface="storage:b", field="body", q="a"),
               dict(surface="collection:posts", field="", q="a"), dict(surface="collection:posts", field="body", q=""),
               dict(surface="collection:posts", field="body", q="a", limit=2000), dict(surface="posts", field="b", q="a")):
        _raises(lambda: c.metadata.search(**kw))
    assert seen == []


def test_search_decodes_encrypted_column():
    c, _ = _client(400, {"code": "K9O_ENCRYPTED_COLUMN_OPERATION_UNSUPPORTED", "message": "x",
                         "context": {"column": "body", "op": "textSearch"}})
    assert _raises(lambda: c.metadata.search(surface="collection:posts", field="body", q="a")).context["op"] == "textSearch"


# vectors.nearest ----------------------------------------------------------------------------------------------------

def test_nearest_posts_prefixed_column_embedding_and_options():
    c, seen = _client()
    page = c.metadata.vectors.nearest(surface="collection:posts", column="embedding", embedding=[1, 0, 0], k=3,
                                      metric="dot", where=[{"field": "priority", "op": "gt", "value": 1}])
    (req,) = seen
    assert (req.method, req.url.path) == ("POST", f"{M}/vectors/nearest")
    assert _body(req) == {"surface": "collection:posts", "column": "metadata.embedding", "embedding": [1, 0, 0],
                          "k": 3, "metric": "dot", "where": [{"field": "metadata.priority", "op": "gt", "value": 1}]}
    assert page.data[0]["recordId"] == "p1" and "score" not in page.data[0]


def test_nearest_sends_only_required_fields_by_default():
    c, seen = _client()
    c.metadata.vectors.nearest(surface="collection:posts", column="metadata.embedding", embedding=(0.5,))
    assert _body(seen[0]) == {"surface": "collection:posts", "column": "metadata.embedding", "embedding": [0.5]}


def test_nearest_refuses_bad_input_before_sending():
    c, seen = _client()
    base = dict(surface="collection:posts", column="embedding", embedding=[1])
    for override in (dict(embedding=[]), dict(embedding=[1, float("nan")]), dict(embedding=[float("inf")]),
                     dict(embedding=[True]), dict(embedding="abc"), dict(k=0), dict(k=1001), dict(metric="l2"),
                     dict(surface="telemetry:x"), dict(column="")):
        _raises(lambda: c.metadata.vectors.nearest(**{**base, **override}))
    assert seen == []


# index --------------------------------------------------------------------------------------------------------------

INDEX = {"data": {"collection": "posts", "columns": [{"column": "body", "indexed": False}, {"column": "tags", "indexed": True}]},
         "meta": {}}


def test_index_get():
    c, seen = _client(body=INDEX)
    env = c.metadata.index.get("posts")
    assert (seen[0].method, seen[0].url.path, seen[0].content) == ("GET", f"{M}/index/posts", b"")
    assert env.data["columns"][1] == {"column": "tags", "indexed": True}


def test_index_declare_puts_the_columns():
    c, seen = _client(body=INDEX)
    c.metadata.index.declare("posts", [{"column": "body"}, {"column": "tags", "indexed": True}])
    assert (seen[0].method, seen[0].url.path) == ("PUT", f"{M}/index/posts")
    assert _body(seen[0]) == {"columns": [{"column": "body"}, {"column": "tags", "indexed": True}]}


def test_index_declare_empty_clears_and_64_columns_pass():
    c, seen = _client(body=INDEX)
    c.metadata.index.declare("posts", [])
    c.metadata.index.declare("posts", [{"column": f"c{i}"} for i in range(64)])
    assert _body(seen[0]) == {"columns": []} and len(_body(seen[1])["columns"]) == 64


def test_index_refuses_bad_input_before_sending():
    c, seen = _client(body=INDEX)
    for call in (lambda: c.metadata.index.declare("posts", [{"column": f"c{i}"} for i in range(65)]),
                 lambda: c.metadata.index.declare("posts", [{"column": "id"}]),
                 lambda: c.metadata.index.declare("posts", [{"column": "a"}, {"column": "a"}]),
                 lambda: c.metadata.index.declare("posts", [{"column": "a b"}]),
                 lambda: c.metadata.index.declare("posts", [{"column": "a", "indexed": "yes"}]),
                 lambda: c.metadata.index.declare("posts", "a"),
                 lambda: c.metadata.index.declare("bad-name", []),
                 lambda: c.metadata.index.get("bad-name")):
        assert _raises(call).code == "K9O_INVALID_FIELD_VALUE"
    assert seen == []


def test_index_decodes_missing_collection_and_refused_role():
    c, _ = _client(404, {"code": "K9O_COLLECTION_NOT_FOUND", "message": "x"})
    assert _raises(lambda: c.metadata.index.declare("ghost", [])).status == 404
    c, _ = _client(403, {"code": "K9O_IAM_POLICY_DENIED", "message": "x"})
    assert _raises(lambda: c.metadata.index.declare("posts", [])).code == "K9O_IAM_POLICY_DENIED"


# subscribe ----------------------------------------------------------------------------------------------------------

HEAD = "retry: 10\n:ok\n\n"
RESYNC = 'event: resync\ndata: {"reason": "cursor-evicted", "head": 9, "action": "reread-source"}\n\n'


def _frame(sse_id, event_type="row_updated", record_id="p2"):
    data = {"topic": f"collections.posts.{event_type}", "eventType": event_type,
            "payload": {"collection": "posts", "id": record_id}, "occurredAt": "2026-10-27T10:20:38.969Z"}
    return f"id: {sse_id}\nevent: {event_type}\ndata: {json.dumps(data)}\n\n"


def _feed_client(bodies):
    conns: list[httpx.Request] = []

    def handler(req):
        if req.method == "HEAD":
            return httpx.Response(200)
        conns.append(req)
        text = bodies[min(len(conns) - 1, len(bodies) - 1)]
        return httpx.Response(200, content=text.encode(), headers={"content-type": "text/event-stream"})

    c = create_client(base_url=BASE, project_slug="portal", api_key="k")
    c.config.transport = httpx.MockTransport(handler)
    return c, conns


def _take(feed, n):
    async def go():
        out = []
        async for ev in feed:
            out.append(ev)
            if len(out) == n:
                break
        return out

    return asyncio.run(go())


def _query(req):
    return dict(parse_qsl(req.url.query.decode()))


def test_subscribe_opens_the_metadata_feed_with_surface_and_from_sequence():
    c, conns = _feed_client([HEAD + _frame("1:5")])
    (ev,) = _take(c.metadata.subscribe("collection:posts", from_sequence=4), 1)
    req = conns[0]
    assert (req.method, req.url.path) == ("GET", f"{M}/subscribe")
    assert _query(req) == {"surface": "collection:posts", "fromSequence": "4"}
    assert "last-event-id" not in req.headers
    assert (ev["eventType"], ev["eventId"], ev["payload"]) == ("row_updated", "1:5", {"collection": "posts", "id": "p2"})


def test_subscribe_omits_from_sequence_when_not_given():
    c, conns = _feed_client([HEAD + _frame("1:5")])
    _take(c.metadata.subscribe("collection:posts"), 1)
    assert _query(conns[0]) == {"surface": "collection:posts"}


def test_subscribe_reconnects_with_last_event_id_drops_resent_frame_and_not_from_sequence():
    c, conns = _feed_client([HEAD + _frame("1:5") + _frame("1:6", "row_inserted", "p9"),
                             HEAD + _frame("1:6", "row_inserted", "p9") + _frame("1:7", "row_deleted", "p1")])
    got = _take(c.metadata.subscribe("collection:posts", from_sequence=4), 3)
    assert [(e["eventId"], e["eventType"], e["payload"]["id"]) for e in got] == [
        ("1:5", "row_updated", "p2"), ("1:6", "row_inserted", "p9"), ("1:7", "row_deleted", "p1")]
    assert conns[1].headers["last-event-id"] == "1:6"
    assert _query(conns[1]) == {"surface": "collection:posts"}


def test_subscribe_keeps_from_sequence_until_a_connection_is_answered():
    seen: list[httpx.Request] = []

    def handler(req):
        if req.method == "HEAD":
            return httpx.Response(200)
        seen.append(req)
        if len(seen) == 1:
            return httpx.Response(503, json={"code": "K9O_INTERNAL_ERROR", "message": "x"})
        return httpx.Response(200, content=(HEAD + _frame("2:1")).encode(), headers={"content-type": "text/event-stream"})

    c = create_client(base_url=BASE, project_slug="portal", api_key="k")
    c.config.transport = httpx.MockTransport(handler)
    feed = c.metadata.subscribe("collection:posts", from_sequence=3)

    async def go():
        async for ev in feed:
            return ev

    # the 5xx retry waits the default delay
    asyncio.run(asyncio.wait_for(go(), 10))
    assert [_query(r) for r in seen] == [{"surface": "collection:posts", "fromSequence": "3"}] * 2


def test_subscribe_calls_on_resync_and_restarts_without_last_event_id_or_from_sequence():
    c, conns = _feed_client([HEAD + RESYNC, HEAD + _frame("2:1")])
    infos: list[dict] = []
    (ev,) = _take(c.metadata.subscribe("collection:posts", from_sequence=3, on_resync=infos.append), 1)
    assert ev["eventId"] == "2:1"
    assert infos == [{"reason": "cursor-evicted", "head": 9, "action": "reread-source"}]
    assert _query(conns[0]) == {"surface": "collection:posts", "fromSequence": "3"}
    assert "last-event-id" not in conns[1].headers
    assert _query(conns[1]) == {"surface": "collection:posts"}


def test_subscribe_opening_resync_is_informational_and_the_event_after_it_is_delivered():
    c, conns = _feed_client([HEAD + RESYNC + _frame("2:1")])
    infos: list[dict] = []
    (ev,) = _take(c.metadata.subscribe("collection:posts", on_resync=infos.append), 1)
    assert ev["eventId"] == "2:1"
    assert infos == [] and len(conns) == 1


def test_subscribe_resync_on_a_resumed_connection_is_a_real_resync():
    # Connection 1 delivers an event and ends; connection 2 sends Last-Event-ID and is answered with a resync.
    c, conns = _feed_client([HEAD + _frame("1:1"), HEAD + RESYNC, HEAD + _frame("3:1")])
    infos: list[dict] = []
    got = _take(c.metadata.subscribe("collection:posts", on_resync=infos.append), 2)
    assert [e["eventId"] for e in got] == ["1:1", "3:1"]
    assert infos == [{"reason": "cursor-evicted", "head": 9, "action": "reread-source"}]
    assert conns[1].headers["last-event-id"] == "1:1" and "last-event-id" not in conns[2].headers


def test_subscribe_refuses_bad_input_before_connecting():
    c, conns = _feed_client([HEAD])
    for call in (lambda: c.metadata.subscribe("telemetry:x"), lambda: c.metadata.subscribe("storage:b"),
                 lambda: c.metadata.subscribe("posts"),
                 lambda: c.metadata.subscribe("collection:posts", from_sequence=-1),
                 lambda: c.metadata.subscribe("collection:posts", from_sequence=1.5)):
        _raises(call)
    assert conns == []


def test_subscribe_4xx_raises_once():
    def handler(req):
        if req.method == "HEAD":
            return httpx.Response(200)
        return httpx.Response(403, json={"code": "K9O_EXTENSION_PERMISSION_DENIED", "message": "no"})

    c = create_client(base_url=BASE, project_slug="portal", api_key="k")
    c.config.transport = httpx.MockTransport(handler)
    with pytest.raises(KError) as info:
        _take(c.metadata.subscribe("collection:posts"), 1)
    assert (info.value.code, info.value.status) == ("K9O_EXTENSION_PERMISSION_DENIED", 403)
