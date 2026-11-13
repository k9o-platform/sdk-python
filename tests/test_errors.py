"""Tests for KError + kerror_from_wire."""

import pytest

from k9o import KError, kerror_from_wire


def test_kerror_constructs_with_required_fields():
    e = KError(code="K9O_INVALID_FIELD_VALUE", message="bad")
    assert e.code == "K9O_INVALID_FIELD_VALUE"
    assert e.message == "bad"
    assert e.context == {}
    assert e.recommendation is None
    assert e.status is None
    assert e.request_id is None


def test_kerror_context_is_frozen():
    e = KError(code="X", message="m", context={"dim": "mau"})
    assert e.context["dim"] == "mau"
    with pytest.raises(TypeError):
        e.context["dim"] = "modified"


def test_kerror_is_exception():
    with pytest.raises(KError) as exc_info:
        raise KError(code="X", message="m")
    assert exc_info.value.code == "X"


def test_kerror_from_wire_full_payload():
    wire = {
        "code": "K9O_TIER_CAP_HIT",
        "message": "cap exceeded",
        "context": {"dimension": "mau", "limit": 1000},
        "recommendation": "upgrade to Ascent",
        "requestId": "req_abc",
    }
    e = kerror_from_wire(wire, 429)
    assert e.code == "K9O_TIER_CAP_HIT"
    assert e.status == 429
    assert e.context == {"dimension": "mau", "limit": 1000}
    assert e.recommendation == "upgrade to Ascent"
    assert e.request_id == "req_abc"


def test_kerror_from_wire_passes_unknown_codes_verbatim():
    e = kerror_from_wire({"code": "K9O_NOVEL_CODE", "message": "y"}, 422)
    assert e.code == "K9O_NOVEL_CODE"
    assert e.status == 422


def test_kerror_from_wire_tolerates_missing_optional_fields():
    e = kerror_from_wire({"code": "X", "message": "y"}, 500)
    assert e.context == {}
    assert e.recommendation is None
    assert e.request_id is None


def test_kerror_from_wire_handles_request_id_snake_case_alt():
    """Some upstreams may send snake_case `request_id` — accept either."""
    e = kerror_from_wire({"code": "X", "message": "y", "request_id": "alt"}, 500)
    assert e.request_id == "alt"


def _wire(rec):
    return {"code": "K9O_X", "message": "m", "context": {"x": 1}, "recommendation": rec}


@pytest.mark.parametrize(
    "rec,text,after",
    [
        ({"action": "Open Billing."}, "Open Billing.", None),
        ({"retryAfterSec": 30, "note": "retry"}, "retry", 30),
        (None, None, None),
        ("plain", "plain", None),
        (7, None, None),
        (["a"], None, None),
        ({"retryAfterSec": -1}, None, None),
        ({"retryAfterSec": "5", "action": 3, "note": "n"}, "n", None),
    ],
)
def test_recommendation_shapes(rec, text, after):
    e = kerror_from_wire(_wire(rec), 429)
    assert (e.code, e.message, e.status, dict(e.context)) == ("K9O_X", "m", 429, {"x": 1})
    assert e.recommendation == text
    assert e.retry_after_sec == after


def _get_404(headers, body):
    import httpx

    from k9o.http import HttpClientConfig, request

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json=body, headers=headers)

    cfg = HttpClientConfig(base_url="https://acme.api-de01.k9o.io", transport=httpx.MockTransport(handler))
    with pytest.raises(KError) as exc_info:
        request(cfg, method="GET", path="/orgs/x")
    return exc_info.value


_NOT_FOUND = {"code": "K9O_NOT_FOUND", "message": "gone"}


def test_request_id_comes_from_response_header_when_body_has_none():
    assert _get_404({"x-k9o-request-id": "req_abc"}, _NOT_FOUND).request_id == "req_abc"


def test_request_id_from_body_wins_over_header():
    e = _get_404({"x-k9o-request-id": "req_hdr"}, {**_NOT_FOUND, "requestId": "req_body"})
    assert e.request_id == "req_body"


def test_request_id_is_none_without_header_or_body_value():
    assert _get_404({}, _NOT_FOUND).request_id is None


def test_request_id_header_name_is_case_insensitive():
    assert _get_404({"X-K9O-Request-Id": "req_abc"}, _NOT_FOUND).request_id == "req_abc"
