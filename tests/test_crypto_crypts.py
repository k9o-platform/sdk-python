"""client.crypto.crypts (stubbed transport): list, create, retire, name check, fail-closed answers."""

from __future__ import annotations

import json

import httpx
import pytest

from k9o import Crypt, KError, create_client

BASE = "https://acme.api.k9o.io"
P = "/@portal"
ACTIVE = {"name": "pii", "status": "active", "createdAt": "2026-11-02T10:00:00.000Z", "retiredAt": None}
RETIRED = {"name": "pii", "status": "retired", "createdAt": "2026-11-02T10:00:00.000Z", "retiredAt": "2026-11-03T10:00:00.000Z"}


def _client(response):
    seen: list[httpx.Request] = []

    def handler(req):
        if req.method == "HEAD":
            return httpx.Response(200)
        seen.append(req)
        return response

    c = create_client(base_url=BASE, project_slug="portal", api_key="k")
    c.config.transport = httpx.MockTransport(handler)
    return c, seen


def _ok(data, status=200):
    return httpx.Response(status, json={"data": data, "meta": {}})


def test_list_returns_crypts():
    c, seen = _client(_ok({"crypts": [ACTIVE, {**RETIRED, "name": "old"}]}))
    out = c.crypto.crypts.list()
    assert out == [Crypt("pii", "active", ACTIVE["createdAt"], None), Crypt("old", "retired", RETIRED["createdAt"], RETIRED["retiredAt"])]
    assert [(r.method, r.url.path) for r in seen] == [("GET", P + "/crypto/crypts")]


def test_list_empty():
    c, _ = _client(_ok({"crypts": []}))
    assert c.crypto.crypts.list() == []


def test_create_sends_only_name():
    c, seen = _client(_ok(ACTIVE, 201))
    out = c.crypto.crypts.create("pii")
    assert out.name == "pii" and out.status == "active" and out.retired_at is None
    assert (seen[0].method, seen[0].url.path) == ("POST", P + "/crypto/crypts")
    assert json.loads(seen[0].content) == {"name": "pii"}


def test_retire_sends_no_body_to_the_right_url():
    c, seen = _client(_ok(RETIRED))
    out = c.crypto.crypts.retire("pii")
    assert out.status == "retired" and out.retired_at == RETIRED["retiredAt"]
    assert (seen[0].method, seen[0].url.path) == ("POST", P + "/crypto/crypts/pii/retire")
    assert seen[0].content in (b"", None)


@pytest.mark.parametrize("bad", ["", "actions", "Pii", "a_b", "a/b", "x" * 65, "pii/retire", None, 5])
@pytest.mark.parametrize("op", ["create", "retire"])
def test_bad_names_refused_with_nothing_sent(op, bad):
    c, seen = _client(_ok(ACTIVE))
    with pytest.raises(KError) as e:
        getattr(c.crypto.crypts, op)(bad)
    assert e.value.code == "K9O_INVALID_FIELD_VALUE" and e.value.context.get("field") == "name"
    assert e.value.message.startswith(f"crypto.crypts.{op}:")
    assert seen == []


@pytest.mark.parametrize("name", ["a", "actions-2", "x" * 64])
def test_good_edge_names_are_sent(name):
    c, seen = _client(_ok({**ACTIVE, "name": name}, 201))
    assert c.crypto.crypts.create(name).name == name
    assert json.loads(seen[0].content) == {"name": name}


@pytest.mark.parametrize("op,call,status,code", [
    ("create", lambda c: c.crypto.crypts.create("pii"), 409, "K9O_CRYPT_ALREADY_EXISTS"),
    ("create", lambda c: c.crypto.crypts.create("pii"), 409, "K9O_CRYPT_LIMIT_EXCEEDED"),
    ("create", lambda c: c.crypto.crypts.create("pii"), 409, "K9O_CRYPT_RETIRED"),
    ("retire", lambda c: c.crypto.crypts.retire("pii"), 404, "K9O_CRYPT_NOT_FOUND"),
    ("retire", lambda c: c.crypto.crypts.retire("pii"), 409, "K9O_CRYPT_RETIRED"),
    ("retire", lambda c: c.crypto.crypts.retire("pii"), 403, "K9O_RBAC_DENY"),
    ("create", lambda c: c.crypto.crypts.create("pii"), 403, "K9O_IAM_POLICY_DENIED"),
    ("list", lambda c: c.crypto.crypts.list(), 403, "K9O_ACCESS_DENIED"),
])
def test_gateway_refusal_passes_through_once(op, call, status, code):
    c, seen = _client(httpx.Response(status, json={"code": code, "message": "no", "context": {}}))
    with pytest.raises(KError) as e:
        call(c)
    assert (e.value.code, e.value.status) == (code, status) and len(seen) == 1


@pytest.mark.parametrize("op,call,data,why", [
    ("list", lambda c: c.crypto.crypts.list(), {}, "without crypts"),
    ("list", lambda c: c.crypto.crypts.list(), {"crypts": "x"}, "without crypts"),
    ("list", lambda c: c.crypto.crypts.list(), {"crypts": [{**ACTIVE, "status": "gone"}]}, "with an unknown status"),
    ("create", lambda c: c.crypto.crypts.create("pii"), {k: v for k, v in ACTIVE.items() if k != "name"}, "without a name"),
    ("create", lambda c: c.crypto.crypts.create("pii"), {k: v for k, v in ACTIVE.items() if k != "createdAt"}, "without createdAt"),
    ("retire", lambda c: c.crypto.crypts.retire("pii"), {**RETIRED, "retiredAt": 5}, "with a retiredAt that is neither a date nor null"),
    ("retire", lambda c: c.crypto.crypts.retire("pii"), {**ACTIVE, "status": "paused"}, "with an unknown status"),
    ("retire", lambda c: c.crypto.crypts.retire("pii"), None, "without a crypt"),
])
def test_malformed_answer_fails_closed(op, call, data, why):
    c, _ = _client(_ok(data))
    with pytest.raises(KError) as e:
        call(c)
    assert e.value.code == "K9O_INTERNAL_ERROR"
    assert e.value.message == f"crypto.crypts.{op}: the gateway answered {why}"


def test_federated_token_provider_and_project_id_only_when_non_empty():
    base = {"token": "t", "expiresAt": "e", "issuer": "i", "subject": "s", "audience": "a", "kid": "k"}
    c, _ = _client(_ok({**base, "provider": "aws", "projectId": "p1"}))
    t = c.crypto.federated_token("pii", "encrypt")
    assert (t.provider, t.project_id) == ("aws", "p1")
    c, _ = _client(_ok({**base, "provider": "", "projectId": 5}))
    t = c.crypto.federated_token("pii", "encrypt")
    assert (t.provider, t.project_id) == (None, None)
