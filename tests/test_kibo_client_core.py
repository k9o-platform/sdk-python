"""Tests for create_kibo_client_core with a fake bound core (the generated binding is not needed)."""

from __future__ import annotations

import base64
import sys
import threading
import types
from dataclasses import dataclass

import pytest

# `k9o.kibo` imports the generated native binding at package import. Where it is not built, stand in an empty module so
# the client core (which never touches it with an injected core) can be imported.
try:
    import k9o.kibo._ffi.kibo_kms_rest_ffi  # noqa: F401
except ImportError:
    stub = types.ModuleType("k9o.kibo._ffi.kibo_kms_rest_ffi")
    stub.__getattr__ = lambda name: type(name, (), {})  # type: ignore[attr-defined]
    sys.modules["k9o.kibo._ffi.kibo_kms_rest_ffi"] = stub

from k9o.errors import KError
from k9o.kibo import create_kibo_client_core

KEY = "arn:aws:kms:eu-central-1:111122223333:key/mine"


@dataclass(frozen=True)
class Tok:
    token: str = "jwt-secret"
    subject: str = "proj_1:crypt:c:op:encrypt"
    provider: str | None = "aws"
    project_id: str | None = "proj_1"


class FakeCore:
    def __init__(self, log, caps=("expected_key_refs",), exchange=None, seal=None):
        self.log = log
        self._caps = list(caps)
        self._exchange = exchange
        self._seal = seal

    def core_capabilities(self):
        return self._caps

    def acquire_kms_credential(self, inp):
        self.log.append(f"exchange:{inp['identity_token']}")
        if self._exchange:
            return self._exchange(inp)
        return {"secret": "cred-1", "expires_at_unix_secs": 10_000}

    def app_crypto_seal(self, inp):
        self.log.append(f"seal:{inp['crypt']}:{inp['project_id']}:{','.join(inp['expected_key_refs'])}")
        if self._seal:
            return self._seal(inp)
        return {"container": bytes([1, 2, 3, *inp["plaintext"]])}

    def app_crypto_open(self, inp):
        self.log.append(f"open:{inp['crypt']}:{','.join(inp['expected_key_refs'])}")
        if inp["container"][0] != 1:
            raise RuntimeError("K9O_CRYPTO_UNEXPECTED_KEY: no")
        return {"plaintext": bytes(inp["container"][3:])}


def build(log, **over):
    def token(crypt, op):
        log.append(f"token:{crypt}:{op}")
        return Tok()

    cfg = dict(
        provider="aws", posture="byok", project_id="proj_1",
        key_ref={"version": 1, "external_ref": KEY},
        federation={"provider": "aws", "role_arn": "arn:aws:iam::1:role/r", "session_name": "s",
                    "region": "eu-central-1", "key_arn": KEY, "duration_secs": 900},
        expected_key_refs=[KEY], federated_token=token, core=FakeCore(log), now=lambda: 1000)
    cfg.update(over)
    return create_kibo_client_core(**cfg)


ONE = [{"plaintext": b"\x01"}]


def test_encrypt_token_then_exchange_then_seal_round_trips_through_decrypt():
    log: list[str] = []
    core = build(log)
    enc = core.encrypt("c", [{"plaintext": b"hi"}])
    assert log == ["token:c:encrypt", "exchange:jwt-secret", f"seal:c:proj_1:{KEY}"]
    ct = enc.items[0].ciphertext
    assert base64.b64decode(ct) == bytes([1, 2, 3]) + b"hi"
    assert core.decrypt("c", [{"ciphertext": ct}]).items[0].text() == "hi"


def test_declares_it_enforces_the_pin():
    assert build([]).enforces_expected_key_refs is True


def test_refuses_to_be_built_without_a_pin_with_an_empty_pin_or_with_a_key_outside_the_pin():
    with pytest.raises(KError):
        build([], expected_key_refs=None)
    with pytest.raises(KError):
        build([], expected_key_refs=[])
    with pytest.raises(KError, match="key_ref"):
        build([], expected_key_refs=["arn:aws:kms:eu-central-1:111122223333:key/other"])


def test_empty_pin_from_the_router_reaches_no_network_and_no_kms():
    log: list[str] = []
    core = build(log)
    with pytest.raises(KError, match="key pin"):
        core.decrypt("c", [{"ciphertext": "AQID"}], expected_key_refs=())
    assert log == []


def test_core_that_does_not_list_expected_key_refs_is_refused_before_any_token_is_fetched():
    log: list[str] = []
    core = build(log, core=FakeCore(log, caps=()))
    with pytest.raises(KError) as e:
        core.encrypt("c", ONE)
    assert e.value.code == "K9O_CRYPTO_KEY_PIN_UNENFORCED"
    assert log == []


def test_older_core_without_the_new_exports_is_refused():
    class Old:
        def core_capabilities(self):
            return ["expected_key_refs"]

    core = build([], core=Old())
    with pytest.raises(KError) as e:
        core.encrypt("c", ONE)
    assert e.value.code == "KIBO_WASM_ERROR"


@pytest.mark.parametrize("over", [{"provider": "gcp"}, {"project_id": "proj_other"}])
def test_response_naming_another_provider_or_project_is_refused_and_nothing_is_exchanged(over):
    log: list[str] = []
    core = build(log, federated_token=lambda crypt, op: Tok(**over))
    with pytest.raises(KError) as e:
        core.encrypt("c", ONE)
    assert e.value.code == "K9O_INVALID_FIELD_VALUE"
    assert log == []


def test_project_id_comes_from_the_response_only_when_not_configured():
    log: list[str] = []
    core = build(log, project_id=None)
    core.encrypt("c", ONE)
    assert log[-1] == f"seal:c:proj_1:{KEY}"
    none = build([], project_id=None, federated_token=lambda c, o: Tok(project_id=None))
    with pytest.raises(KError) as e:
        none.encrypt("c", ONE)
    assert e.value.code == "K9O_INVALID_FIELD_VALUE"


def test_exchanged_credential_is_reused_until_near_expiry_then_exchanged_again():
    log: list[str] = []
    t = [1000]
    core = build(log, now=lambda: t[0])
    core.encrypt("c", ONE)
    core.encrypt("c", ONE)
    assert len([x for x in log if x.startswith("exchange")]) == 1
    assert len([x for x in log if x.startswith("token")]) == 2  # a token every call
    t[0] = 9_950  # inside the 60 s skew of 10_000
    core.encrypt("c", ONE)
    assert len([x for x in log if x.startswith("exchange")]) == 2


def test_stalled_exchange_is_abandoned_at_the_deadline():
    log: list[str] = []
    release = threading.Event()
    stalled = FakeCore(log, exchange=lambda inp: release.wait(10))
    core = build(log, core=stalled, exchange_deadline_secs=0.05)
    try:
        with pytest.raises(KError) as e:
            core.encrypt("c", ONE)
        assert e.value.code == "KIBO_EXCHANGE_TIMEOUT"
    finally:
        release.set()


def test_one_bad_item_fails_on_its_own_and_carries_the_cores_code():
    log: list[str] = []
    core = build(log)
    good = core.encrypt("c", [{"plaintext": b"\x09"}]).items[0]
    r = core.decrypt("c", [{"ciphertext": "AAAA"}, {"ciphertext": good.ciphertext}])
    assert (r.items[0].ok, r.items[0].code) == (False, "K9O_CRYPTO_UNEXPECTED_KEY")
    assert r.items[1].ok


def test_rewrap_opens_and_seals_under_the_configured_key_with_the_rewrap_token():
    log: list[str] = []
    core = build(log)
    ct = core.encrypt("c", [{"plaintext": b"\x07"}]).items[0].ciphertext
    log.clear()
    r = core.rewrap("c", [{"ciphertext": ct}])
    assert r.items[0].ok
    assert log[0] == "token:c:rewrap"
    assert [x.split(":")[0] for x in log[-2:]] == ["open", "seal"]


def test_decrypt_uses_the_decrypt_token():
    log: list[str] = []
    core = build(log)
    core.decrypt("c", [{"ciphertext": "AQID"}])
    assert log[0] == "token:c:decrypt"


def test_upstream_kms_error_is_per_item_and_neither_token_nor_credential_appears_in_it():
    log: list[str] = []

    def boom(inp):
        raise RuntimeError("UPSTREAM_KMS_ERROR: denied")

    core = build(log, core=FakeCore(log, seal=boom))
    r = core.encrypt("c", ONE)
    assert (r.items[0].ok, r.items[0].code, r.items[0].message) == (False, "UPSTREAM_KMS_ERROR", "denied")
    s = repr(r) + repr(core)
    assert "jwt-secret" not in s and "cred-1" not in s


def test_per_call_pin_excluding_the_key_refuses_before_token_and_exchange():
    log: list[str] = []
    core = build(log)
    other = ["arn:aws:kms:eu-central-1:111122223333:key/other"]
    for r in (core.encrypt("c", ONE, expected_key_refs=other),
              core.decrypt("c", [{"ciphertext": "AQID"}], expected_key_refs=other),
              core.rewrap("c", [{"ciphertext": "AQID"}], expected_key_refs=other)):
        assert [(i.ok, i.code) for i in r.items] == [(False, "K9O_CRYPTO_UNEXPECTED_KEY")]
    assert log == []
    r = core.encrypt("c", ONE, expected_key_refs=[KEY])
    assert r.items[0].ok
    assert [e.split(":")[0] for e in log] == ["token", "exchange", "seal"]


def test_vault_federation_jwt_auth_mount_reaches_the_binding_as_mount() -> None:
    from types import SimpleNamespace

    from k9o.kibo.client_core import _BundledCore

    seen: dict = {}

    class _Vault:
        def __init__(self, **kw):
            seen["vault"] = kw

    class _Input:
        def __init__(self, **kw):
            seen["input"] = kw

    ffi = SimpleNamespace(
        FfiFederationSpec=SimpleNamespace(AWS=None, GCP=None, AZURE=None, VAULT=_Vault),
        FfiFederationInput=_Input,
        acquire_kms_credential_blocking=lambda i: SimpleNamespace(secret="s", expires_at_unix_secs=5),
        ZkFfiUniffiError=RuntimeError,
    )
    core = _BundledCore.__new__(_BundledCore)
    core._ffi = ffi
    core._to_kerror = lambda e: e
    out = core.acquire_kms_credential({
        "spec": {"provider": "vault", "vault_addr": "https://v", "jwt_auth_mount": "jwt", "role": "r"},
        "identity_token": "t", "now_unix_secs": 1})
    assert seen["vault"] == {"vault_addr": "https://v", "mount": "jwt", "role": "r"}
    assert out["expires_at_unix_secs"] == 5
