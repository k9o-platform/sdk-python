"""client.approvals, client.approval_policies, client.schema.collections: wire shape and client-side validation."""

from __future__ import annotations

import json
from urllib.parse import parse_qsl

import httpx
import pytest

from k9o import KError, create_client

BASE = "https://acme.api.k9o.io"
CRITERIA = {"eq": ["change.table", "orders"]}
PREDICATE = {"gt": ["change.row.amount", 1000]}


def _client(handler=None, **kw):
    seen: list[httpx.Request] = []

    def default(req):
        seen.append(req)
        return httpx.Response(200, json={"data": {}, "meta": {}})

    def recording(req):
        seen.append(req)
        return handler(req)

    c = create_client(base_url=BASE, project_slug=kw.pop("project_slug", "portal"), api_key="k", **kw)
    c.config.transport = httpx.MockTransport(recording if handler else default)
    return c, seen


def _sent(call):
    c, seen = _client()
    call(c)
    reqs = [r for r in seen if r.method != "HEAD"]
    assert len(reqs) == 1
    return reqs[0]


def _body(req):
    return json.loads(req.content) if req.content else None


def _query(req):
    return dict(parse_qsl(req.url.query.decode()))


CASES = [
    (lambda c: c.approvals.list(), "GET", "/@portal/pending-approvals", {}, None),
    (lambda c: c.approvals.list(status="pending_approval", limit=20), "GET", "/@portal/pending-approvals",
     {"status": "pending_approval", "limit": "20"}, None),
    (lambda c: c.approvals.list(limit=1, cursor="eyJ2IjoxfQ"), "GET", "/@portal/pending-approvals",
     {"limit": "1", "cursor": "eyJ2IjoxfQ"}, None),
    (lambda c: c.approvals.get("apr_1"), "GET", "/@portal/pending-approvals/apr_1", {}, None),
    (lambda c: c.approvals.approve("apr_1"), "POST", "/@portal/pending-approvals/apr_1/approve", {}, {}),
    (lambda c: c.approvals.approve("apr_1", reason="ok"), "POST", "/@portal/pending-approvals/apr_1/approve", {},
     {"reason": "ok"}),
    (lambda c: c.approvals.reject("apr_1", "duplicate"), "POST", "/@portal/pending-approvals/apr_1/reject", {},
     {"reason": "duplicate"}),
    (lambda c: c.approvals.bulk("approve", CRITERIA), "POST", "/@portal/pending-approvals/bulk", {},
     {"action": "approve", "criteria": CRITERIA}),
    (lambda c: c.approvals.bulk("approve", CRITERIA, reason="fine"), "POST", "/@portal/pending-approvals/bulk", {},
     {"action": "approve", "criteria": CRITERIA, "reason": "fine"}),
    (lambda c: c.approvals.bulk("reject", CRITERIA, reason="stale"), "POST", "/@portal/pending-approvals/bulk", {},
     {"action": "reject", "criteria": CRITERIA, "reason": "stale"}),
    (lambda c: c.approval_policies.list(), "GET", "/@portal/approval-policies", {}, None),
    (lambda c: c.approval_policies.create("orders", "update", "queue", PREDICATE), "POST",
     "/@portal/approval-policies", {},
     {"collection": "orders", "operation": "update", "action": "queue", "predicate": PREDICATE}),
    (lambda c: c.approval_policies.delete("apo_1"), "DELETE", "/@portal/approval-policies/apo_1", {}, None),
    (lambda c: c.schema.collections.list(), "GET", "/@portal/schema/collections", {}, None),
    (lambda c: c.schema.collections.list(limit=2, cursor="dGFza3M"), "GET", "/@portal/schema/collections",
     {"limit": "2", "cursor": "dGFza3M"}, None),
]


@pytest.mark.parametrize("call,method,path,query,body", CASES)
def test_wire_shape(call, method, path, query, body):
    req = _sent(call)
    assert req.method == method
    assert req.url.path == path
    assert _query(req) == query
    assert _body(req) == body


def test_ids_are_path_encoded():
    assert _sent(lambda c: c.approvals.get("a/b")).url.raw_path.decode() == "/@portal/pending-approvals/a%2Fb"


def test_response_envelope_is_returned():
    def handler(req):
        return httpx.Response(200, json={"data": {"id": "apr_1", "status": "pending_approval"},
                                         "meta": {"cost": {}}})

    c, _ = _client(handler)
    res = c.approvals.approve("apr_1")
    assert res.data["status"] == "pending_approval"


def test_schema_page_exposes_cursor():
    def handler(req):
        return httpx.Response(200, json={"data": [{"name": "orders", "rowCount": 1}],
                                         "meta": {"cursor": {"next": "b3JkZXJz", "totalCount": None}}})

    c, _ = _client(handler)
    res = c.schema.collections.list(limit=1)
    assert res.data[0]["name"] == "orders"
    assert res.meta.cursor.next == "b3JkZXJz"


def _invalid(call):
    c, seen = _client()
    with pytest.raises(KError) as exc:
        call(c)
    assert exc.value.code == "K9O_INVALID_FIELD_VALUE"
    assert [r for r in seen if r.method != "HEAD"] == []


BAD_LIMITS = [0, 201, -1, 1.5, "10", True]


@pytest.mark.parametrize("limit", BAD_LIMITS)
def test_approvals_list_rejects_bad_limit(limit):
    _invalid(lambda c: c.approvals.list(limit=limit))


@pytest.mark.parametrize("limit", BAD_LIMITS)
def test_schema_list_rejects_bad_limit(limit):
    _invalid(lambda c: c.schema.collections.list(limit=limit))


@pytest.mark.parametrize("limit", [1, 200])
def test_limit_bounds_accepted(limit):
    assert _query(_sent(lambda c: c.approvals.list(limit=limit)))["limit"] == str(limit)


@pytest.mark.parametrize("reason", [None, "", "   ", 5])
def test_reject_needs_reason(reason):
    _invalid(lambda c: c.approvals.reject("apr_1", reason))


@pytest.mark.parametrize("bad", ["", None, 5])
def test_ids_must_be_non_empty_strings(bad):
    _invalid(lambda c: c.approvals.get(bad))
    _invalid(lambda c: c.approvals.approve(bad))
    _invalid(lambda c: c.approvals.reject(bad, "x"))
    _invalid(lambda c: c.approval_policies.delete(bad))


@pytest.mark.parametrize("action", ["", "delete", None, "APPROVE"])
def test_bulk_rejects_bad_action(action):
    _invalid(lambda c: c.approvals.bulk(action, CRITERIA, reason="x"))


@pytest.mark.parametrize("criteria", [None, [], [CRITERIA], "eq", 5])
def test_bulk_criteria_must_be_an_object(criteria):
    _invalid(lambda c: c.approvals.bulk("approve", criteria))


@pytest.mark.parametrize("reason", [None, "", "  "])
def test_bulk_reject_needs_reason(reason):
    _invalid(lambda c: c.approvals.bulk("reject", CRITERIA, reason=reason))


@pytest.mark.parametrize("collection", ["", "1orders", "or-ders", "a b", "x" * 64, None, 5])
def test_policy_rejects_bad_collection(collection):
    _invalid(lambda c: c.approval_policies.create(collection, "update", "queue", PREDICATE))


@pytest.mark.parametrize("collection", ["_orders", "Orders1", "x" * 63])
def test_policy_accepts_valid_collection(collection):
    assert _body(_sent(lambda c: c.approval_policies.create(collection, "insert", "queue", PREDICATE)))["collection"] \
        == collection


@pytest.mark.parametrize("operation", ["", "upsert", None, "UPDATE"])
def test_policy_rejects_bad_operation(operation):
    _invalid(lambda c: c.approval_policies.create("orders", operation, "queue", PREDICATE))


@pytest.mark.parametrize("action", ["", "approve", None])
def test_policy_rejects_bad_action(action):
    _invalid(lambda c: c.approval_policies.create("orders", "update", action, PREDICATE))


@pytest.mark.parametrize("predicate", [None, [], [PREDICATE], "gt", 5, True])
def test_policy_predicate_must_be_an_object(predicate):
    _invalid(lambda c: c.approval_policies.create("orders", "update", "queue", predicate))


def test_requires_project_slug():
    c, _ = _client(project_slug=None)
    for call in (lambda: c.approvals.list(), lambda: c.approval_policies.list(),
                 lambda: c.schema.collections.list()):
        with pytest.raises(ValueError, match="project_slug"):
            call()


ROW = {"id": "apr_7FjMvznAaVjl", "status": "rejected", "collection": "conformance_kv", "operation": "insert",
       "makerId": "usr_conformance0001", "proposedPayload": {"id": "w2-probe", "val": "probe"},
       "targetRecordId": None, "approverId": "usr_conformance0001", "decidedAt": "2026-10-26T18:25:20.573Z",
       "decisionReason": "probe, not intended", "createdAt": "2026-10-26T18:25:19.818Z", "expiresAt": None}


def test_list_pages_follow_the_cursor():
    def handler(req):
        if "cursor" in dict(parse_qsl(req.url.query.decode())):
            return httpx.Response(200, json={"data": [{**ROW, "id": "apr_2"}], "meta": {"cursor": {"next": None}}})
        return httpx.Response(200, json={"data": [ROW], "meta": {"cursor": {"next": "eyJ2IjoxLCJwIjoi"}}})

    c, seen = _client(handler)
    p1 = c.approvals.list(limit=1)
    assert p1.meta.cursor.next == "eyJ2IjoxLCJwIjoi"
    p2 = c.approvals.list(limit=1, cursor=p1.meta.cursor.next)
    assert p2.data[0]["id"] == "apr_2"
    assert p2.meta.cursor.next is None
    reqs = [r for r in seen if r.method != "HEAD"]
    assert [_query(r) for r in reqs] == [{"limit": "1"}, {"limit": "1", "cursor": "eyJ2IjoxLCJwIjoi"}]
    assert all(r.method == "GET" and r.url.path == "/@portal/pending-approvals" for r in reqs)


def test_list_rejects_non_string_cursor():
    _invalid(lambda c: c.approvals.list(cursor=5))


def test_invalid_cursor_is_a_kerror():
    def handler(req):
        return httpx.Response(400, json={
            "code": "K9O_INVALID_CURSOR",
            "message": "Cursor is malformed; omit it to start from the newest pending change",
            "context": {}, "recommendation": None})

    c, seen = _client(handler)
    with pytest.raises(KError) as exc:
        c.approvals.list(cursor="abc!")
    assert exc.value.code == "K9O_INVALID_CURSOR"
    assert exc.value.status == 400
    assert _query([r for r in seen if r.method != "HEAD"][0]) == {"cursor": "abc!"}


def _bulk(data):
    def handler(req):
        return httpx.Response(200, json={"data": data, "meta": {}})

    c, seen = _client(handler)
    res = c.approvals.bulk("approve", CRITERIA)
    req = [r for r in seen if r.method != "HEAD"][0]
    assert (req.method, req.url.path, _query(req)) == ("POST", "/@portal/pending-approvals/bulk", {})
    assert _body(req) == {"action": "approve", "criteria": CRITERIA}
    return res


def test_bulk_failed_rows():
    res = _bulk({"attestationId": "apr_x", "action": "approve", "affectedCount": 1, "affectedIds": ["apr_A"],
                 "failed": [{"id": "apr_B", "code": "K9O_STATE_CONFLICT",
                             "reason": "Pending change 'apr_B' was decided concurrently"}], "reason": None})
    assert res.data["affectedIds"] == ["apr_A"]
    assert res.data["failed"] == [{"id": "apr_B", "code": "K9O_STATE_CONFLICT",
                                   "reason": "Pending change 'apr_B' was decided concurrently"}]


def test_bulk_empty_failed():
    res = _bulk({"attestationId": "apr_Vf23zMoj7m0u", "action": "reject", "affectedCount": 0, "affectedIds": [],
                 "failed": [], "reason": "probe: matches nothing"})
    assert res.data["failed"] == []


def test_bulk_absent_failed_decodes_as_empty_list():
    res = _bulk({"attestationId": "apr_x", "action": "approve", "affectedCount": 0, "affectedIds": [],
                 "reason": None})
    assert res.data["failed"] == []


# --- approvals.comments -------------------------------------------------------------------------------------------

COMMENT = {"id": "acm_1", "caseId": "apr_1", "authorId": "usr_1", "visibility": "everyone", "body": "Looks odd",
           "createdAt": "2026-11-13T10:00:00Z"}


def test_comments_add_wire_shape():
    req = _sent(lambda c: c.approvals.comments.add("apr_1", "Looks odd", "everyone"))
    assert req.method == "POST"
    assert req.url.path == "/@portal/pending-approvals/apr_1/comments"
    assert _query(req) == {}
    assert _body(req) == {"body": "Looks odd", "visibility": "everyone"}


def test_comments_add_parses_201_answer():
    def handler(req):
        return httpx.Response(201, json={"data": COMMENT, "meta": {"cost": {}}})

    c, _ = _client(handler)
    res = c.approvals.comments.add("apr_1", "Looks odd", "everyone")
    assert res.data == COMMENT


def test_comments_add_accepts_4000_chars_and_path_encodes_id():
    req = _sent(lambda c: c.approvals.comments.add("a/b", "x" * 4000, "approvers_only"))
    assert req.url.raw_path.decode() == "/@portal/pending-approvals/a%2Fb/comments"
    assert len(_body(req)["body"]) == 4000


@pytest.mark.parametrize("pending_id", ["", None, 5])
def test_comments_add_rejects_bad_id(pending_id):
    _invalid(lambda c: c.approvals.comments.add(pending_id, "hi", "everyone"))


@pytest.mark.parametrize("body", ["", "   ", "x" * 4001, None, 5, "a\x00b"])
def test_comments_add_rejects_bad_body(body):
    _invalid(lambda c: c.approvals.comments.add("apr_1", body, "everyone"))


@pytest.mark.parametrize("visibility", ["", "private", "Everyone", None, True])
def test_comments_add_rejects_bad_visibility(visibility):
    _invalid(lambda c: c.approvals.comments.add("apr_1", "hi", visibility))


@pytest.mark.parametrize("status,code", [(403, "K9O_APPROVAL_COMMENT_NOT_ELIGIBLE"), (409, "K9O_APPROVAL_COMMENT_CLOSED"),
                                         (404, "K9O_NOT_FOUND"), (403, "K9O_RBAC_DENY")])
def test_comments_add_maps_errors(status, code):
    def handler(req):
        return httpx.Response(status, json={"code": code, "message": "no", "context": {"reason": "session_required"}})

    c, _ = _client(handler)
    with pytest.raises(KError) as exc:
        c.approvals.comments.add("apr_1", "hi", "everyone")
    assert exc.value.code == code
    assert exc.value.status == status


def test_comments_list_wire_shape():
    req = _sent(lambda c: c.approvals.comments.list("apr_1", limit=2, cursor="abc"))
    assert req.method == "GET"
    assert req.url.path == "/@portal/pending-approvals/apr_1/comments"
    assert _query(req) == {"limit": "2", "cursor": "abc"}
    assert _body(req) is None
    assert _query(_sent(lambda c: c.approvals.comments.list("apr_1"))) == {}


def test_comments_list_parses_page_and_cursor():
    pages = {
        None: {"data": [COMMENT], "nextCursor": "tok2", "meta": {"cost": {}}},
        "tok2": {"data": [{**COMMENT, "id": "acm_2"}], "nextCursor": None, "meta": {"cost": {}}},
    }

    def handler(req):
        return httpx.Response(200, json=pages[dict(parse_qsl(req.url.query.decode())).get("cursor")])

    c, _ = _client(handler)
    first = c.approvals.comments.list("apr_1", limit=1)
    assert [x["id"] for x in first.data] == ["acm_1"]
    assert first.meta.cursor.next == "tok2"
    last = c.approvals.comments.list("apr_1", limit=1, cursor=first.meta.cursor.next)
    assert [x["id"] for x in last.data] == ["acm_2"]
    assert last.meta.cursor.next is None


def test_comments_list_empty_page():
    def handler(req):
        return httpx.Response(200, json={"data": [], "nextCursor": None, "meta": {}})

    c, _ = _client(handler)
    res = c.approvals.comments.list("apr_1")
    assert res.data == [] and res.meta.cursor.next is None


@pytest.mark.parametrize("limit", BAD_LIMITS)
def test_comments_list_rejects_bad_limit(limit):
    _invalid(lambda c: c.approvals.comments.list("apr_1", limit=limit))


def test_comments_list_rejects_bad_id_and_cursor():
    _invalid(lambda c: c.approvals.comments.list(""))
    _invalid(lambda c: c.approvals.comments.list("apr_1", cursor=5))


def test_comments_need_project_slug():
    c = create_client(base_url=BASE, api_key="k")
    with pytest.raises(ValueError):
        c.approvals.comments.list("apr_1")
