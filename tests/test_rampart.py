"""client.rampart: classify and jwks wire, receipt verification (signed in the test with a fresh key)."""

from __future__ import annotations

import base64
import hashlib
import json
import os

import httpx
import pytest

from k9o import KError, ReceiptRefusal, create_client
from k9o._jcs import canonicalize
from k9o.rampart import REFUSAL_REASONS

from ._ed_sign import public_key, sign

BASE = "https://acme.api.k9o.io"
ISS = "https://api.k9o.io/rampart"
PROJECT_ID = "proj_abc123"
KID = "rampart-classify-test"
NOW = 1_800_000_000
INPUT = {"text": "hello", "n": [1, 2]}


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


SEED = os.urandom(32)
OTHER_SEED = os.urandom(32)


def jwk(seed=SEED, kid=KID):
    return {"kty": "OKP", "crv": "Ed25519", "x": b64(public_key(seed)), "kid": kid, "alg": "EdDSA", "use": "sig"}


def claims(**over):
    base = {
        "iss": ISS, "iat": NOW - 10, "exp": NOW + 3590,
        "input_sha256": hashlib.sha256(canonicalize(INPUT)).hexdigest(),
        "classification": "suspicious", "risk_bp": 6200, "confidence_bp": 8100, "categories": ["injection"],
        "project_id": PROJECT_ID, "calibration_version": "cal-1",
    }
    base.update(over)
    return base


def receipt(claim_over=None, header_over=None, seed=SEED):
    header = {"alg": "EdDSA", "kid": KID, "typ": "k9o.rampart.classify.v1"}
    header.update(header_over or {})
    head = b64(json.dumps(header).encode())
    body = b64(json.dumps(claims(**(claim_over or {}))).encode())
    sig = sign(seed, f"{head}.{body}".encode())
    return f"{head}.{body}.{b64(sig)}"


def client(handler=None, **kw):
    seen: list[httpx.Request] = []

    def inner(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        if req.method == "HEAD":
            return httpx.Response(200)
        if handler is None:
            raise AssertionError(f"unexpected request {req.method} {req.url}")
        return handler(req)

    c = create_client(base_url=BASE, project_slug="portal", api_key="tok_secret", **kw)
    c.config.transport = httpx.MockTransport(inner)
    return c, seen


def real(seen):
    return [r for r in seen if r.method != "HEAD"]


def verify(c, rcpt=None, **opts):
    opts.setdefault("jwks", {"keys": [jwk()], "issuer": ISS})
    opts.setdefault("project_id", PROJECT_ID)
    opts.setdefault("clock", lambda: NOW)
    return c.rampart.verify_receipt(rcpt if rcpt is not None else receipt(), INPUT, **opts)


# ---- classify / jwks wire ---------------------------------------------------------------------------------

def classify_handler(rcpt, status=200):
    def handler(req):
        if status != 200:
            return httpx.Response(status, json={"code": "K9O_RAMPART_CLASSIFIER_UNAVAILABLE",
                                                "message": "Rampart classifier unavailable", "context": {}})
        return httpx.Response(200, json={
            "data": {"classification": "suspicious", "riskScore": 6200, "confidence": 8100,
                     "categories": ["injection"], "calibrationVersion": "cal-1", "classificationReceipt": rcpt,
                     "projectId": PROJECT_ID},
            "meta": {"context": {"requestId": "req_1"}}})
    return handler


def test_classify_wire_and_decoding():
    rcpt = receipt()
    c, seen = client(classify_handler(rcpt))
    r = c.rampart.classify(INPUT, label="chat-turn")
    (req,) = real(seen)
    assert req.method == "POST" and req.url.path == "/@portal/rampart/classify"
    assert json.loads(req.content) == {"input": INPUT, "label": "chat-turn"}
    assert req.headers["authorization"] == "Bearer tok_secret"
    assert (r.classification, r.risk_score, r.confidence, r.categories, r.calibration_version) == (
        "suspicious", 6200, 8100, ["injection"], "cal-1")
    assert r.classification_receipt == rcpt and r.project_id == PROJECT_ID


def test_classify_without_a_project_id_in_the_response_leaves_it_none():
    def handler(req):
        return httpx.Response(200, json={"data": {"classification": "clear", "riskScore": 0, "confidence": 9000,
                                                  "categories": [], "calibrationVersion": "cal-1",
                                                  "classificationReceipt": "a.b.c"}, "meta": {}})
    c, _ = client(handler)
    assert c.rampart.classify(INPUT).project_id is None


def test_classify_without_label_omits_it():
    c, seen = client(classify_handler(receipt()))
    c.rampart.classify(INPUT)
    assert json.loads(real(seen)[0].content) == {"input": INPUT}


def test_classify_503_is_an_error_with_no_verdict_and_is_not_retried():
    c, seen = client(classify_handler("", status=503))
    with pytest.raises(KError) as e:
        c.rampart.classify(INPUT)
    assert e.value.code == "K9O_RAMPART_CLASSIFIER_UNAVAILABLE" and e.value.status == 503
    assert len(real(seen)) == 1


@pytest.mark.parametrize("input_,label", [("text", None), ({"a": 1}, ""), ({"a": 1}, "x" * 129), ({"a": 1}, 5)])
def test_classify_checks_arguments_before_sending(input_, label):
    c, seen = client()
    with pytest.raises(KError) as e:
        c.rampart.classify(input_, label=label)
    assert e.value.code == "K9O_INVALID_FIELD_VALUE" and not real(seen)


def test_classify_rejects_a_response_without_a_verdict():
    c, _ = client(lambda r: httpx.Response(200, json={"data": {"classification": "fine"}, "meta": {}}))
    with pytest.raises(KError):
        c.rampart.classify(INPUT)


def test_jwks_wire():
    feed = {"keys": [jwk()], "issuer": ISS, "meta": {"context": {"requestId": "req_2"}}}
    c, seen = client(lambda r: httpx.Response(200, json=feed))
    r = c.rampart.jwks()
    (req,) = real(seen)
    assert req.method == "GET" and req.url.path == "/rampart/jwks" and req.url.query == b""
    assert "authorization" not in req.headers
    assert r.data == {"keys": [jwk()], "issuer": ISS}


def test_jwks_without_an_issuer_has_no_issuer_key():
    c, _ = client(lambda r: httpx.Response(200, json={"keys": [jwk()]}))
    assert c.rampart.jwks().data == {"keys": [jwk()]}


def test_jwks_error_is_a_kerror():
    c, _ = client(lambda r: httpx.Response(404, json={"code": "K9O_NOT_FOUND", "message": "nope"}),
                  retry_config=None)
    with pytest.raises(KError) as e:
        c.rampart.jwks()
    assert e.value.code == "K9O_NOT_FOUND"


# ---- verify_receipt ------------------------------------------------------------------------------------------

def test_verify_happy_path_with_an_injected_key_set_makes_no_request():
    c, seen = client()
    v = verify(c)
    assert (v.classification, v.risk_score, v.confidence, v.categories) == ("suspicious", 6200, 8100, ["injection"])
    assert v.calibration_version == "cal-1" and v.project_id == PROJECT_ID and v.key_id == KID
    assert v.issuer == ISS and v.input_sha256 == claims()["input_sha256"]
    assert seen == []


def test_the_verdict_comes_from_the_signed_claims_not_the_response_body():
    c, _ = client(classify_handler(receipt(claim_over={"classification": "malicious"})))
    r = c.rampart.classify(INPUT)
    assert r.classification == "suspicious"  # the unsigned body
    v = c.rampart.verify_receipt(r, INPUT, jwks={"keys": [jwk()], "issuer": ISS}, clock=lambda: NOW)
    assert v.classification == "malicious"  # the verified claims


def reason(c, rcpt=None, **opts):
    with pytest.raises(ReceiptRefusal) as e:
        verify(c, rcpt, **opts)
    assert isinstance(e.value, KError) and e.value.reason in REFUSAL_REASONS
    return e.value.reason


def test_wrong_kid():
    c, _ = client()
    assert reason(c, receipt(header_over={"kid": "other"})) == "key_unknown"


def test_a_key_carried_in_the_receipt_is_never_used():
    c, _ = client()
    r = receipt(header_over={"kid": "mine", "jwk": jwk(kid="mine")})
    assert reason(c, r) == "key_unknown"


def test_wrong_alg():
    c, _ = client()
    for alg in ("none", "HS256", "RS256", "eddsa"):
        assert reason(c, receipt(header_over={"alg": alg})) == "alg"


def test_wrong_typ():
    c, _ = client()
    assert reason(c, receipt(header_over={"typ": "JWT"})) == "typ"
    assert reason(c, receipt(header_over={"typ": "k9o.rampart.classify.v2"})) == "typ"


def test_wrong_issuer():
    c, _ = client()
    assert reason(c, receipt(claim_over={"iss": "https://api.evil.example/rampart"})) == "issuer"
    assert reason(c, receipt(claim_over={"iss": "https://api.k9o.io/warden"})) == "issuer"


def test_issuer_option_overrides_the_feed():
    c, _ = client()
    r = receipt(claim_over={"iss": "https://api.corp.internal/rampart"})
    assert verify(c, r, issuer="https://api.corp.internal/rampart").classification == "suspicious"
    assert reason(c, receipt(), issuer="https://api.corp.internal/rampart") == "issuer"


def test_feed_without_an_issuer_and_no_option_is_refused():
    c, _ = client()
    with pytest.raises(ReceiptRefusal) as e:
        verify(c, jwks={"keys": [jwk()]})
    assert e.value.reason == "issuer" and "no issuer is advertised" in e.value.detail
    with pytest.raises(ReceiptRefusal) as e:
        verify(c, jwks=[jwk()])
    assert e.value.reason == "issuer"
    assert verify(c, jwks=[jwk()], issuer=ISS).issuer == ISS


def test_wrong_project():
    c, _ = client()
    assert reason(c, receipt(claim_over={"project_id": "proj_other"})) == "project"


def test_bare_receipt_without_a_project_id_is_project_unknown():
    c, _ = client()
    assert reason(c, project_id=None) == "project_unknown"


def test_classify_result_carries_the_project_id():
    rcpt = receipt()
    c, _ = client(classify_handler(rcpt))
    r = c.rampart.classify(INPUT)
    feed = {"keys": [jwk()], "issuer": ISS}
    assert c.rampart.verify_receipt(r, INPUT, jwks=feed, clock=lambda: NOW).project_id == PROJECT_ID
    other = receipt(claim_over={"project_id": "proj_other"})
    c2, _ = client(classify_handler(other))
    with pytest.raises(ReceiptRefusal) as e:
        c2.rampart.verify_receipt(c2.rampart.classify(INPUT), INPUT, jwks=feed, clock=lambda: NOW)
    assert e.value.reason == "project"


def test_project_id_option_wins_over_the_classify_result():
    rcpt = receipt(claim_over={"project_id": "proj_other"})
    c, _ = client(classify_handler(rcpt))
    r = c.rampart.classify(INPUT)  # carries PROJECT_ID, the receipt says proj_other
    feed = {"keys": [jwk()], "issuer": ISS}
    assert c.rampart.verify_receipt(r, INPUT, jwks=feed, project_id="proj_other", clock=lambda: NOW)
    with pytest.raises(ReceiptRefusal) as e:
        c.rampart.verify_receipt(r, INPUT, jwks=feed, clock=lambda: NOW)
    assert e.value.reason == "project"


def test_the_project_is_not_learned_from_meta_or_earlier_calls():
    rcpt = receipt()
    c, _ = client(classify_handler(rcpt))
    c.rampart.classify(INPUT)
    with pytest.raises(ReceiptRefusal) as e:
        c.rampart.verify_receipt(rcpt, INPUT, jwks={"keys": [jwk()], "issuer": ISS}, clock=lambda: NOW)
    assert e.value.reason == "project_unknown"


def test_expired_and_not_yet_valid_with_the_default_and_a_custom_skew():
    c, _ = client()
    r = receipt()  # iat NOW-10, exp NOW+3590
    assert verify(c, r, clock=lambda: NOW + 3590 + 60).classification  # at the skew edge
    assert reason(c, r, clock=lambda: NOW + 3590 + 61) == "expired"
    assert reason(c, r, clock=lambda: NOW + 3590 + 20, clock_skew_s=10) == "expired"
    assert reason(c, r, clock=lambda: NOW - 10 - 61) == "not_yet_valid"
    assert verify(c, r, clock=lambda: NOW - 10 - 60).classification


def test_input_hash_mismatch():
    c, _ = client()
    with pytest.raises(ReceiptRefusal) as e:
        c.rampart.verify_receipt(receipt(), {"text": "hello!"}, jwks={"keys": [jwk()], "issuer": ISS},
                                 project_id=PROJECT_ID, clock=lambda: NOW)
    assert e.value.reason == "input_hash"


@pytest.mark.parametrize("bad", [float("nan"), 1.5, 2**53, -(2**53), {"a": [0.25]}])
def test_input_that_cannot_be_canonicalised_is_an_input_hash_refusal(bad):
    c, _ = client()
    with pytest.raises(ReceiptRefusal) as e:
        c.rampart.verify_receipt(receipt(), {"x": bad}, jwks={"keys": [jwk()], "issuer": ISS},
                                 project_id=PROJECT_ID, clock=lambda: NOW)
    assert e.value.reason == "input_hash" and "cannot be canonicalised" in e.value.detail


def test_classify_does_not_prevalidate_the_input():
    c, seen = client(classify_handler(receipt()))
    c.rampart.classify({"x": 1.5})
    assert json.loads(real(seen)[0].content) == {"input": {"x": 1.5}}


def test_every_shared_jcs_case_matches_through_the_receipt_hash():
    cases = json.load(open(os.path.join(os.path.dirname(__file__), "fixtures", "jcs-codepoint-cases.json"),
                           encoding="utf-8"))["cases"]
    c, _ = client()
    for case in cases:
        feed = {"keys": [jwk()], "issuer": ISS}
        if "expectError" in case:
            with pytest.raises(ReceiptRefusal) as e:
                c.rampart.verify_receipt(receipt(), case["input"], jwks=feed, project_id=PROJECT_ID,
                                         clock=lambda: NOW)
            assert e.value.reason == "input_hash", case["name"]
        else:
            digest = hashlib.sha256(case["expectCanonical"].encode("utf-8")).hexdigest()
            rcpt = receipt(claim_over={"input_sha256": digest})
            assert c.rampart.verify_receipt(rcpt, case["input"], jwks=feed, project_id=PROJECT_ID,
                                            clock=lambda: NOW).input_sha256 == digest, case["name"]


def test_tampered_signature_and_tampered_claims_and_wrong_signer():
    c, _ = client()
    head, body, sig = receipt().split(".")
    raw = bytearray(base64.urlsafe_b64decode(sig + "=="))
    raw[0] ^= 1
    assert reason(c, f"{head}.{body}.{b64(bytes(raw))}") == "signature"
    forged = b64(json.dumps(claims(classification="clear", risk_bp=0)).encode())
    assert reason(c, f"{head}.{forged}.{sig}") == "signature"
    assert reason(c, receipt(seed=OTHER_SEED)) == "signature"


@pytest.mark.parametrize("bad", ["", "a.b", "a.b.c.d", "a..c", "!!.@@.##", "e30.e30.", 5])
def test_malformed_receipts(bad):
    c, _ = client()
    assert reason(c, bad) == "malformed"


def test_malformed_header_and_claims_shapes():
    c, _ = client()
    not_json = b64(b"nope")
    assert reason(c, f"{not_json}.{not_json}.{not_json}") == "malformed"
    assert reason(c, f"{b64(b'[]')}.{b64(b'{}')}.AAAA") == "malformed"
    assert reason(c, f"{b64(b'{}')}.{b64(b'{}')}.AAAA") == "malformed"  # no kid
    # signed but with a verdict that is not one of the closed values
    assert reason(c, receipt(claim_over={"classification": "fine"})) == "malformed"
    assert reason(c, receipt(claim_over={"iat": "now"})) == "malformed"
    assert reason(c, receipt(claim_over={"risk_bp": 1.5})) == "malformed"


def test_unusable_key_is_key_unknown():
    c, _ = client()
    bad = dict(jwk(), crv="P-256")
    assert reason(c, jwks=[bad]) == "key_unknown"


# ---- the key feed ----------------------------------------------------------------------------------------------

def feed_handler(feeds):
    """Answers each /rampart/jwks request with the next feed in `feeds` (the last one repeats)."""
    calls = {"n": 0}

    def handler(req):
        assert req.url.path == "/rampart/jwks"
        feed = feeds[min(calls["n"], len(feeds) - 1)]
        calls["n"] += 1
        return httpx.Response(200, json={"keys": feed, "issuer": ISS, "meta": {}})

    return handler, calls


def fetching(c, rcpt=None):
    return c.rampart.verify_receipt(rcpt or receipt(), INPUT, project_id=PROJECT_ID, clock=lambda: NOW)


def test_verify_fetches_the_feed_itself_and_caches_it_for_the_clients_life():
    handler, calls = feed_handler([[jwk()]])
    c, seen = client(handler)
    fetching(c)
    fetching(c)
    fetching(c)
    assert calls["n"] == 1
    assert real(seen)[0].url.path == "/rampart/jwks"


def test_an_unknown_kid_refetches_exactly_once_then_refuses():
    handler, calls = feed_handler([[jwk()]])
    c, _ = client(handler)
    fetching(c)  # fills the cache: 1 request
    with pytest.raises(ReceiptRefusal) as e:
        fetching(c, receipt(header_over={"kid": "rotated"}))
    assert e.value.reason == "key_unknown"
    assert calls["n"] == 2  # exactly one refetch


def test_a_rotated_key_is_found_by_the_single_refetch():
    new_seed = os.urandom(32)
    handler, calls = feed_handler([[jwk()], [jwk(), jwk(new_seed, "rotated")]])
    c, _ = client(handler)
    fetching(c)
    r = receipt(header_over={"kid": "rotated"}, seed=new_seed)
    assert fetching(c, r).key_id == "rotated"
    assert calls["n"] == 2
    fetching(c, r)  # now cached
    assert calls["n"] == 2


def test_an_unreachable_feed_is_an_error_not_a_pass():
    c, _ = client(lambda r: httpx.Response(503, json={"code": "K9O_UPSTREAM_UNAVAILABLE", "message": "down"}),
                  retry_config=__import__("k9o").RetryConfig(max_retries=0))
    with pytest.raises(KError) as e:
        fetching(c)
    assert not isinstance(e.value, ReceiptRefusal) and e.value.code == "K9O_UPSTREAM_UNAVAILABLE"


def test_an_injected_key_set_is_never_refetched():
    c, seen = client()
    assert reason(c, receipt(header_over={"kid": "rotated"})) == "key_unknown"
    assert seen == []


def test_the_issuer_comes_from_the_cached_feed_and_follows_a_refetch():
    handler, calls = feed_handler([[jwk()]])
    c, _ = client(handler)
    fetching(c)
    assert calls["n"] == 1 and c.rampart._issuer == ISS


# ---- windows claim and unknown claims ------------------------------------------------------------------------

def test_windows_claim_is_exposed_as_a_number():
    c, _ = client()
    assert verify(c, receipt(claim_over={"windows": 3})).windows == 3


def test_a_receipt_without_windows_still_verifies_with_none():
    c, _ = client()
    assert verify(c).windows is None


def test_unknown_claims_are_tolerated():
    c, _ = client()
    v = verify(c, receipt(claim_over={"windows": 1, "some_future_claim": {"a": [1]}}))
    assert v.windows == 1 and v.classification == "suspicious"


@pytest.mark.parametrize("bad", [0, -1, 1.5, "2", True, [1]])
def test_a_windows_claim_that_is_not_a_whole_number_is_malformed(bad):
    c, _ = client()
    assert reason(c, receipt(claim_over={"windows": bad})) == "malformed"


def test_the_windows_claim_is_covered_by_the_signature():
    c, _ = client()
    good = receipt(claim_over={"windows": 2}).split(".")
    forged = ".".join([good[0], b64(json.dumps(claims(windows=1)).encode()), good[2]])
    assert reason(c, forged) == "signature"
