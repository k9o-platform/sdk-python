"""client.custody.status: wire shape and response handling."""

from __future__ import annotations

import httpx
import pytest

from k9o import KError, create_client

BASE = "https://acme.api.k9o.io"
META = {"requestId": "req_1"}
PLATFORM = {"mode": "PLATFORM_MANAGED", "attached": False, "provider": None, "lastJob": None}
HYOK = {
    "mode": "HYOK", "attached": True, "provider": "hsm", "lastJob": None,
    "hsm": {"vendor": "aws-cloudhsm", "clusterId": "cluster-1234567890abcdef", "region": "de01",
            "attestation": {"status": None, "verifiedAt": None}, "keys": None},
}


def _client(status=200, body=None, project_slug="portal"):
    seen: list[httpx.Request] = []

    def handler(req):
        seen.append(req)
        return httpx.Response(status, json=body)

    c = create_client(base_url=BASE, project_slug=project_slug, api_key="k")
    c.config.transport = httpx.MockTransport(handler)
    return c, seen


def test_status_without_hsm_key_reads_hsm_as_none():
    c, seen = _client(body={"data": dict(PLATFORM), "meta": META})
    r = c.custody.status()
    req = [x for x in seen if x.method != "HEAD"][0]
    assert req.method == "GET"
    assert req.url.path == "/@portal/custody/status"
    assert req.url.query == b""
    assert r.data == {**PLATFORM, "hsm": None}


def test_status_hsm_details():
    c, _ = _client(body={"data": HYOK, "meta": META})
    d = c.custody.status().data
    assert d["provider"] == "hsm"
    assert d["hsm"]["vendor"] == "aws-cloudhsm"
    assert d["hsm"]["clusterId"] == "cluster-1234567890abcdef"
    assert d["hsm"]["attestation"] == {"status": None, "verifiedAt": None}
    assert d["hsm"]["keys"] is None


def test_status_last_job_passes_through():
    job = {"kind": "transition", "status": "completed", "completedAt": "2026-10-26T12:00:00.000Z"}
    c, _ = _client(body={"data": {**PLATFORM, "provider": "platform", "lastJob": job}, "meta": META})
    assert c.custody.status().data["lastJob"] == job


@pytest.mark.parametrize("status,code", [(403, "K9O_RBAC_DENY"), (404, "K9O_NOT_FOUND"),
                                         (502, "K9O_UPSTREAM_UNAVAILABLE")])
def test_status_errors_raise_kerror(status, code):
    c, _ = _client(status=status, body={"code": code, "message": "no", "context": {}, "recommendation": None})
    with pytest.raises(KError) as e:
        c.custody.status()
    assert e.value.code == code
    assert e.value.status == status


def test_status_needs_project_slug():
    c = create_client(base_url=BASE, api_key="k")
    with pytest.raises(ValueError):
        c.custody.status()
