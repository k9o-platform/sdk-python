"""client.analytics, client.rollups, client.keys, audit.tail / audit.replay and the Rampart windows claim.

A stubbed transport answers every route; each test checks method, path, query, body and how the answer decodes."""

from __future__ import annotations

import asyncio
import json
from urllib.parse import parse_qsl

import httpx
import pytest

from k9o import KError, OriginalFile, RampartInputTooLong, create_client

BASE = "https://acme.api.k9o.io"
P = "/@portal"


def _client(status=200, data=None, headers=None, body=None, content=None):
    seen: list[httpx.Request] = []

    def handler(req):
        if req.method == "HEAD":
            return httpx.Response(200)
        seen.append(req)
        if content is not None:
            return httpx.Response(status, content=content, headers=headers)
        return httpx.Response(status, json=body if body is not None else {"data": data, "meta": {}}, headers=headers)

    c = create_client(base_url=BASE, project_slug="portal", api_key="k")
    c.config.transport = httpx.MockTransport(handler)
    return c, seen


def _json(req):
    return json.loads(req.content) if req.content else None


def _q(req):
    return dict(parse_qsl(req.url.query.decode()))


def _raises(call) -> KError:
    with pytest.raises(KError) as info:
        call()
    return info.value


# datasets -----------------------------------------------------------------------------------------------------------

def test_namespaces_need_a_project_slug():
    c = create_client(base_url=BASE, api_key="k")
    for call in (c.analytics.list, c.rollups.list, c.keys.list):
        with pytest.raises(ValueError):
            call()


def test_list_get_delete_decode_the_answer():
    c, seen = _client(data=[{"slug": "sales", "displayName": "Sales", "partitionKeys": ["region"]}])
    assert c.analytics.list().data[0]["slug"] == "sales"
    assert (seen[0].method, seen[0].url.path) == ("GET", f"{P}/analytics")
    c, seen = _client(data={"slug": "sales", "status": "retired"})
    assert c.analytics.delete("sales").data == {"slug": "sales", "status": "retired"}
    assert (seen[0].method, seen[0].url.path) == ("DELETE", f"{P}/analytics/sales")
    c, seen = _client(data={"slug": "sales"})
    c.analytics.get("sa les")
    assert seen[0].url.raw_path.decode() == f"{P}/analytics/sa%20les"


def test_create_sends_the_manifest_fields_only():
    c, seen = _client(data={"slug": "sales"})
    c.analytics.create("sales", display_name="Sales", columns=[{"name": "n", "type": "int64"}], residency_class="per-subject")
    assert _json(seen[0]) == {"displayName": "Sales", "columns": [{"name": "n", "type": "int64"}], "residencyClass": "per-subject"}


def test_create_and_other_calls_refuse_bad_input_before_sending():
    c, seen = _client()
    bad = [
        lambda: c.analytics.create("s", display_name="", columns=[{"name": "n", "type": "x"}], residency_class="shared"),
        lambda: c.analytics.create("s", display_name="S", columns=[], residency_class="shared"),
        lambda: c.analytics.create("s", display_name="S", columns=[{"name": "n"}], residency_class="shared"),
        lambda: c.analytics.create("s", display_name="S", columns=[{"name": "n", "type": "x"}], residency_class="private"),
        lambda: c.analytics.query("", ["a"]),
        lambda: c.analytics.query("SELECT 1", []),
        lambda: c.analytics.materialize("s"),
        lambda: c.analytics.materialize("s", from_collection="a", from_query="b"),
        lambda: c.analytics.write("s", []),
        lambda: c.analytics.write("s", [{"a": 1}], on_error="ignore"),
        lambda: c.analytics.upload("s", b"x", format="xml"),
        lambda: c.analytics.read("s", limit=0),
        lambda: c.analytics.rollups.create("s", slug="r", source="o", measures=[{"name": "n", "agg": "count"}], window_seconds=30, non_personal=True),
        lambda: c.analytics.rollups.create("s", slug="r", source="o", measures=[{"name": "n", "agg": "count"}], window_seconds=60),
        lambda: c.analytics.rollups.create("s", slug="r", source="o", measures=[{"name": "n", "agg": "count"}], window_seconds=60, subject_field="u", non_personal=True),
        lambda: c.analytics.rollups.reattest("s", "r", reattest_interval_days=10),
        lambda: c.analytics.schedules.create("s", from_collection="o", interval_seconds=59),
        lambda: c.analytics.schedules.reattest("s", ""),
        lambda: c.rollups.subscribe("r", from_sequence=-1),
    ]
    for call in bad:
        assert _raises(call).code == "K9O_INVALID_FIELD_VALUE"
    assert seen == []


def test_query_and_read_pages():
    c, seen = _client(body={"data": [{"_k9o_row_id": "r1", "city": "Berlin"}],
                            "meta": {"cursor": {"next": "cur_2"}, "schema": [{"name": "city", "type": "string"}]}})
    page = c.analytics.read("sales", limit=1)
    assert _json(seen[0]) == {"limit": 1}
    assert page.data[0]["_k9o_row_id"] == "r1" and page.meta.cursor.next == "cur_2"
    c.analytics.read("sales", cursor=page.meta.cursor.next)
    assert _json(seen[1]) == {"cursor": "cur_2"}
    c.analytics.read("sales")
    assert _json(seen[2]) == {}
    c.analytics.query("SELECT city FROM dataset.sales", ("sales",))
    assert _json(seen[3]) == {"sql": "SELECT city FROM dataset.sales", "datasets": ["sales"]}


def test_materialize_with_a_saved_query():
    c, seen = _client(data={"rowsRead": 2, "ingested": 2, "rejected": 0})
    assert c.analytics.materialize("sales", from_query="stq_1").data["ingested"] == 2
    assert _json(seen[0]) == {"fromQuery": "stq_1"}


def test_write_sends_a_json_array_and_decodes_the_counts():
    c, seen = _client(data={"ingested": 1, "skipped": 1, "skippedDetails": [{"row": 2}], "partitions": [], "bytes": 10})
    out = c.analytics.write("sales", [{"city": "Berlin"}, {"bad": 1}])
    assert _json(seen[0]) == [{"city": "Berlin"}, {"bad": 1}] and _q(seen[0]) == {}
    assert (out.data["ingested"], out.data["skipped"]) == (1, 1)


def test_upload_is_multipart_with_file_manifest_and_query():
    c, seen = _client(data={"ingested": 1})
    c.analytics.upload("sales", "city\nBerlin\n", format="csv", filename="sales.csv", manifest={"displayName": "Sales"},
                       has_header=False)
    req = seen[0]
    assert (req.method, req.url.path) == ("POST", f"{P}/analytics/sales/upload")
    assert _q(req) == {"format": "csv", "hasHeader": "false"}
    assert req.headers["content-type"].startswith("multipart/form-data; boundary=")
    body = req.content.decode()
    assert 'name="file"; filename="sales.csv"' in body and "city\nBerlin\n" in body
    assert 'name="manifest"' in body and '{"displayName": "Sales"}' in body


def test_download_original_returns_the_bytes_and_headers():
    c, seen = _client(content=b"a,b\n1,2\n", headers={"content-type": "text/csv",
                                                      "content-disposition": 'attachment; filename="sales.csv"'})
    f = c.analytics.download_original("sales")
    assert f == OriginalFile(content=b"a,b\n1,2\n", content_type="text/csv", filename="sales.csv")
    assert seen[0].headers["accept"] == "*/*" and seen[0].url.path == f"{P}/analytics/sales/upload/original"


def test_download_original_errors_arrive_as_kerror():
    c, _ = _client(status=404, body={"code": "K9O_NOT_FOUND", "message": "no upload"})
    e = _raises(lambda: c.analytics.download_original("sales"))
    assert (e.code, e.status) == ("K9O_NOT_FOUND", 404)


def test_rollups_create_modes():
    c, seen = _client(data={"id": "rol_1"})
    c.analytics.rollups.create("sales", slug="per_user", source="events", measures=[{"name": "s", "agg": "sum", "column": "v"}],
                               window_seconds=60, subject_field="user_id")
    assert _json(seen[0]) == {"slug": "per_user", "source": "events", "measures": [{"name": "s", "agg": "sum", "column": "v"}],
                              "windowSeconds": 60, "subjectField": "user_id"}


def test_attestation_state_decodes_and_reattest_posts_an_empty_body():
    c, seen = _client(data=[{"id": "rol_1", "attestationStatus": "expiring_soon", "expiresAt": "2026-11-01T00:00:00Z"}])
    assert c.analytics.rollups.list("sales").data[0]["attestationStatus"] == "expiring_soon"
    c.analytics.schedules.reattest("sales", "sch_1")
    assert _json(seen[1]) == {} and seen[1].url.path == f"{P}/analytics/sales/schedules/sch_1/reattest"


def test_reattest_by_a_service_token_is_human_only():
    body = {"code": "K9O_RBAC_DENY", "message": "only a person", "context": {"reason": "human_only"}}
    c, seen = _client(status=403, body=body)
    for call in (lambda: c.analytics.rollups.reattest("sales", "rol_1"), lambda: c.analytics.schedules.reattest("sales", "sch_1")):
        e = _raises(call)
        assert (e.code, e.status, e.context["reason"]) == ("K9O_RBAC_DENY", 403, "human_only")
    assert len(seen) == 2  # a 403 is not retried


def test_rollups_read_projects_and_decodes_suppressed_cells():
    cells = {"cells": [{"window": "2026-10-01T00:00:00Z", "n": 4}, {"status": "suppressed", "reason": "cohort_below_k"}],
             "coverage": {"complete": True}}
    c, seen = _client(data=cells)
    out = c.rollups.read("per_user", dims="city,tier")
    assert _q(seen[0]) == {"dims": "city,tier"} and seen[0].url.path == f"{P}/rollups/per_user/read"
    assert out.data["cells"][1]["reason"] == "cohort_below_k"
    c.rollups.read("per_user")
    assert _q(seen[1]) == {}


# rollups.subscribe --------------------------------------------------------------------------------------------------

HEAD = "retry: 10\n:ok\n\n"


def _cell(sse_id, n=1, suppressed=False):
    payload = ({"cellKey": "k1", "bucket": "2026-10-01T00:00:00Z", "dims": {}, "final": True, "status": "suppressed",
                "reason": "cohort_below_k"} if suppressed else
               {"cellKey": "k1", "bucket": "2026-10-01T00:00:00Z", "dims": {"city": "Oslo"}, "final": True,
                "measures": {"n": n}})
    frame = {"topic": "rollups.per_user", "eventType": "cell", "payload": payload,
             "occurredAt": "2026-10-27T19:31:40.636Z", "eventId": f"evt_{sse_id}"}
    return f"id: {sse_id}\nevent: cell\ndata: {json.dumps(frame)}\n\n"


def _feed(bodies):
    conns: list[httpx.Request] = []

    def handler(req):
        if req.method == "HEAD":
            return httpx.Response(200)
        if req.method == "POST":
            return httpx.Response(200, json={"data": {"events": []}, "meta": {}})
        conns.append(req)
        text = bodies[min(len(conns) - 1, len(bodies) - 1)]
        if callable(text):
            return text(req)
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

    return asyncio.run(asyncio.wait_for(go(), 10))


def test_rollup_subscribe_yields_payload_cells_resumes_and_drops_duplicates():
    c, conns = _feed([HEAD + _cell("cur1") + "event: heartbeat\ndata: {}\n\n" + _cell("cur2", 2),
                      HEAD + _cell("cur2", 2) + _cell("cur3", 3)])
    got = _take(c.rollups.subscribe("per_user", from_sequence=7), 3)
    assert [(e["eventId"], e["measures"]["n"]) for e in got] == [("evt_cur1", 1), ("evt_cur2", 2), ("evt_cur3", 3)]
    assert got[0]["cellKey"] == "k1" and got[0]["final"] is True and got[0]["dims"] == {"city": "Oslo"}
    assert got[0]["suppressed"] is False and got[0]["occurredAt"] == "2026-10-27T19:31:40.636Z"
    assert (conns[0].method, conns[0].url.path) == ("GET", f"{P}/rollups/per_user/subscribe")
    assert _q(conns[0]) == {"fromSequence": "7"}
    assert conns[1].headers["last-event-id"] == "cur2" and _q(conns[1]) == {}  # the SSE id resumes, eventId dedupes


def test_rollup_subscribe_suppressed_cell_is_a_typed_field():
    c, _ = _feed([HEAD + _cell("cur1", suppressed=True)])
    (cell,) = _take(c.rollups.subscribe("per_user"), 1)
    assert cell["suppressed"] is True and (cell["status"], cell["reason"]) == ("suppressed", "cohort_below_k")
    assert "measures" not in cell and cell["eventId"] == "evt_cur1"


def test_rollup_subscribe_resync_calls_the_handler_and_restarts_fresh():
    resync = 'event: resync\ndata: {"reason": "cursor-evicted", "action": "reread-source"}\n\n'
    c, conns = _feed([HEAD + _cell("c1") + resync, HEAD + _cell("c9")])
    infos: list[dict] = []
    got = _take(c.rollups.subscribe("per_user", on_resync=infos.append), 2)
    assert [e["eventId"] for e in got] == ["evt_c1", "evt_c9"]
    assert infos == [{"reason": "cursor-evicted", "action": "reread-source"}]
    assert "last-event-id" not in conns[1].headers


def test_rollup_subscribe_opening_resync_is_informational_and_the_cell_after_it_is_delivered():
    resync = 'event: resync\ndata: {"reason": "cursor-evicted", "head": 4, "action": "reread-source"}\n\n'
    c, conns = _feed([HEAD + resync + _cell("c1")])
    infos: list[dict] = []
    got = _take(c.rollups.subscribe("per_user", on_resync=infos.append), 1)
    assert [e["eventId"] for e in got] == ["evt_c1"] and infos == [] and len(conns) == 1


def test_rollup_subscribe_opening_resync_with_from_sequence_is_a_real_resync():
    resync = 'event: resync\ndata: {"reason": "cursor-evicted", "head": 4, "action": "reread-source"}\n\n'
    c, conns = _feed([HEAD + resync, HEAD + _cell("c9")])
    infos: list[dict] = []
    got = _take(c.rollups.subscribe("per_user", from_sequence=2, on_resync=infos.append), 1)
    assert [e["eventId"] for e in got] == ["evt_c9"]
    assert infos == [{"reason": "cursor-evicted", "head": 4, "action": "reread-source"}]
    assert "fromSequence" in str(conns[0].url) and "fromSequence" not in str(conns[1].url)


# keys ---------------------------------------------------------------------------------------------------------------

KEY = {"id": "vkey_1", "version": 2, "status": "active", "custodyMode": "BYOK", "createdAt": "2026-10-01T00:00:00Z",
       "rotatedAt": "2026-10-20T00:00:00Z", "byokRef": {"provider": "aws", "region": "eu-central-1",
                                                         "externalKeyFingerprint": "AbC123xyz_-9AbCd"}, "hyokRef": None}


def test_keys_list_and_get_expose_the_documented_fields_only():
    c, seen = _client(data={"items": [KEY], "nextCursor": None})
    items = c.keys.list(status="all").data["items"]
    assert _q(seen[0]) == {"status": "all"} and seen[0].url.path == f"{P}/vault/keys"
    assert set(items[0]) == {"id", "version", "status", "custodyMode", "createdAt", "rotatedAt", "byokRef", "hyokRef"}
    assert items[0]["byokRef"]["externalKeyFingerprint"] == "AbC123xyz_-9AbCd" and items[0]["hyokRef"] is None
    c.keys.list()
    assert _q(seen[1]) == {}
    c, seen = _client(data={**KEY, "byokRef": None, "hyokRef": {"clusterId": "hsm_1", "clusterName": "main",
                                                                   "externalKeyFingerprint": "0123456789abcdef"}})
    assert c.keys.get("vkey_1").data["hyokRef"]["clusterName"] == "main"
    assert (seen[0].method, seen[0].url.path) == ("GET", f"{P}/vault/keys/vkey_1")


def test_keys_refuse_bad_input_before_sending():
    c, seen = _client()
    assert _raises(lambda: c.keys.list(status="revoked")).code == "K9O_INVALID_FIELD_VALUE"
    assert _raises(lambda: c.keys.get("")).code == "K9O_INVALID_FIELD_VALUE"
    assert _raises(lambda: c.keys.rotate("")).code == "K9O_INVALID_FIELD_VALUE"
    assert seen == []


def test_rotate_answers_the_new_version():
    c, seen = _client(data={"keyId": "vkey_1", "previousVersion": 2, "newVersion": 3, "status": "rotated"})
    out = c.keys.rotate("vkey_1")
    assert (seen[0].method, seen[0].url.path, seen[0].content) == ("POST", f"{P}/vault/keys/vkey_1/rotate", b"")
    assert out.data == {"keyId": "vkey_1", "previousVersion": 2, "newVersion": 3, "status": "rotated"}


@pytest.mark.parametrize("status,code,reason", [
    (409, "K9O_STATE_CONFLICT", "rotation_in_flight"), (409, "K9O_STATE_CONFLICT", "key_not_active"),
    (429, "K9O_RATE_LIMITED", "rotation_rate_cap"),
])
def test_rotate_refusals_are_typed_and_not_retried(status, code, reason):
    c, seen = _client(status=status, body={"code": code, "message": "no", "context": {"reason": reason}},
                      headers={"retry-after": "3600"} if status == 429 else None)
    e = _raises(lambda: c.keys.rotate("vkey_1"))
    assert (e.code, e.status, e.context["reason"]) == (code, status, reason)
    assert e.retry_after_sec == (3600 if status == 429 else None)
    assert len(seen) == 1


def test_rotate_retry_after_in_the_body_wins_over_the_header():
    c, _ = _client(status=429, body={"code": "K9O_RATE_LIMITED", "message": "cap", "context": {"reason": "rotation_rate_cap"},
                                     "recommendation": {"retryAfterSec": 120}}, headers={"retry-after": "3600"})
    assert _raises(lambda: c.keys.rotate("vkey_1")).retry_after_sec == 120


def test_rotate_without_a_fresh_proof_surfaces_the_step_up_challenge():
    body = {"code": "K9O_STEP_UP_REQUIRED", "message": "step up", "context": {"challenge": "abc", "method": "webauthn"}}
    c, seen = _client(status=401, body=body)
    e = _raises(lambda: c.keys.rotate("vkey_1"))
    assert (e.code, e.status, e.context["challenge"]) == ("K9O_STEP_UP_REQUIRED", 401, "abc")
    assert len(seen) == 1


def test_key_reads_refuse_other_callers_once():
    c, seen = _client(status=403, body={"code": "K9O_TRUST_ZONE_VIOLATION", "message": "no"})
    assert _raises(c.keys.list).code == "K9O_TRUST_ZONE_VIOLATION" and len(seen) == 1


# audit.tail ---------------------------------------------------------------------------------------------------------

def _evt(i, type_="data.write.access.insert"):
    return {"auditEventId": f"evt_{i}", "type": type_, "actorType": "user", "occurredAt": "2026-10-27T10:00:00Z",
            "outcome": None}


def _aframe(i, type_="data.write.access.insert"):
    """The real wire: the SSE id is the publisher's (evt_pub_*), the event name is the type, the five fields sit in payload."""
    frame = {"topic": f"audit.project.{type_}", "eventType": type_, "payload": _evt(i, type_),
             "occurredAt": "2026-10-27T10:00:00Z", "eventId": f"evt_pub_{i}"}
    return f"id: evt_pub_{i}\nevent: {type_}\ndata: {json.dumps(frame)}\n\n"


def _replay_event(i, seq, type_="data.write.access.insert"):
    return {"sequence": str(seq), "topic": f"audit.project.{type_}", "eventType": type_, "payload": _evt(i, type_),
            "occurredAt": "2026-10-27T10:00:00Z"}


RESYNC_LEDGER = 'event: resync\ndata: {"reason": "cursor-evicted", "head": 9, "action": "replay-ledger"}\n\n'
FIVE = {"auditEventId", "type", "actorType", "occurredAt", "outcome"}

# Captured live from the gateway (staging): the raw frame, byte for byte.
LIVE_FRAME = (
    "retry: 1000\n:ok\n\n"
    "id: evt_TqfKnZdBRtggfnwJvuvroU\n"
    "event: data.write.forge.scheduled_trigger.created\n"
    'data: {"topic":"audit.project.data.write.forge.scheduled_trigger.created","eventType":"data.write.forge.scheduled_trigger.created",'
    '"payload":{"auditEventId":"evt_UNs3pW7TSadU","type":"data.write.forge.scheduled_trigger.created","actorType":"user",'
    '"occurredAt":"2026-10-27T19:31:40.636Z","outcome":null},"occurredAt":"2026-10-27T19:31:40.636Z","eventId":"evt_TqfKnZdBRtggfnwJvuvroU"}\n\n'
)
# Captured live: the first replay events.
LIVE_REPLAY = {"data": {"topic": "audit.project.*", "events": [
    {"sequence": "1", "topic": "audit.project.data.write.pulse_stream.created", "eventType": "data.write.pulse_stream.created",
     "payload": {"auditEventId": "evt_UGgF77wit3Xx", "type": "data.write.pulse_stream.created", "actorType": "user",
                 "occurredAt": "2026-10-20T08:00:00.000Z", "outcome": None},
     "occurredAt": "2026-10-20T08:00:00.000Z"}], "nextCursor": None, "hasMore": False}, "meta": {}}


def test_tail_decodes_the_captured_live_frame():
    c, conns = _feed([LIVE_FRAME])
    (ev,) = _take(c.audit.tail(), 1)
    assert ev == {"auditEventId": "evt_UNs3pW7TSadU", "type": "data.write.forge.scheduled_trigger.created",
                  "actorType": "user", "occurredAt": "2026-10-27T19:31:40.636Z", "outcome": None,
                  "eventId": "evt_TqfKnZdBRtggfnwJvuvroU"}


def test_replay_decodes_the_captured_live_answer_into_a_typed_page():
    c, seen = _client(body=LIVE_REPLAY)
    page = c.audit.replay(from_sequence=0, limit=3)
    assert _json(seen[0]) == {"topic": "audit.project.*", "fromSequence": 0, "limit": 3}
    assert page.has_more is False and page.next_cursor is None
    assert page.events == [{"auditEventId": "evt_UGgF77wit3Xx", "type": "data.write.pulse_stream.created",
                            "actorType": "user", "occurredAt": "2026-10-20T08:00:00.000Z", "outcome": None,
                            "sequence": "1"}]


def test_replay_after_id_wins_and_both_are_sent():
    c, seen = _client(body=LIVE_REPLAY)
    c.audit.replay(after_audit_event_id="evt_1", from_sequence=0, until_sequence=9)
    assert _json(seen[0]) == {"topic": "audit.project.*", "afterAuditEventId": "evt_1", "fromSequence": 0, "untilSequence": 9, "limit": 1000}


def test_tail_opens_the_wildcard_topic_and_yields_exactly_the_five_fields():
    c, conns = _feed([HEAD + _aframe("a1")])
    (ev,) = _take(c.audit.tail(), 1)
    assert (conns[0].method, conns[0].url.path) == ("GET", f"{P}/realtime/subscribe")
    assert _q(conns[0]) == {"topic": "audit.project.*"}
    assert set(ev) == FIVE | {"eventId"} and ev["auditEventId"] == "evt_a1" and ev["eventId"] == "evt_pub_a1"
    assert ev["outcome"] is None


def test_tail_topic_per_type_and_filter_for_several():
    c, conns = _feed([HEAD + _aframe("a1", "auth.sign_in")])
    _take(c.audit.tail(["auth.sign_in"]), 1)
    assert _q(conns[0]) == {"topic": "audit.project.auth.sign_in"}
    c, conns = _feed([HEAD + _aframe("a1", "x.y") + _aframe("a2", "auth.sign_in") + _aframe("a3", "z.z")])
    got = _take(c.audit.tail(types=["auth.sign_in", "z.z"]), 2)
    assert _q(conns[0]) == {"topic": "audit.project.*"}
    assert [e["type"] for e in got] == ["auth.sign_in", "z.z"]


def test_tail_refuses_bad_types_before_connecting():
    c, conns = _feed([HEAD])
    for bad in ("auth.sign_in", ["a b"], [""], ["audit.*"], [3]):
        assert _raises(lambda: c.audit.tail(bad)).code == "K9O_INVALID_FIELD_VALUE"
    assert conns == []


def test_tail_resumes_with_last_event_id_and_drops_duplicates():
    c, conns = _feed([HEAD + _aframe("a1") + _aframe("a2"), HEAD + _aframe("a2") + _aframe("a3")])
    got = _take(c.audit.tail(), 3)
    assert [e["auditEventId"] for e in got] == ["evt_a1", "evt_a2", "evt_a3"]
    assert conns[1].headers["last-event-id"] == "evt_pub_a2"  # the SSE id, not the auditEventId


def test_tail_resync_replays_the_ledger_from_the_last_event_then_continues():
    replays: list[httpx.Request] = []
    conns: list[httpx.Request] = []
    bodies = [HEAD + _aframe("a1") + _aframe("a2") + RESYNC_LEDGER, HEAD + _aframe("a4") + _aframe("a5")]

    def handler(req):
        if req.method == "HEAD":
            return httpx.Response(200)
        if req.method == "POST":
            replays.append(req)
            pages = [{"events": [_replay_event("a3", 3), _replay_event("a4", 4)], "nextCursor": "c", "hasMore": True},
                     {"events": [_replay_event("a4b", 5)], "nextCursor": None, "hasMore": False}]
            return httpx.Response(200, json={"data": pages[len(replays) - 1], "meta": {}})
        conns.append(req)
        return httpx.Response(200, content=bodies[min(len(conns) - 1, 1)].encode(), headers={"content-type": "text/event-stream"})

    c = create_client(base_url=BASE, project_slug="portal", api_key="k")
    c.config.transport = httpx.MockTransport(handler)
    infos: list[dict] = []
    got = _take(c.audit.tail(on_resync=infos.append), 6)
    # a4 arrives in the caught-up page and again live: delivered once
    assert [e["auditEventId"] for e in got] == ["evt_a1", "evt_a2", "evt_a3", "evt_a4", "evt_a4b", "evt_a5"]
    assert infos[0]["action"] == "replay-ledger"
    assert (replays[0].url.path, _json(replays[0])) == (f"{P}/realtime/replay", {"topic": "audit.project.*", "afterAuditEventId": "evt_a2", "limit": 1000})
    assert _json(replays[1])["afterAuditEventId"] == "evt_a4"  # hasMore: the next page starts after the page's last event
    assert len(replays) == 2
    assert got[2]["sequence"] == "3" and set(got[2]) == FIVE | {"sequence"}


def test_tail_resync_with_nothing_delivered_has_nothing_to_replay():
    posts = []

    def handler(req):
        if req.method == "HEAD":
            return httpx.Response(200)
        if req.method == "POST":
            posts.append(req)
        text = HEAD + RESYNC_LEDGER if not posts and len(handler.n) == 0 else HEAD + _aframe("a1")
        handler.n.append(1)
        return httpx.Response(200, content=text.encode(), headers={"content-type": "text/event-stream"})

    handler.n = []
    c = create_client(base_url=BASE, project_slug="portal", api_key="k")
    c.config.transport = httpx.MockTransport(handler)
    (ev,) = _take(c.audit.tail(), 1)
    assert ev["auditEventId"] == "evt_a1" and posts == []


def test_tail_idle_connection_is_abandoned_and_reconnected():
    class Hang(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b":ok\n\n"
            await asyncio.sleep(30)

    c, conns = _feed([lambda r: httpx.Response(200, stream=Hang(), headers={"content-type": "text/event-stream"}),
                      HEAD + _aframe("a1")])
    (ev,) = _take(c.audit.tail(idle_timeout=0.1), 1)
    assert ev["auditEventId"] == "evt_a1" and len(conns) == 2


def test_tail_heartbeat_comments_keep_the_stream_quiet():
    c, _ = _feed([HEAD + ":hb\n\n:hb\n\n" + _aframe("a1")])
    assert [e["auditEventId"] for e in _take(c.audit.tail(), 1)] == ["evt_a1"]


def test_tail_denied_raises_once():
    def handler(req):
        if req.method == "HEAD":
            return httpx.Response(200)
        return httpx.Response(403, json={"code": "K9O_RBAC_DENY", "message": "no"})

    c = create_client(base_url=BASE, project_slug="portal", api_key="k")
    c.config.transport = httpx.MockTransport(handler)
    with pytest.raises(KError) as info:
        _take(c.audit.tail(), 1)
    assert info.value.code == "K9O_RBAC_DENY"


def test_tail_with_since_minutes_is_the_earlier_one_shot_read():
    c, seen = _client(data=[])
    c.audit.tail(since_minutes=5)
    assert seen[0].url.path == f"{P}/audit-log" and set(_q(seen[0])) == {"after", "limit"}


def test_replay_by_sequence_and_validation():
    c, seen = _client(data={"events": [_replay_event("a1", 5)], "nextCursor": "x", "hasMore": True})
    out = c.audit.replay(topic="audit.project.auth.sign_in", from_sequence=5, limit=100)
    assert _json(seen[0]) == {"topic": "audit.project.auth.sign_in", "fromSequence": 5, "limit": 100}
    assert out.events[0]["sequence"] == "5" and out.has_more is True and out.next_cursor == "x"
    for call in (lambda: c.audit.replay(after_audit_event_id="a1"),
                 lambda: c.audit.replay(limit=0), lambda: c.audit.replay(from_sequence=-1)):
        assert _raises(call).code == "K9O_INVALID_FIELD_VALUE"
    assert len(seen) == 1


# Rampart: input too long --------------------------------------------------------------------------------------------

def test_classify_413_is_a_typed_error_and_never_a_verdict():
    body = {"code": "K9O_RAMPART_INPUT_TOO_LONG", "message": "too long", "context": {"reason": "rampart_input_too_long"},
            "requestId": "req_1"}
    c, seen = _client(status=413, body=body)
    with pytest.raises(RampartInputTooLong) as info:
        c.rampart.classify({"text": "x"})
    e = info.value
    assert isinstance(e, KError) and (e.code, e.status, e.request_id) == ("K9O_RAMPART_INPUT_TOO_LONG", 413, "req_1")
    assert e.context["reason"] == "rampart_input_too_long" and len(seen) == 1


def test_classify_503_stays_a_plain_kerror():
    c, _ = _client(status=503, body={"code": "K9O_RAMPART_CLASSIFIER_UNAVAILABLE", "message": "down"})
    e = _raises(lambda: c.rampart.classify({"text": "x"}))
    assert not isinstance(e, RampartInputTooLong) and e.code == "K9O_RAMPART_CLASSIFIER_UNAVAILABLE"


# audit.tail: a gapless resync ---------------------------------------------------------------------------------------

def _scripted(streams, replay_pages, on_replay=None):
    """Scripted SSE: `streams` are served per connection (text, or a callable(req)); POSTs answer `replay_pages` in order."""
    conns: list[httpx.Request] = []
    posts: list[httpx.Request] = []

    def handler(req):
        if req.method == "HEAD":
            return httpx.Response(200)
        if req.method == "POST":
            posts.append(req)
            if on_replay:
                on_replay()
            page = replay_pages[min(len(posts) - 1, len(replay_pages) - 1)]
            return httpx.Response(200, json={"data": page, "meta": {}})
        conns.append(req)
        s = streams[min(len(conns) - 1, len(streams) - 1)]
        if callable(s):
            return s(req)
        return httpx.Response(200, content=s.encode(), headers={"content-type": "text/event-stream"})

    c = create_client(base_url=BASE, project_slug="portal", api_key="k")
    c.config.transport = httpx.MockTransport(handler)
    return c, conns, posts


def _ids(events):
    return [e["auditEventId"] for e in events]


def test_resync_event_published_after_replay_end_is_in_the_fresh_backlog_exactly_once():
    # E (a4) was published after the replay's last event (a3) but before the fresh stream's first frame: the fresh
    # stream was opened first, so its backlog carries a3 and a4.
    c, conns, posts = _scripted(
        [HEAD + _aframe("a1") + _aframe("a2") + RESYNC_LEDGER, HEAD + _aframe("a3") + _aframe("a4") + _aframe("a5")],
        [{"events": [_replay_event("a3", 3)], "nextCursor": None, "hasMore": False}])
    got = _take(c.audit.tail(), 5)
    assert _ids(got) == ["evt_a1", "evt_a2", "evt_a3", "evt_a4", "evt_a5"]
    assert "last-event-id" not in conns[1].headers  # the reconnect after a resync is fresh
    assert _json(posts[0])["afterAuditEventId"] == "evt_a2" and len(posts) == 1


def test_resync_frames_that_arrive_during_the_replay_follow_the_replay_events_deduped():
    order: list[str] = []

    class Slow(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield (HEAD + _aframe("b1") + _aframe("r2")).encode()  # r2 is also in the replay page
            order.append("frames-sent")
            await asyncio.sleep(0.3)
            yield _aframe("b3").encode()
            await asyncio.sleep(30)

    c, conns, posts = _scripted(
        [HEAD + _aframe("a1") + RESYNC_LEDGER,
         lambda r: httpx.Response(200, stream=Slow(), headers={"content-type": "text/event-stream"})],
        [{"events": [_replay_event("r1", 2), _replay_event("r2", 3)], "nextCursor": None, "hasMore": False}],
        on_replay=lambda: order.append("replay"))
    got = _take(c.audit.tail(), 5)
    assert _ids(got) == ["evt_a1", "evt_r1", "evt_r2", "evt_b1", "evt_b3"]
    assert got[1]["sequence"] == "2"


def test_resync_with_nothing_delivered_skips_the_replay_and_continues_fresh():
    c, conns, posts = _scripted([HEAD + RESYNC_LEDGER, HEAD + _aframe("a1") + _aframe("a1") + _aframe("a2")], [{"events": []}])
    got = _take(c.audit.tail(), 2)
    assert _ids(got) == ["evt_a1", "evt_a2"] and posts == [] and len(conns) == 2
    assert "last-event-id" not in conns[1].headers


def test_double_resync_backs_off_and_does_not_spin(monkeypatch):
    sleeps: list[float] = []
    real = asyncio.sleep

    async def fake(delay, *a):
        sleeps.append(delay)
        await real(0)

    monkeypatch.setattr("k9o.realtime.asyncio.sleep", fake)
    c, conns, posts = _scripted([HEAD + _aframe("a1") + RESYNC_LEDGER, HEAD + RESYNC_LEDGER, HEAD + RESYNC_LEDGER, HEAD + _aframe("a2")], [{"events": []}])
    got = _take(c.audit.tail(), 2)
    assert _ids(got) == ["evt_a1", "evt_a2"] and len(conns) == 4
    assert sleeps[0] == 0.01 and sleeps[1] > sleeps[0] and sleeps[2] > sleeps[1]


def test_resync_pages_while_has_more_and_a_page_without_progress_ends_it():
    c, conns, posts = _scripted(
        [HEAD + _aframe("a1") + RESYNC_LEDGER, HEAD],
        [{"events": [_replay_event("r1", 2)], "nextCursor": "c", "hasMore": True},
         {"events": [_replay_event("r1", 2)], "nextCursor": "c", "hasMore": True}])
    got = _take(c.audit.tail(), 2)
    assert _ids(got) == ["evt_a1", "evt_r1"] and [_json(p)["afterAuditEventId"] for p in posts] == ["evt_a1", "evt_r1"]


def test_replay_default_limit_is_1000_and_out_of_range_is_refused_client_side():
    c, seen = _client(data={"events": [], "nextCursor": None, "hasMore": False})
    c.audit.replay()
    c.audit.replay(limit=10000)
    assert [_json(r)["limit"] for r in seen] == [1000, 10000]
    for bad in (10001, 0, -1):
        assert _raises(lambda: c.audit.replay(limit=bad)).code == "K9O_INVALID_FIELD_VALUE"
    assert len(seen) == 2


LIVE_HEAD_STREAM = 'retry: 1000\n:ok\n\nevent: resync\ndata: {"reason":"cursor-evicted","head":75,"action":"replay-ledger"}\n\n' + LIVE_FRAME.split(":ok\n\n",1)[1]


def test_resync_notice_at_the_head_of_a_fresh_connection_keeps_the_stream():
    # Staging answers a fresh connection with a resync notice and then sends the live frames on the same connection.
    infos: list[dict] = []
    c, conns, posts = _scripted([LIVE_HEAD_STREAM], [{"events": []}])
    got = _take(c.audit.tail(on_resync=infos.append), 1)
    assert _ids(got) == ["evt_UNs3pW7TSadU"] and len(conns) == 1 and posts == [] and infos == []
