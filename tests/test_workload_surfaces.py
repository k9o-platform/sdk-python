"""require_approval on writes, client.functions.triggers, client.schedules, client.end_users: wire shape,
204 handling, the queued-write discriminator and client-side validation."""

from __future__ import annotations

import json
from urllib.parse import parse_qsl

import httpx
import pytest

from k9o import KError, create_client, is_queued_write

BASE = "https://acme.api.k9o.io"
META = {"cost": {}, "timing": {}, "context": {"requestId": "req_1"}}

# Live-confirmed bodies from the staging conformance project.
QUEUED_INSERT = {
    "data": {
        "id": "apr_5oLshD2MVT27",
        "status": "pending_approval",
        "collection": "conformance_kv",
        "operation": "insert",
        "makerId": "usr_conformance0001",
        "proposedPayload": {"id": "contract2-probe-0001", "val": "contract2-probe"},
        "targetRecordId": None,
    },
    "meta": {**META, "approval": {"status": "pending_approval"}},
}
APPLIED_ROW = {"data": {"id": "r1", "val": "x"}, "meta": META}
TRIGGER = {
    "id": "0b8f6a52-3f0e-4c8e-9a47-5d2b0f1c7e11", "functionName": "w4c-probe", "collection": "orders",
    "events": ["insert", "update"], "filter": None, "enabled": True, "logOkInvocations": False,
    "createdAt": "2026-10-26T17:59:56.815Z",
}
SCHEDULE_ID = "0b8f6a52-3f0e-4c8e-9a47-5d2b0f1c7e11"


def _client(status=200, body=None):
    seen: list[httpx.Request] = []

    def handler(req):
        seen.append(req)
        if status == 204:
            return httpx.Response(204)
        return httpx.Response(status, json=body if body is not None else {"data": {}, "meta": META})

    c = create_client(base_url=BASE, project_slug="portal", api_key="k")
    c.config.transport = httpx.MockTransport(handler)
    return c, seen


def _only(seen):
    reqs = [r for r in seen if r.method != "HEAD"]
    assert len(reqs) == 1
    return reqs[0]


def _body(req):
    return json.loads(req.content) if req.content else None


def _query(req):
    return dict(parse_qsl(req.url.query.decode()))


# ---- require_approval -------------------------------------------------------------------------------------

def test_insert_without_flag_sends_no_query():
    c, seen = _client(201, APPLIED_ROW)
    r = c.from_("conformance_kv").insert({"id": "a"})
    req = _only(seen)
    assert (req.method, req.url.path, req.url.query) == ("POST", "/@portal/data/conformance_kv", b"")
    assert _body(req) == {"id": "a"}
    assert is_queued_write(r) is False


def test_insert_require_approval_queued_202():
    c, seen = _client(202, QUEUED_INSERT)
    r = c.from_("conformance_kv").insert({"id": "w2-probe-python-1", "val": "v"}, require_approval=True)
    req = _only(seen)
    assert (req.method, req.url.path) == ("POST", "/@portal/data/conformance_kv")
    assert _query(req) == {"requireApproval": "true"}
    assert _body(req) == {"id": "w2-probe-python-1", "val": "v"}
    assert is_queued_write(r) is True
    assert r.data["id"].startswith("apr_") and r.data["targetRecordId"] is None


def test_insert_require_approval_auto_approved_is_not_queued():
    c, seen = _client(201, APPLIED_ROW)
    r = c.from_("conformance_kv").insert({"id": "a"}, require_approval=True)
    assert _query(_only(seen)) == {"requireApproval": "true"}
    assert is_queued_write(r) is False


def test_update_and_delete_require_approval():
    c, seen = _client(202, {**QUEUED_INSERT})
    r = c.from_("kv").update({"val": "n"}).eq("id", "r 1").require_approval().execute()
    req = _only(seen)
    assert (req.method, req.url.path, _query(req), _body(req)) == ("PATCH", "/@portal/data/kv/r 1",
                                                                   {"requireApproval": "true"}, {"val": "n"})
    assert is_queued_write(r)

    c, seen = _client(202, QUEUED_INSERT)
    c.from_("kv").delete().eq("id", "r1").require_approval().execute()
    req = _only(seen)
    assert (req.method, req.url.path, _query(req)) == ("DELETE", "/@portal/data/kv/r1", {"requireApproval": "true"})


def test_update_and_delete_default_send_no_query():
    c, seen = _client()
    c.from_("kv").update({"a": 1}).eq("id", "r1").execute()
    assert _only(seen).url.query == b""
    c, seen = _client()
    c.from_("kv").delete().eq("id", "r1").execute()
    assert _only(seen).url.query == b""


@pytest.mark.parametrize("result", [
    {"data": {"status": "pending_approval"}, "meta": META},                        # no meta.approval
    {"data": None, "meta": {**META, "approval": {"status": "pending_approval"}}},  # no data
    {"data": {"status": "applied"}, "meta": {**META, "approval": {"status": "pending_approval"}}},
])
def test_is_queued_write_needs_both_markers(result):
    from k9o.envelope import Envelope, meta_from_wire
    assert is_queued_write(Envelope(data=result["data"], meta=meta_from_wire(result["meta"]))) is False


# ---- triggers ---------------------------------------------------------------------------------------------

def test_triggers_list_empty():
    c, seen = _client(200, {"data": [], "meta": META})
    r = c.functions.triggers.list("w4c-probe")
    req = _only(seen)
    assert (req.method, req.url.path, req.url.query) == ("GET", "/@portal/functions/w4c-probe/triggers", b"")
    assert r.data == []


def test_triggers_create_body_and_201():
    c, seen = _client(201, {"data": TRIGGER, "meta": META})
    r = c.functions.triggers.create("w4c-probe", collection="orders", events=["insert", "update"],
                                    filter={"eq": ["resource.status", "paid"]}, enabled=False,
                                    log_ok_invocations=True)
    req = _only(seen)
    assert (req.method, req.url.path) == ("POST", "/@portal/functions/w4c-probe/triggers")
    assert _body(req) == {"collection": "orders", "events": ["insert", "update"],
                          "filter": {"eq": ["resource.status", "paid"]}, "enabled": False, "logOkInvocations": True}
    assert r.data["functionName"] == "w4c-probe"


def test_triggers_create_minimal_body():
    c, seen = _client(201, {"data": TRIGGER, "meta": META})
    c.functions.triggers.create("f", collection="orders", events=["upsert"])
    assert _body(_only(seen)) == {"collection": "orders", "events": ["upsert"]}


def test_triggers_update_clear_filter_and_omit():
    c, seen = _client(200, {"data": TRIGGER, "meta": META})
    c.functions.triggers.update("f", "tid", filter=None, enabled=True)
    req = _only(seen)
    assert (req.method, req.url.path, _body(req)) == ("PATCH", "/@portal/functions/f/triggers/tid",
                                                      {"filter": None, "enabled": True})
    c, seen = _client(200, {"data": TRIGGER, "meta": META})
    c.functions.triggers.update("f", "tid", events=["delete"], log_ok_invocations=False)
    assert _body(_only(seen)) == {"events": ["delete"], "logOkInvocations": False}


def test_triggers_delete_204_has_no_body():
    c, seen = _client(204)
    r = c.functions.triggers.delete("f", "tid")
    req = _only(seen)
    assert (req.method, req.url.path) == ("DELETE", "/@portal/functions/f/triggers/tid")
    assert r.data is None


@pytest.mark.parametrize("call", [
    lambda t: t.create("f", collection="orders", events=[]),
    lambda t: t.create("f", collection="orders", events=["insert"] * 5),
    lambda t: t.create("f", collection="orders", events=["truncate"]),
    lambda t: t.create("f", collection="1bad", events=["insert"]),
    lambda t: t.create("", collection="orders", events=["insert"]),
    lambda t: t.update("f", "tid"),
    lambda t: t.update("f", "tid", events=["nope"]),
    lambda t: t.update("f", ""),
    lambda t: t.delete("f", ""),
])
def test_triggers_validation_sends_nothing(call):
    c, seen = _client()
    with pytest.raises(KError) as e:
        call(c.functions.triggers)
    assert e.value.code == "K9O_INVALID_FIELD_VALUE"
    assert seen == []


# ---- schedules --------------------------------------------------------------------------------------------

SCHEDULE = {
    "id": SCHEDULE_ID, "functionName": "nightly", "scheduleExpression": "0 0 * * *", "scheduleTimezone": "UTC",
    "catchupPolicy": "skip", "collection": None, "enabled": True, "status": "active", "logOkInvocations": False,
    "createdAt": "2026-10-26T17:59:56.815Z", "lastInvocationAt": None, "nextInvocationAt": None,
}


def test_schedules_list_empty_live_fixture():
    c, seen = _client(200, {"data": [], "meta": {**META, "cursor": {"next": None}}})
    r = c.schedules.list(limit=2)
    req = _only(seen)
    assert (req.method, req.url.path, _query(req)) == ("GET", "/@portal/forge/schedules", {"limit": "2"})
    assert r.data == [] and r.meta.cursor.next is None


def test_schedules_list_query_names():
    c, seen = _client(200, {"data": [], "meta": META})
    c.schedules.list(status="paused", function_name="nightly", limit=5, cursor="abc")
    assert _query(_only(seen)) == {"status": "paused", "function_name": "nightly", "limit": "5", "cursor": "abc"}


def test_schedules_list_no_args_has_no_query():
    c, seen = _client(200, {"data": [], "meta": META})
    c.schedules.list()
    assert _only(seen).url.query == b""


def test_schedules_get_update_delete_paths():
    c, seen = _client(200, {"data": SCHEDULE, "meta": META})
    c.schedules.get(SCHEDULE_ID)
    req = _only(seen)
    assert (req.method, req.url.path) == ("GET", f"/@portal/forge/schedules/{SCHEDULE_ID}")

    c, seen = _client(200, {"data": SCHEDULE, "meta": META})
    c.schedules.update(SCHEDULE_ID, schedule_expression="@daily", status="paused", log_ok_invocations=True)
    req = _only(seen)
    assert (req.method, req.url.path) == ("PATCH", f"/@portal/forge/schedules/{SCHEDULE_ID}")
    assert _body(req) == {"scheduleExpression": "@daily", "status": "paused", "logOkInvocations": True}

    c, seen = _client(204)
    r = c.schedules.delete(SCHEDULE_ID)
    req = _only(seen)
    assert (req.method, req.url.path) == ("DELETE", f"/@portal/forge/schedules/{SCHEDULE_ID}")
    assert r.data is None


def test_schedules_create_body():
    c, seen = _client(201, {"data": SCHEDULE, "meta": META})
    c.schedules.create(function_name="nightly", schedule_expression="0 0 * * *", schedule_timezone="Europe/Berlin",
                       catchup_policy="run_once", collection="orders", enabled=False, log_ok_invocations=True)
    req = _only(seen)
    assert (req.method, req.url.path) == ("POST", "/@portal/forge/schedules")
    assert _body(req) == {"functionName": "nightly", "scheduleExpression": "0 0 * * *",
                          "scheduleTimezone": "Europe/Berlin", "catchupPolicy": "run_once", "collection": "orders",
                          "enabled": False, "logOkInvocations": True}


def test_schedules_create_minimal_body():
    c, seen = _client(201, {"data": SCHEDULE, "meta": META})
    c.schedules.create(function_name="nightly", schedule_expression="@hourly")
    assert _body(_only(seen)) == {"functionName": "nightly", "scheduleExpression": "@hourly"}


def test_schedules_runs_and_run_now():
    c, seen = _client(200, {"data": [], "meta": META})
    c.schedules.runs(SCHEDULE_ID, limit=10, cursor="c")
    req = _only(seen)
    assert (req.method, req.url.path, _query(req)) == ("GET", f"/@portal/forge/schedules/{SCHEDULE_ID}/runs",
                                                       {"limit": "10", "cursor": "c"})
    c, seen = _client(202, {"data": {"accepted": True}, "meta": META})
    r = c.schedules.run_now(SCHEDULE_ID)
    req = _only(seen)
    assert (req.method, req.url.path, req.content) == ("POST", f"/@portal/forge/schedules/{SCHEDULE_ID}/run-now", b"")
    assert r.data == {"accepted": True}


@pytest.mark.parametrize("call", [
    lambda s: s.list(limit=0),
    lambda s: s.list(limit=201),
    lambda s: s.list(status="stopped"),
    lambda s: s.get(""),
    lambda s: s.update("id"),
    lambda s: s.update("id", status="bogus"),
    lambda s: s.create(function_name="f", schedule_expression="@daily", catchup_policy="always"),
    lambda s: s.runs("id", limit=0),
    lambda s: s.delete(""),
])
def test_schedules_validation_sends_nothing(call):
    c, seen = _client()
    with pytest.raises(KError) as e:
        call(c.schedules)
    assert e.value.code == "K9O_INVALID_FIELD_VALUE"
    assert seen == []


# ---- end users --------------------------------------------------------------------------------------------

END_USER = {"id": "eu_YmGE3Z6nkv5i", "email": "user@example.com", "emailVerifiedAt": None, "status": "active",
            "createdAt": "2026-10-09T12:15:13.430Z", "lastSigninAt": None, "signInMethods": ["password"],
            "region": None}
GRANT = {"id": "eurg_AbCdEf123456", "endUserId": "eu_YmGE3Z6nkv5i", "role": "billing-manager",
         "grantedAt": "2026-10-26T17:59:56.815Z", "grantedBy": "usr_conformance0001"}


def test_end_users_list_and_get():
    c, seen = _client(200, {"data": [END_USER], "meta": {**META, "cursor": {"next": "n"}}})
    r = c.end_users.list(status="all", q="user@", limit=2, cursor="c")
    req = _only(seen)
    assert (req.method, req.url.path, _query(req)) == ("GET", "/@portal/end-users",
                                                       {"status": "all", "q": "user@", "limit": "2", "cursor": "c"})
    assert r.data[0]["id"] == "eu_YmGE3Z6nkv5i" and r.meta.cursor.next == "n"

    c, seen = _client(200, {"data": END_USER, "meta": META})
    c.end_users.get("eu_YmGE3Z6nkv5i")
    req = _only(seen)
    assert (req.method, req.url.path, req.url.query) == ("GET", "/@portal/end-users/eu_YmGE3Z6nkv5i", b"")


def test_role_grants_list_unwraps_grants_live_fixture():
    c, seen = _client(200, {"data": {"grants": []}, "meta": META})
    r = c.end_users.role_grants.list()
    req = _only(seen)
    assert (req.method, req.url.path, req.url.query) == ("GET", "/@portal/end-user-role-grants", b"")
    assert r.data == []

    c, seen = _client(200, {"data": {"grants": [GRANT]}, "meta": META})
    r = c.end_users.role_grants.list(end_user_id="eu_x")
    assert _query(_only(seen)) == {"endUserId": "eu_x"}
    assert r.data == [GRANT]


def test_role_grants_grant_unwraps_grant():
    c, seen = _client(200, {"data": {"grant": GRANT}, "meta": META})
    r = c.end_users.role_grants.grant("eu_YmGE3Z6nkv5i", "billing-manager")
    req = _only(seen)
    assert (req.method, req.url.path) == ("POST", "/@portal/end-user-role-grants")
    assert _body(req) == {"endUserId": "eu_YmGE3Z6nkv5i", "role": "billing-manager"}
    assert r.data == GRANT


def test_role_grants_revoke_and_unknown_is_false_not_error():
    c, seen = _client(200, {"data": {"revoked": True, "revokedAt": "2026-10-26T18:00:00.000Z"}, "meta": META})
    r = c.end_users.role_grants.revoke("eurg_AbCdEf123456")
    req = _only(seen)
    assert (req.method, req.url.path) == ("DELETE", "/@portal/end-user-role-grants/eurg_AbCdEf123456")
    assert r.data["revoked"] is True

    c, _ = _client(200, {"data": {"revoked": False, "revokedAt": None}, "meta": META})
    r = c.end_users.role_grants.revoke("eurg_nope")
    assert r.data == {"revoked": False, "revokedAt": None}


@pytest.mark.parametrize("call", [
    lambda e: e.list(status="gone"),
    lambda e: e.list(limit=0),
    lambda e: e.get(""),
    lambda e: e.get("x" * 65),
    lambda e: e.role_grants.grant("", "r"),
    lambda e: e.role_grants.grant("eu_1", ""),
    lambda e: e.role_grants.grant("eu_1", "r" * 129),
    lambda e: e.role_grants.revoke(""),
])
def test_end_users_validation_sends_nothing(call):
    c, seen = _client()
    with pytest.raises(KError) as e:
        call(c.end_users)
    assert e.value.code == "K9O_INVALID_FIELD_VALUE"
    assert seen == []


def test_project_slug_required():
    c = create_client(base_url=BASE, api_key="k")
    with pytest.raises(ValueError):
        c.schedules.list()
    with pytest.raises(ValueError):
        c.end_users.list()
    with pytest.raises(ValueError):
        c.functions.triggers.list("f")
