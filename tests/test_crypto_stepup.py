"""client.crypto (stubbed transport), keys.rotate step-up hook, keys.list unpaged, Rampart input must be an object."""

from __future__ import annotations

import base64
import json

import httpx
import pytest

from k9o.crypto import KEY_PIN_UNENFORCED, ZK_CLIENT_CORE_UNAVAILABLE, CryptoItemResult, key_ref_matches
from k9o import KError, StepUpChallenge, StepUpProof, StepUpRequired, create_client

BASE = "https://acme.api.k9o.io"
P = "/@portal"


def _mode(mode="PLATFORM_MANAGED", epoch="ep-1", **extra):
    return _ok({"custodyMode": mode, "epoch": epoch, **extra})


def _client(*responses, modes=None, expected_key_refs=None):
    """A client whose transport answers with `responses` in turn (the last repeats). Returns (client, seen requests).

    `GET /crypto/mode` is answered from `modes` (a platform-managed answer by default; a list answers in turn, the last
    repeats) and is not in `seen`: `c.mode_calls` holds those requests and `c.log` every request in order.
    """
    seen: list[httpx.Request] = []
    mode_calls: list[httpx.Request] = []
    log: list[tuple[str, str]] = []
    mode_answers = list(modes) if isinstance(modes, (list, tuple)) else [modes if modes is not None else _mode()]

    def handler(req):
        if req.method == "HEAD":
            return httpx.Response(200)
        log.append((req.method, req.url.path))
        if req.url.path.endswith("/crypto/mode"):
            mode_calls.append(req)
            r = mode_answers[min(len(mode_calls) - 1, len(mode_answers) - 1)]
            return r(req) if callable(r) else r
        seen.append(req)
        r = responses[min(len(seen) - 1, len(responses) - 1)]
        return r(req) if callable(r) else r

    c = create_client(base_url=BASE, project_slug="portal", api_key="k", expected_key_refs=expected_key_refs)
    c.config.transport = httpx.MockTransport(handler)
    c.mode_calls, c.log = mode_calls, log
    return c, seen


def _ok(data):
    return httpx.Response(200, json={"data": data, "meta": {}})


def _err(status, code, context=None, headers=None):
    return httpx.Response(status, json={"code": code, "message": "no", "context": context or {}}, headers=headers)


def _json(req):
    return json.loads(req.content)


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode()


# crypto -------------------------------------------------------------------------------------------------------------

def test_encrypt_body_and_decoding_in_item_order():
    c, seen = _client(_ok({"items": [{"ciphertext": "AAA="}, {"code": "K9O_X", "message": "bad", "extra": 1}, {"ciphertext": "CCC="}]}))
    out = c.crypto.encrypt("pii-main", [b"\x00\xff", {"plaintext": "héllo", "context": "user-1"}, bytearray(b"z")])
    assert (seen[0].method, seen[0].url.path) == ("POST", f"{P}/crypto/encrypt")
    assert _json(seen[0]) == {"crypt": "pii-main", "custodyEpoch": "ep-1", "items": [
        {"plaintext": _b64(b"\x00\xff")}, {"plaintext": _b64("héllo".encode()), "context": "user-1"}, {"plaintext": "eg=="}]}
    assert [i.ciphertext for i in out.items] == ["AAA=", None, "CCC="]
    assert out.items[1].code == "K9O_X" and out.items[1].message == "bad" and out.items[1].raw["extra"] == 1
    assert out.items[0].ok and not out.items[1].ok and not out.ok


def test_decrypt_decodes_plaintext_bytes_and_text():
    c, seen = _client(_ok({"items": [{"plaintext": _b64("héllo".encode())}]}))
    out = c.crypto.decrypt("pii-main", [{"ciphertext": "AAA=", "context": "user-1"}])
    assert (seen[0].url.path, _json(seen[0])) == (f"{P}/crypto/decrypt", {"crypt": "pii-main", "items": [{"ciphertext": "AAA=", "context": "user-1"}]})
    assert out.items[0].plaintext == "héllo".encode() and out.items[0].text() == "héllo" and out.ok


def test_rewrap_route_and_body():
    c, seen = _client(_ok({"items": [{"ciphertext": "NEW="}]}))
    out = c.crypto.rewrap("pii-main", ["OLD="])
    assert (seen[0].url.path, _json(seen[0])) == (f"{P}/crypto/rewrap", {"crypt": "pii-main", "items": [{"ciphertext": "OLD="}]})
    assert out.items[0].ciphertext == "NEW="


def test_validation_happens_before_anything_is_sent():
    c, seen = _client(_ok({"items": []}))
    bad = [
        lambda: c.crypto.encrypt("Bad Ring", [b"x"]),
        lambda: c.crypto.encrypt("", [b"x"]),
        lambda: c.crypto.encrypt("a" * 65, [b"x"]),
        lambda: c.crypto.encrypt("ok", []),
        lambda: c.crypto.encrypt("ok", [b"x"] * 101),
        lambda: c.crypto.encrypt("ok", "abc"),
        lambda: c.crypto.encrypt("ok", [7]),
        lambda: c.crypto.encrypt("ok", [{"plaintext": b"x", "context": "c" * 257}]),
        lambda: c.crypto.encrypt("ok", [{"plaintext": b"x", "context": 3}]),
        lambda: c.crypto.encrypt("ok", [{"plaintext": b"x", "keyId": "k"}]),
        lambda: c.crypto.decrypt("ok", [""]),
        lambda: c.crypto.decrypt("ok", [b"bytes"]),
        lambda: c.crypto.rewrap("ok", [{"plaintext": "x"}]),
    ]
    for call in bad:
        with pytest.raises(KError) as e:
            call()
        assert e.value.code == "K9O_INVALID_FIELD_VALUE"
    assert seen == []


def test_size_limits_use_the_server_code_and_send_nothing():
    c, seen = _client(_ok({"items": []}))
    with pytest.raises(KError) as e:
        c.crypto.encrypt("ok", [b"x" * (64 * 1024 + 1)])
    assert e.value.code == "K9O_PAYLOAD_TOO_LARGE"
    with pytest.raises(KError) as e:  # 17 items of 64 KiB is over 1 MiB
        c.crypto.encrypt("ok", [b"x" * 65536] * 17)
    assert e.value.code == "K9O_PAYLOAD_TOO_LARGE"
    assert seen == []
    c.crypto.encrypt("ok", [b"x" * 65536] * 16)  # exactly 1 MiB is allowed
    assert len(seen) == 1
    c.crypto.encrypt("ok", [{"plaintext": b"x", "context": "é" * 128}])  # 256 bytes exactly
    assert len(seen) == 2


@pytest.mark.parametrize("status,code,ctx", [
    (400, "K9O_ENVELOPE_INTEGRITY_FAILURE", {}),
    (409, "K9O_SEALED_UNDER_RETIRED_KEY", {"resealableUntil": "2026-11-01T00:00:00Z"}),
    (413, "K9O_PAYLOAD_TOO_LARGE", {}),
    (503, "K9O_KEY_SERVICE_UNAVAILABLE", {}),
    (503, "K9O_BYOK_KEY_UNREACHABLE", {}),
    (503, "K9O_HYOK_CLUSTER_UNREACHABLE", {}),
    (404, "K9O_NOT_FOUND", {}),
])
@pytest.mark.parametrize("verb", ["encrypt", "decrypt", "rewrap"])
def test_each_error_is_typed_and_never_retried(verb, status, code, ctx):
    c, seen = _client(_err(status, code, ctx))
    arg = [b"x"] if verb == "encrypt" else ["AAA="]
    with pytest.raises(KError) as e:
        getattr(c.crypto, verb)("ok", arg)
    assert (e.value.code, e.value.status) == (code, status) and dict(e.value.context) == ctx
    assert len(seen) == 1


@pytest.mark.parametrize("status,code", [(404, "K9O_CRYPT_NOT_FOUND"), (409, "K9O_CRYPT_RETIRED")])
def test_encrypt_passes_crypt_registry_errors_through_unchanged(status, code):
    c, seen = _client(_err(status, code))
    with pytest.raises(KError) as e:
        c.crypto.encrypt("ok", [b"x"])
    assert (e.value.code, e.value.status) == (code, status) and len(seen) == 1


def test_crypt_named_actions_is_refused_and_actions_2_accepted():
    c, seen = _client(_ok({"items": [{"ciphertext": "Y3Q="}]}))
    for verb, arg in (("encrypt", [b"x"]), ("decrypt", ["AAA="]), ("rewrap", ["AAA="])):
        with pytest.raises(KError) as e:
            getattr(c.crypto, verb)("actions", arg)
        assert e.value.code == "K9O_INVALID_FIELD_VALUE"
        assert 'not "actions"' in e.value.message
    with pytest.raises(KError) as e:
        c.crypto.federated_token("actions", "encrypt")
    assert e.value.code == "K9O_INVALID_FIELD_VALUE"
    assert seen == []
    c.crypto.encrypt("actions-2", [b"x"])
    assert _json(seen[0])["crypt"] == "actions-2"


def test_rate_limit_carries_retry_after_and_is_not_retried():
    c, seen = _client(_err(429, "K9O_RATE_LIMITED", headers={"retry-after": "7"}))
    with pytest.raises(KError) as e:
        c.crypto.decrypt("ok", ["AAA="])
    assert e.value.code == "K9O_RATE_LIMITED" and e.value.retry_after_sec == 7 and len(seen) == 1


def test_transport_failure_is_not_retried():
    def boom(req):
        raise httpx.ConnectError("down")

    c, seen = _client(boom)
    with pytest.raises(KError) as e:
        c.crypto.encrypt("ok", [b"x"])
    assert e.value.code == "K9O_UPSTREAM_UNAVAILABLE" and len(seen) == 1


def test_crypto_needs_a_project_slug():
    c = create_client(base_url=BASE, api_key="k")
    with pytest.raises(ValueError):
        c.crypto.encrypt("ok", [b"x"])


# custody router -----------------------------------------------------------------------------------------------------

class FakeCore:
    def __init__(self):
        self.calls: list[tuple[str, str, list]] = []

    def _answer(self, verb, crypt, items):
        self.calls.append((verb, crypt, items))
        key = "plaintext" if verb == "decrypt" else "ciphertext"
        return [CryptoItemResult(**{key: b"core" if verb == "decrypt" else "CORE="}) for _ in items]

    def encrypt(self, crypt, items):
        return self._answer("encrypt", crypt, items)

    def decrypt(self, crypt, items):
        return self._answer("decrypt", crypt, items)

    def rewrap(self, crypt, items):
        return self._answer("rewrap", crypt, items)


def _zk409(route="client", mode="BYOK", epoch="ep-2"):
    return _err(409, "K9O_ZK_STORE_FLOW_REQUIRED", {"custodyMode": mode, "route": route, "epoch": epoch})


def test_platform_managed_encrypt_is_one_mode_check_then_one_post_with_the_epoch():
    c, seen = _client(_ok({"items": [{"ciphertext": "AAA="}]}))
    c.crypto.encrypt("pii", [b"x"])
    assert c.log == [("GET", f"{P}/crypto/mode"), ("POST", f"{P}/crypto/encrypt")]
    assert _json(seen[0]) == {"crypt": "pii", "custodyEpoch": "ep-1", "items": [{"plaintext": "eA=="}]}


@pytest.mark.parametrize("mode", ["PLATFORM_MANAGED", "CONFIDENTIAL_WORKLOAD", "SKR"])
def test_server_custody_modes_use_the_server_route(mode):
    c, seen = _client(_ok({"items": [{"ciphertext": "AAA="}]}), modes=_mode(mode))
    c.crypto.encrypt("pii", [b"x"])
    assert len(seen) == 1


def test_encrypt_mode_check_names_the_crypt_and_mode_does_not():
    c, seen = _client(_ok({"items": [{"ciphertext": "AAA="}]}))
    c.crypto.encrypt("customer-notes", [b"x"])
    assert c.mode_calls[0].url.params.get("crypt") == "customer-notes"
    assert c.mode_calls[0].url.query == b"crypt=customer-notes"
    c.crypto.mode()
    assert c.mode_calls[1].url.query == b""


def test_mode_with_a_crypt_sends_it_without_one_sends_none_and_a_bad_one_is_refused_first():
    c, seen = _client()
    c.crypto.mode("customer-notes")
    assert c.mode_calls[0].url.query == b"crypt=customer-notes"
    c.crypto.mode()
    assert c.mode_calls[1].url.query == b""
    for bad in ["Bad_Name", "", "actions", "a" * 65]:
        with pytest.raises(KError) as e:
            c.crypto.mode(bad)
        assert e.value.code == "K9O_INVALID_FIELD_VALUE"
    assert len(c.mode_calls) == 2 and seen == []


def test_two_encrypts_check_the_mode_twice_and_a_batch_checks_once():
    c, seen = _client(_ok({"items": [{"ciphertext": "AAA="}, {"ciphertext": "BBB="}]}))
    c.crypto.encrypt("pii", [b"x", b"y"])
    assert len(c.mode_calls) == 1
    c.crypto.encrypt("pii", [b"x"])
    assert len(c.mode_calls) == 2 and len(seen) == 2


def test_mode_is_typed_and_reads_the_epoch():
    c, seen = _client(modes=_mode("HYOK", "ep-9"))
    m = c.crypto.mode()
    assert (m.custody_mode, m.epoch) == ("HYOK", "ep-9") and seen == []
    assert (c.mode_calls[0].method, c.mode_calls[0].url.path) == ("GET", f"{P}/crypto/mode")


@pytest.mark.parametrize("route", ["server", "client"])
def test_mode_returns_the_route_the_gateway_sent(route):
    c, _ = _client(modes=_mode("HYOK", route=route))
    assert c.crypto.mode().route == route
    assert c.crypto.mode("customer-notes").route == route


@pytest.mark.parametrize("extra", [{}, {"route": "edge"}, {"route": None}, {"route": 7}])
def test_mode_route_is_none_when_absent_or_unknown(extra):
    c, _ = _client(modes=_mode("HYOK", **extra))
    assert c.crypto.mode().route is None
    assert c.crypto.mode("customer-notes").route is None
    assert c.crypto.mode().custody_mode == "HYOK"


@pytest.mark.parametrize("mode", ["BYOK", "HYOK"])
def test_client_custody_without_a_core_raises_and_sends_no_plaintext(mode):
    c, seen = _client(_ok({"items": []}), modes=_mode(mode))
    with pytest.raises(KError) as e:
        c.crypto.encrypt("pii", [b"secret"])
    assert (e.value.code, e.value.status) == ("K9O_ZK_CLIENT_CORE_UNAVAILABLE", 501) == (ZK_CLIENT_CORE_UNAVAILABLE, 501)
    assert dict(e.value.context) == {"custodyMode": mode, "requirement": "kibo-client-core"}
    assert "client core" in e.value.message and "kibo.encrypt/decrypt" in e.value.message
    assert seen == [] and c.log == [("GET", f"{P}/crypto/mode")]


@pytest.mark.parametrize("mode", ["BYOK", "HYOK"])
def test_client_custody_with_a_core_calls_only_the_core(mode):
    core = FakeCore()
    c, seen = _client(_ok({"items": []}), modes=_mode(mode))
    c.crypto.use_client_core(core)
    out = c.crypto.encrypt("pii", [b"x", {"plaintext": "y", "context": "u1"}])
    assert seen == [] and [i.ciphertext for i in out.items] == ["CORE=", "CORE="]
    assert core.calls == [("encrypt", "pii", [{"plaintext": b"x"}, {"plaintext": b"y", "context": "u1"}])]


def test_epoch_race_rechecks_and_reroutes_once():
    core = FakeCore()
    c, seen = _client(_zk409(), modes=[_mode("PLATFORM_MANAGED", "ep-1"), _mode("BYOK", "ep-2")])
    c.crypto.use_client_core(core)
    c.crypto.encrypt("pii", [b"x"])
    assert len(c.mode_calls) == 2 and len(seen) == 1 and len(core.calls) == 1
    assert _json(seen[0])["custodyEpoch"] == "ep-1"


def test_epoch_race_to_a_server_mode_sends_the_new_epoch_and_a_second_409_raises():
    c, seen = _client(_zk409(), modes=[_mode("PLATFORM_MANAGED", "ep-1"), _mode("PLATFORM_MANAGED", "ep-2")])
    with pytest.raises(KError) as e:
        c.crypto.encrypt("pii", [b"x"])
    assert e.value.code == "K9O_ZK_STORE_FLOW_REQUIRED" and e.value.status == 409
    assert len(c.mode_calls) == 2 and [_json(r)["custodyEpoch"] for r in seen] == ["ep-1", "ep-2"]


def test_epoch_race_into_client_custody_without_a_core_raises_unavailable():
    c, seen = _client(_zk409(), modes=[_mode("PLATFORM_MANAGED", "ep-1"), _mode("HYOK", "ep-2")])
    with pytest.raises(KError) as e:
        c.crypto.encrypt("pii", [b"x"])
    assert e.value.code == "K9O_ZK_CLIENT_CORE_UNAVAILABLE" and len(seen) == 1


@pytest.mark.parametrize("modes", [
    _ok({"custodyMode": "WHATEVER", "epoch": "e"}),
    _ok({"epoch": "e"}),
    _ok({"custodyMode": "PLATFORM_MANAGED"}),
    _ok({}),
    _err(404, "K9O_NOT_FOUND"),
    _err(503, "K9O_KEY_SERVICE_UNAVAILABLE"),
    lambda req: (_ for _ in ()).throw(httpx.ConnectError("down")),
])
def test_unknown_mode_or_failed_check_fails_closed_with_nothing_sent(modes):
    core = FakeCore()
    c, seen = _client(_ok({"items": [{"ciphertext": "AAA="}]}), modes=modes)
    c.crypto.use_client_core(core)
    with pytest.raises(KError):
        c.crypto.encrypt("pii", [b"secret"])
    assert seen == [] and core.calls == []


@pytest.mark.parametrize("verb", ["decrypt", "rewrap"])
def test_ciphertext_routing_200_never_touches_the_core(verb):
    core = FakeCore()
    c, seen = _client(_ok({"items": [{"plaintext": "eA==", "ciphertext": "N="}]}))
    c.crypto.use_client_core(core)
    getattr(c.crypto, verb)("pii", ["AAA="])
    assert core.calls == [] and len(seen) == 1 and c.mode_calls == []
    assert _json(seen[0]) == {"crypt": "pii", "items": [{"ciphertext": "AAA="}]}


@pytest.mark.parametrize("verb", ["decrypt", "rewrap"])
def test_ciphertext_routing_409_client_route_opens_with_the_core(verb):
    core = FakeCore()
    c, seen = _client(_zk409("client", "HYOK"))
    c.crypto.use_client_core(core)
    out = getattr(c.crypto, verb)("pii", ["AAA=", {"ciphertext": "BBB=", "context": "u1"}])
    assert len(seen) == 1 and c.mode_calls == [] and len(out.items) == 2
    assert core.calls == [(verb, "pii", [{"ciphertext": "AAA="}, {"ciphertext": "BBB=", "context": "u1"}])]


@pytest.mark.parametrize("verb", ["decrypt", "rewrap"])
def test_ciphertext_routing_409_client_route_without_a_core_raises_unavailable(verb):
    c, seen = _client(_zk409("client", "HYOK"))
    with pytest.raises(KError) as e:
        getattr(c.crypto, verb)("pii", ["AAA="])
    assert e.value.code == "K9O_ZK_CLIENT_CORE_UNAVAILABLE" and e.value.status == 501
    assert dict(e.value.context) == {"custodyMode": "HYOK", "requirement": "kibo-client-core"}


@pytest.mark.parametrize("verb", ["decrypt", "rewrap"])
@pytest.mark.parametrize("response,code", [
    (_zk409("server"), "K9O_ZK_STORE_FLOW_REQUIRED"),
    (_err(409, "K9O_SEALED_UNDER_RETIRED_KEY"), "K9O_SEALED_UNDER_RETIRED_KEY"),
    (_err(400, "K9O_ENVELOPE_INTEGRITY_FAILURE"), "K9O_ENVELOPE_INTEGRITY_FAILURE"),
])
def test_ciphertext_routing_other_errors_are_raised_unchanged(verb, response, code):
    core = FakeCore()
    c, seen = _client(response)
    c.crypto.use_client_core(core)
    with pytest.raises(KError) as e:
        getattr(c.crypto, verb)("pii", ["AAA="])
    assert e.value.code == code and core.calls == []


def test_use_client_core_checks_the_shape_and_none_removes_it():
    c, _ = _client()
    with pytest.raises(KError):
        c.crypto.use_client_core(object())
    c.crypto.use_client_core(FakeCore())
    c.crypto.use_client_core(None)


# expected_key_refs ---------------------------------------------------------------------------------------------------

REFS = ["arn:aws:kms:eu-central-1:111122223333:key/abc"]


class OptCore:
    """A core that takes the option, declares that it enforces it, and records what it received."""

    enforces_expected_key_refs = True

    def __init__(self):
        self.got: list[tuple[str, object]] = []

    def encrypt(self, crypt, items, *, expected_key_refs=None):
        self.got.append(("encrypt", expected_key_refs))
        return [CryptoItemResult(ciphertext="C=") for _ in items]

    def decrypt(self, crypt, items, *, expected_key_refs=None):
        self.got.append(("decrypt", expected_key_refs))
        return [CryptoItemResult(plaintext=b"p") for _ in items]

    def rewrap(self, crypt, items, *, expected_key_refs=None):
        self.got.append(("rewrap", expected_key_refs))
        return [CryptoItemResult(ciphertext="C=") for _ in items]


def test_expected_key_refs_are_passed_to_the_core_on_encrypt_decrypt_and_rewrap():
    core = OptCore()
    c, _ = _client(_zk409("client", "BYOK"), modes=_mode("BYOK"), expected_key_refs=REFS)
    c.crypto.use_client_core(core)
    c.crypto.encrypt("pii", [b"x"])
    c.crypto.decrypt("pii", ["AAA="])
    c.crypto.rewrap("pii", ["AAA="])
    assert core.got == [("encrypt", tuple(REFS)), ("decrypt", tuple(REFS)), ("rewrap", tuple(REFS))]


def test_no_option_is_passed_when_none_is_set_so_an_old_core_keeps_working():
    core = FakeCore()  # takes no option; a keyword would raise TypeError
    c, _ = _client(_zk409("client", "BYOK"), modes=_mode("BYOK"))
    c.crypto.use_client_core(core)
    c.crypto.encrypt("pii", [b"x"])
    c.crypto.decrypt("pii", ["AAA="])
    assert [call[0] for call in core.calls] == ["encrypt", "decrypt"]
    opt = OptCore()
    c.crypto.use_client_core(opt)
    c.crypto.encrypt("pii", [b"x"])
    assert opt.got == [("encrypt", None)]


@pytest.mark.parametrize("bad", [[], (), "arn:aws:kms:x", ["*"], ["arn:aws:kms:*:1:key/*"], [""], ["  "], ["has space"], [7], [None]])
def test_invalid_expected_key_refs_are_rejected_when_the_client_is_built(bad):
    with pytest.raises(KError) as e:
        create_client(base_url=BASE, project_slug="portal", api_key="k", expected_key_refs=bad)
    assert e.value.code == "K9O_INVALID_FIELD_VALUE"
    assert e.value.message.startswith("expectedKeyRefs: ") and e.value.context["field"] == "expectedKeyRefs"


def test_expected_key_refs_are_stored_as_an_immutable_copy():
    refs = list(REFS)
    c = create_client(base_url=BASE, project_slug="portal", api_key="k", expected_key_refs=refs)
    refs.append("arn:aws:kms:eu-central-1:111122223333:key/other")
    assert c.config.expected_key_refs == tuple(REFS)


@pytest.mark.parametrize("verb", ["encrypt", "decrypt", "rewrap"])
def test_a_core_that_does_not_declare_enforcement_is_refused_when_the_list_is_set(verb):
    core = FakeCore()
    c, seen = _client(_zk409("client", "BYOK"), modes=_mode("BYOK"), expected_key_refs=REFS)
    c.crypto.use_client_core(core)
    arg = [b"x"] if verb == "encrypt" else ["AAA="]
    with pytest.raises(KError) as e:
        getattr(c.crypto, verb)("pii", arg)
    assert e.value.code == KEY_PIN_UNENFORCED == "K9O_CRYPTO_KEY_PIN_UNENFORCED"
    assert e.value.status is None
    assert e.value.context == {"custodyMode": "BYOK", "requirement": "client-core-enforces-expected-key-refs"}
    assert "enforces_expected_key_refs" in e.value.recommendation
    assert core.calls == []


def test_enforcement_must_be_exactly_true_and_an_instance_attribute_counts():
    class Truthy(FakeCore):
        enforces_expected_key_refs = "yes"

    c, _ = _client(_zk409("client", "BYOK"), modes=_mode("BYOK"), expected_key_refs=REFS)
    c.crypto.use_client_core(Truthy())
    with pytest.raises(KError) as e:
        c.crypto.decrypt("pii", ["AAA="])
    assert e.value.code == KEY_PIN_UNENFORCED
    core = OptCore()
    core.enforces_expected_key_refs = True
    c.crypto.use_client_core(core)
    c.crypto.encrypt("pii", [b"x"])
    assert core.got == [("encrypt", tuple(REFS))]


def test_an_undeclared_core_works_as_before_when_no_list_is_set():
    core = FakeCore()
    c, _ = _client(_zk409("client", "BYOK"), modes=_mode("BYOK"))
    c.crypto.use_client_core(core)
    c.crypto.encrypt("pii", [b"x"])
    c.crypto.decrypt("pii", ["AAA="])
    c.crypto.rewrap("pii", ["AAA="])
    assert [call[0] for call in core.calls] == ["encrypt", "decrypt", "rewrap"]


def test_no_core_still_raises_unavailable_before_the_pin_check():
    c, _ = _client(modes=_mode("BYOK"), expected_key_refs=REFS)
    with pytest.raises(KError) as e:
        c.crypto.encrypt("pii", [b"x"])
    assert e.value.code == ZK_CLIENT_CORE_UNAVAILABLE


def test_key_ref_matches_azure_ignores_version_and_case():
    az = "azure"
    assert key_ref_matches("https://V.vault.azure.net/keys/Orders/0123456789abcdef0123456789abcdef", ["https://v.vault.azure.net/keys/orders"], az)
    assert key_ref_matches("https://v.vault.azure.net/keys/orders", ["https://V.vault.azure.net/keys/Orders/abc"], az)
    assert not key_ref_matches("https://v.vault.azure.net/keys/other/abc", ["https://v.vault.azure.net/keys/orders"], az)
    assert not key_ref_matches("https://w.vault.azure.net/keys/orders", ["https://v.vault.azure.net/keys/orders"], az)


def test_key_ref_matches_vault_ignores_the_colon_version():
    v = "vault"
    assert key_ref_matches("transit/keys/orders:3", ["transit/keys/orders"], v)
    assert key_ref_matches("transit/keys/orders", ["transit/keys/orders:7"], v)
    assert key_ref_matches("orders:2", ["orders"], v)
    assert not key_ref_matches("transit/keys/orders", ["other/keys/orders"], v)
    assert not key_ref_matches("transit/keys/other:1", ["transit/keys/orders"], v)


def test_key_ref_matches_gcp_and_aws():
    gcp = "projects/p/locations/l/keyRings/r/cryptoKeys/k"
    assert key_ref_matches(f"{gcp}/cryptoKeyVersions/7", [gcp], "gcp")
    assert key_ref_matches(gcp, [f"{gcp}/cryptoKeyVersions/2"], "gcp")
    assert not key_ref_matches(f"{gcp}x/cryptoKeyVersions/7", [gcp], "gcp")
    arn = "arn:aws:kms:eu-central-1:111122223333:key/abc"
    assert key_ref_matches(arn, [arn], "aws")
    assert not key_ref_matches("arn:aws:kms:eu-central-1:111122223333:key/other", [arn], "aws")
    assert not key_ref_matches(arn.upper(), [arn], "aws")


def test_key_ref_matches_provider_is_required_and_never_inferred():
    with pytest.raises(TypeError):
        key_ref_matches("transit/keys/orders", ["transit/keys/orders"])  # type: ignore[call-arg]
    with pytest.raises(KError) as e:
        key_ref_matches("a", ["a"], "gopher")  # type: ignore[arg-type]
    assert e.value.code == "K9O_INVALID_FIELD_VALUE"
    # the same colon-versioned reference is a different key by the whole-reference rule
    assert not key_ref_matches("arn:aws:kms:r:1:key/abc:2", ["arn:aws:kms:r:1:key/abc"], "aws")
    assert key_ref_matches("arn:aws:kms:r:1:key/abc:2", ["arn:aws:kms:r:1:key/abc"], "vault")
    assert key_ref_matches("a/b/c/d/e/f", ["a/b/c/d/e/g"], "azure")
    assert not key_ref_matches("a/b/c/d/e/f", ["a/b/c/d/e/g"], "aws")
    assert key_ref_matches("x/cryptoKeyVersions/1", ["x"], "gcp")


def test_url_looking_vault_reference_uses_the_vault_rule_only_when_named():
    ref = "https://vault.example.com:8200/transit/keys/orders:3"
    exp = ["https://vault.example.com:8200/transit/keys/orders"]
    assert key_ref_matches(ref, exp, "vault")
    # by the Azure rule only five parts count, so the key name is cut off and different keys collide
    assert key_ref_matches("https://vault.example.com:8200/transit/keys/other:1", exp, "azure")
    assert not key_ref_matches("https://vault.example.com:8200/transit/keys/other:1", exp, "vault")
    # by the AWS rule the whole reference counts, version included
    assert not key_ref_matches(ref, exp, "aws")


# keys.rotate step-up ------------------------------------------------------------------------------------------------

CHALLENGE = {"scopeSetRoot": "root_1", "nonce": "n_1", "minTier": "T3_KEY_POSSESSION", "methods": ["webauthn", "dpop"],
             "maxAgeSec": 120, "singleUse": True, "rpId": "acme.k9o.io"}
ROTATED = {"keyId": "vkey_1", "previousVersion": 1, "newVersion": 2, "status": "rotated"}


def _step_up_required():
    return _err(401, "K9O_STEP_UP_REQUIRED", {"stepUp": CHALLENGE})


def _decode_header(value: str) -> dict:
    assert "=" not in value and "+" not in value and "/" not in value
    return json.loads(base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)))


def test_rotate_answers_the_challenge_once_and_retries_with_the_header():
    c, seen = _client(_step_up_required(), _ok(ROTATED))
    calls = []

    def hook(challenge, method, url):
        calls.append((challenge, method, url))
        return {"method": "dpop", "proof": "eyJ.dpop.proof"}

    out = c.keys.rotate("vkey_1", step_up=hook)
    assert out.data == ROTATED and len(seen) == 2
    ch, method, url = calls[0]
    assert isinstance(ch, StepUpChallenge)
    assert (ch.scope_set_root, ch.nonce, ch.methods, ch.max_age_sec, ch.rp_id) == ("root_1", "n_1", ("webauthn", "dpop"), 120, "acme.k9o.io")
    assert (method, url) == ("POST", f"{BASE}{P}/vault/keys/vkey_1/rotate")
    assert "x-k9o-step-up-proof" not in seen[0].headers
    assert _decode_header(seen[1].headers["x-k9o-step-up-proof"]) == {
        "scopeSetRoot": "root_1", "nonce": "n_1", "method": "dpop", "proof": "eyJ.dpop.proof"}
    assert (seen[1].method, seen[1].url.path) == ("POST", f"{P}/vault/keys/vkey_1/rotate")


def test_rotate_hook_may_return_a_step_up_proof_object_and_its_repr_hides_the_proof():
    c, seen = _client(_step_up_required(), _ok(ROTATED))
    proof = StepUpProof("webauthn", "SECRET-PROOF")
    assert "SECRET-PROOF" not in repr(proof)
    c.keys.rotate("vkey_1", step_up=lambda ch, m, u: proof)
    assert _decode_header(seen[1].headers["x-k9o-step-up-proof"])["method"] == "webauthn"


def test_rotate_without_a_hook_raises_the_typed_error_with_the_challenge():
    c, seen = _client(_step_up_required())
    with pytest.raises(StepUpRequired) as e:
        c.keys.rotate("vkey_1")
    assert isinstance(e.value, KError) and (e.value.code, e.value.status) == ("K9O_STEP_UP_REQUIRED", 401)
    assert e.value.step_up.nonce == "n_1" and e.value.step_up.methods == ("webauthn", "dpop")
    assert e.value.context["stepUp"]["scopeSetRoot"] == "root_1"
    assert len(seen) == 1


def test_rotate_hook_that_throws_ends_the_call_without_a_second_request():
    c, seen = _client(_step_up_required(), _ok(ROTATED))

    def hook(ch, m, u):
        raise RuntimeError("no passkey")

    with pytest.raises(RuntimeError, match="no passkey"):
        c.keys.rotate("vkey_1", step_up=hook)
    assert len(seen) == 1


def test_rotate_second_401_is_not_retried():
    c, seen = _client(_step_up_required(), _err(401, "K9O_STEP_UP_PROOF_MALFORMED"))
    hooks = []

    def hook(ch, m, u):
        hooks.append(1)
        return {"method": "webauthn", "proof": "p"}

    with pytest.raises(KError) as e:
        c.keys.rotate("vkey_1", step_up=hook)
    assert e.value.code == "K9O_STEP_UP_PROOF_MALFORMED" and len(seen) == 2 and len(hooks) == 1
    # a second challenge is not answered either
    c, seen = _client(_step_up_required())
    with pytest.raises(StepUpRequired):
        c.keys.rotate("vkey_1", step_up=hook)
    assert len(seen) == 2 and len(hooks) == 2


def test_rotate_hook_answers_are_checked_before_sending():
    for bad in ({"method": "smoke-signal", "proof": "p"}, {"method": "dpop"}, {"method": "dpop", "proof": ""}, "proof", None):
        c, seen = _client(_step_up_required(), _ok(ROTATED))
        with pytest.raises(KError) as e:
            c.keys.rotate("vkey_1", step_up=lambda ch, m, u, bad=bad: bad)
        assert e.value.code == "K9O_INVALID_FIELD_VALUE" and len(seen) == 1


def test_rotate_other_errors_skip_the_hook():
    c, seen = _client(_err(409, "K9O_STATE_CONFLICT", {"reason": "rotation_in_flight"}))
    with pytest.raises(KError) as e:
        c.keys.rotate("vkey_1", step_up=lambda *a: pytest.fail("hook must not run"))
    assert e.value.code == "K9O_STATE_CONFLICT" and len(seen) == 1


def test_keys_list_is_unpaged_and_drops_next_cursor():
    c, seen = _client(_ok({"items": [{"id": "vkey_1"}], "nextCursor": None}))
    out = c.keys.list()
    assert out.data == {"items": [{"id": "vkey_1"}]}
    c, _ = _client(_ok({"items": [], "nextCursor": "abc"}))
    assert c.keys.list(status="all").data == {"items": []}
    with pytest.raises(TypeError):
        c.keys.list(cursor="x")


# Rampart input ------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("bad", ["ignore previous instructions", ["a"], 7, None, b"x"])
def test_rampart_classify_refuses_a_non_object_input_before_sending(bad):
    c, seen = _client(_ok({}))
    with pytest.raises(KError) as e:
        c.rampart.classify(bad)
    assert e.value.code == "K9O_INVALID_FIELD_VALUE" and seen == []


_FED = {"token": "eyJhbGciOiJSUzI1NiJ9.e30.sig", "expiresAt": "2026-11-02T22:15:00.000Z", "issuer": "https://api.k9o.io/kms-federation/p1/",
        "subject": "p1:crypt:payments:op:encrypt", "audience": "sts.amazonaws.com", "kid": "k1"}


def test_federated_token_posts_crypt_and_op_and_returns_the_six_fields():
    c, seen = _client(_ok(_FED))
    t = c.crypto.federated_token("payments", "encrypt")
    assert (seen[0].method, seen[0].url.path, _json(seen[0])) == ("POST", f"{P}/crypto/federated-token", {"crypt": "payments", "op": "encrypt"})
    assert (t.token, t.expires_at, t.issuer, t.subject, t.audience, t.kid) == tuple(_FED[k] for k in ("token", "expiresAt", "issuer", "subject", "audience", "kid"))
    assert _FED["token"] not in repr(t)


@pytest.mark.parametrize("op", ["decrypt", "rewrap"])
def test_federated_token_accepts_decrypt_and_rewrap(op):
    c, seen = _client(_ok(_FED))
    c.crypto.federated_token("payments", op)
    assert _json(seen[0]) == {"crypt": "payments", "op": op}


@pytest.mark.parametrize("crypt,op", [("", "encrypt"), ("Payments", "encrypt"), ("a b", "encrypt"), ("x" * 65, "encrypt"), ("payments", "sign"), ("payments", None)])
def test_federated_token_refuses_a_bad_crypt_or_op_before_sending(crypt, op):
    c, seen = _client(_ok(_FED))
    with pytest.raises(KError) as e:
        c.crypto.federated_token(crypt, op)
    assert e.value.code == "K9O_INVALID_FIELD_VALUE" and seen == []


@pytest.mark.parametrize("status,code", [(409, "K9O_CUSTODY_SERVER_ROUTE"), (429, "K9O_RATE_LIMITED"), (503, "K9O_KEY_SERVICE_UNAVAILABLE")])
def test_federated_token_passes_errors_through_and_is_not_retried(status, code):
    c, seen = _client(_err(status, code))
    with pytest.raises(KError) as e:
        c.crypto.federated_token("payments", "encrypt")
    assert (e.value.status, e.value.code, len(seen)) == (status, code, 1)


@pytest.mark.parametrize("missing", list(_FED))
def test_federated_token_missing_field_is_an_internal_error_without_the_token(missing):
    c, _ = _client(_ok({k: v for k, v in _FED.items() if k != missing}))
    with pytest.raises(KError) as e:
        c.crypto.federated_token("payments", "encrypt")
    assert e.value.code == "K9O_INTERNAL_ERROR" and missing in e.value.message and _FED["token"] not in e.value.message


# route field of the mode answer ---------------------------------------------------------------------------------------

def test_byok_without_a_route_uses_the_client_core_only():
    core = FakeCore()
    c, seen = _client(_ok({"items": []}), modes=_mode("BYOK"))
    c.crypto.use_client_core(core)
    c.crypto.encrypt("pii", [b"x"])
    assert seen == [] and len(core.calls) == 1


@pytest.mark.parametrize("mode", ["BYOK", "HYOK"])
def test_client_custody_with_route_server_sends_the_server_encrypt_not_the_core(mode):
    core = FakeCore()
    c, seen = _client(_ok({"items": [{"ciphertext": "AAA="}]}), modes=_mode(mode, "ep-7", route="server"))
    c.crypto.use_client_core(core)
    c.crypto.encrypt("pii", [b"x"])
    assert core.calls == [] and len(seen) == 1
    assert _json(seen[0]) == {"crypt": "pii", "custodyEpoch": "ep-7", "items": [{"plaintext": "eA=="}]}


def test_client_custody_with_route_client_uses_the_core():
    core = FakeCore()
    c, seen = _client(_ok({"items": []}), modes=_mode("BYOK", route="client"))
    c.crypto.use_client_core(core)
    c.crypto.encrypt("pii", [b"x"])
    assert seen == [] and len(core.calls) == 1


@pytest.mark.parametrize("mode", ["BYOK", "PLATFORM_MANAGED"])
@pytest.mark.parametrize("route", ["bogus", 5, 123, None, ["server"], ""])
def test_unknown_route_refuses_and_sends_nothing(mode, route):
    core = FakeCore()
    c, seen = _client(_ok({"items": [{"ciphertext": "AAA="}]}), modes=_mode(mode, route=route))
    c.crypto.use_client_core(core)
    with pytest.raises(KError) as e:
        c.crypto.encrypt("pii", [b"secret"])
    assert e.value.code == "K9O_CRYPTO_CUSTODY_UNKNOWN"
    assert dict(e.value.context) == ({"custodyMode": mode, "route": route} if isinstance(route, str) else {"custodyMode": mode})
    assert seen == [] and core.calls == []


@pytest.mark.parametrize("epoch", ["", None])
def test_byok_route_server_without_an_epoch_refuses(epoch):
    core = FakeCore()
    c, seen = _client(_ok({"items": [{"ciphertext": "AAA="}]}), modes=_mode("BYOK", epoch, route="server"))
    c.crypto.use_client_core(core)
    with pytest.raises(KError) as e:
        c.crypto.encrypt("pii", [b"secret"])
    assert e.value.code == "K9O_CRYPTO_CUSTODY_UNKNOWN" and seen == [] and core.calls == []


def test_platform_managed_with_route_server_is_unchanged():
    c, seen = _client(_ok({"items": [{"ciphertext": "AAA="}]}), modes=_mode("PLATFORM_MANAGED", route="server"))
    c.crypto.encrypt("pii", [b"x"])
    assert len(seen) == 1 and _json(seen[0])["custodyEpoch"] == "ep-1"


def test_byok_two_encrypts_make_two_mode_requests():
    core = FakeCore()
    c, _ = _client(_ok({"items": []}), modes=[_mode("BYOK"), _mode("BYOK", route="client")])
    c.crypto.use_client_core(core)
    c.crypto.encrypt("pii", [b"x"])
    c.crypto.encrypt("pii", [b"x"])
    assert len(c.mode_calls) == 2 and len(core.calls) == 2


def test_route_change_between_two_encrypts_is_followed_not_cached():
    core = FakeCore()
    c, seen = _client(_ok({"items": [{"ciphertext": "AAA="}]}), modes=[_mode("BYOK", route="client"), _mode("BYOK", "ep-9", route="server")])
    c.crypto.use_client_core(core)
    c.crypto.encrypt("pii", [b"x"])
    assert len(core.calls) == 1 and seen == []
    c.crypto.encrypt("pii", [b"x"])
    assert len(core.calls) == 1 and len(seen) == 1 and _json(seen[0])["custodyEpoch"] == "ep-9"
    assert len(c.mode_calls) == 2
