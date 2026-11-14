"""The client core for ``client.crypto`` on a BYOK or HYOK project: opens and seals data on this machine, against your own
KMS or HSM, so the platform never sees plaintext or the key. ZERO crypto here: sealing, opening, the key pin and the token
exchange all run in the audited Rust core (``kibo-kms-rest-ffi``). This module only sequences them::

    federated token (platform) -> acquire_kms_credential (your cloud) -> app_crypto_seal / app_crypto_open (your KMS)

Everything that picks a key, a provider, a role, a region or an endpoint comes from the configuration you pass here. Nothing
comes from the platform's answer: the token's ``provider`` and ``project_id`` (when the gateway sends them) are checked
against your configuration, and a mismatch is refused, never followed.

The crypto router calls a client core synchronously, so this core is synchronous too and drives the binding's ``*_blocking``
functions.
"""

from __future__ import annotations

import base64
import binascii
import threading
import time
from typing import Any, Callable, Mapping, Sequence

from ..crypto import (
    KEY_PIN_UNENFORCED,
    CryptoBatch,
    CryptoItemResult,
    check_expected_key_refs,
    key_ref_matches,
)
from ..errors import KError

EXCHANGE_TIMEOUT = "KIBO_EXCHANGE_TIMEOUT"

_PROVIDER_WIRE = {"aws": "aws-kms", "gcp": "gcp-kms", "azure": "azure-kv", "vault": "hashicorp-vault"}
_REQUIRED_EXPORTS = ("app_crypto_seal", "app_crypto_open", "acquire_kms_credential", "core_capabilities")
# Refresh a credential this long before it expires.
_SKEW_SECS = 60


def _bad(message: str, **context: Any) -> KError:
    return KError(code="K9O_INVALID_FIELD_VALUE", message=message, context=context)


def _map_core_error(e: BaseException) -> KError:
    """The core's error text is ``"<CODE>: <message>"``; ``UPSTREAM_KMS_ERROR`` is your own KMS failing."""
    if isinstance(e, KError):
        return e
    raw = str(e)
    head, sep, rest = raw.partition(":")
    if sep and head and head[0].isupper() and all(c.isupper() or c.isdigit() or c == "_" for c in head):
        return KError(code=head, message=rest.strip())
    return KError(code="KIBO_WASM_ERROR", message=raw)


def _to_bytes(v: Any) -> bytes:
    if isinstance(v, (bytes, bytearray)):
        return bytes(v)
    if isinstance(v, (list, tuple)) and all(isinstance(i, int) and 0 <= i < 256 for i in v):
        return bytes(v)
    raise KError(code="KIBO_WASM_DECODE_ERROR", message="the core returned a non-byte value where bytes were expected")


def _field(obj: Any, *names: str) -> Any:
    for n in names:
        v = obj.get(n) if isinstance(obj, Mapping) else getattr(obj, n, None)
        if v is not None:
            return v
    return None


def _with_deadline(fn: Callable[[], Any], secs: float) -> Any:
    box: dict[str, Any] = {}

    def run() -> None:
        try:
            box["value"] = fn()
        except BaseException as e:  # noqa: BLE001 - handed back to the caller
            box["error"] = e

    t = threading.Thread(target=run, daemon=True, name="k9o-kibo-exchange")
    t.start()
    t.join(secs)
    if t.is_alive():
        raise KError(
            code=EXCHANGE_TIMEOUT,
            message=f"the token exchange with your cloud did not finish within {secs:g} s",
            context={"deadlineSecs": secs},
            recommendation="Check that your cloud's token endpoint is reachable from this host.")
    if "error" in box:
        raise box["error"]
    return box["value"]


class _BundledCore:
    """The generated uniffi binding, loaded on first use. Takes and returns the wire shapes (dicts, bytes) and calls the
    ``*_blocking`` exports of ``ffi_uniffi.rs``; uniffi errors become ``KError`` through ``k9o.kibo.crypto``."""

    def __init__(self) -> None:
        from ._ffi import kibo_kms_rest_ffi as ffi  # type: ignore[import-not-found]
        from . import crypto as kibo_crypto

        self._ffi = ffi
        self._to_kerror = kibo_crypto._to_kerror

    def _call(self, name: str, make_input: Callable[[], Any], to_out: Callable[[Any], Any]) -> Any:
        try:
            return to_out(getattr(self._ffi, name)(make_input()))
        except self._ffi.ZkFfiUniffiError as e:  # type: ignore[attr-defined]
            raise self._to_kerror(e) from e

    def _cred(self, c: Mapping[str, Any]) -> Any:
        return self._ffi.FfiCredential(secret=c["secret"], expires_at_unix_secs=c["expires_at_unix_secs"])

    def _pc(self, c: Mapping[str, Any]) -> Any:
        return self._ffi.FfiProviderConfig(
            aws_region=c.get("aws_region"), vault_address=c.get("vault_address"))

    def core_capabilities(self) -> list[str]:
        return list(self._ffi.core_capabilities())

    def app_crypto_seal(self, i: Mapping[str, Any]) -> dict[str, Any]:
        f = self._ffi
        provider = {"aws-kms": f.KmsProvider.AWS_KMS, "gcp-kms": f.KmsProvider.GCP_KMS,
                    "azure-kv": f.KmsProvider.AZURE_KV, "hashicorp-vault": f.KmsProvider.HASHICORP_VAULT}[i["provider"]]
        posture = {"byok": f.FfiPosture.BYOK, "hyok": f.FfiPosture.HYOK}[i["posture"]]
        return self._call(
            "app_crypto_seal_blocking",
            lambda: f.AppSealInput(
                provider=provider, posture=posture, kms_cred=self._cred(i["kms_cred"]),
                key_ref=f.FfiKeyRef(version=i["key_ref"]["version"], external_ref=i["key_ref"]["external_ref"]),
                provider_config=self._pc(i["provider_config"]), project_id=i["project_id"], crypt=i["crypt"],
                context=i["context"], plaintext=bytes(i["plaintext"]), expected_key_refs=list(i["expected_key_refs"])),
            lambda o: {"container": bytes(o.container)})

    def app_crypto_open(self, i: Mapping[str, Any]) -> dict[str, Any]:
        f = self._ffi
        return self._call(
            "app_crypto_open_blocking",
            lambda: f.AppOpenInput(
                kms_cred=self._cred(i["kms_cred"]), provider_config=self._pc(i["provider_config"]),
                project_id=i["project_id"], crypt=i["crypt"], context=i["context"],
                container=bytes(i["container"]), expected_key_refs=list(i["expected_key_refs"])),
            lambda o: {"plaintext": bytes(o.plaintext)})

    def acquire_kms_credential(self, i: Mapping[str, Any]) -> dict[str, Any]:
        f = self._ffi
        s = dict(i["spec"])
        kind = s.pop("provider")
        if kind == "vault":
            s["mount"] = s.pop("jwt_auth_mount")  # the generated binding keeps the core's field name
        variant = {"aws": f.FfiFederationSpec.AWS, "gcp": f.FfiFederationSpec.GCP,
                   "azure": f.FfiFederationSpec.AZURE, "vault": f.FfiFederationSpec.VAULT}[kind]
        return self._call(
            "acquire_kms_credential_blocking",
            lambda: f.FfiFederationInput(spec=variant(**s), identity_token=i["identity_token"], now_unix_secs=i["now_unix_secs"]),
            lambda o: {"secret": o.secret, "expires_at_unix_secs": o.expires_at_unix_secs})


class _KiboClientCore:
    enforces_expected_key_refs = True

    def __init__(self, *, provider: str, posture: str, project_id: str | None, key_ref: Mapping[str, Any],
                 provider_config: Mapping[str, Any], federation: Mapping[str, Any], pin: tuple[str, ...],
                 federated_token: Callable[[str, str], Any], core: Any, deadline_secs: float,
                 now: Callable[[], int]) -> None:
        self._provider = provider
        self._posture = posture
        self._project_id = project_id
        self._key_ref = {"version": key_ref["version"], "external_ref": key_ref["external_ref"]}
        self._provider_config = dict(provider_config)
        self._federation = dict(federation)
        self._pin = pin
        self._federated_token = federated_token
        self._core = core
        self._deadline = deadline_secs
        self._now = now
        self._creds: dict[tuple[str, str, str], dict[str, Any]] = {}
        self._caps_checked = False
        self._lock = threading.Lock()

    def __repr__(self) -> str:  # nothing here is secret, but keep repr free of config and cached credentials anyway
        return f"<KiboClientCore provider={self._provider} posture={self._posture}>"

    # -- call-level checks -------------------------------------------------------------------------------------------

    def _effective_pin(self, expected_key_refs: Sequence[str] | None) -> tuple[str, ...]:
        p = self._pin if expected_key_refs is None else tuple(expected_key_refs)
        if len(p) == 0:
            raise _bad("expected_key_refs is empty: refusing to call without a key pin", field="expectedKeyRefs")
        return p

    def _refuse_if_key_not_pinned(self, pin: tuple[str, ...], n: int) -> CryptoBatch | None:
        """Match the configured key against this call's pin before any token is fetched or credential exchanged."""
        if key_ref_matches(self._key_ref["external_ref"], pin, self._provider):  # type: ignore[arg-type]
            return None
        item = CryptoItemResult(
            code="K9O_CRYPTO_UNEXPECTED_KEY",
            message="the configured key is not in this call's expected_key_refs; nothing was sent")
        return CryptoBatch(items=[item for _ in range(n)])

    def _bound(self) -> Any:
        w = self._core if self._core is not None else _BundledCore()
        if self._core is None:
            self._core = w
        for fn in _REQUIRED_EXPORTS:
            if not callable(getattr(w, fn, None)):
                raise KError(
                    code="KIBO_WASM_ERROR",
                    message=f"the Kibo core does not export {fn}; it is older than zk-bundle-v2.16.3",
                    context={"missing": fn},
                    recommendation="Use a build of k9o that ships zk-bundle-v2.16.3 or later.")
        if not self._caps_checked:
            if "expected_key_refs" not in list(w.core_capabilities()):
                raise KError(
                    code=KEY_PIN_UNENFORCED,
                    message="the Kibo core does not declare that it enforces expected_key_refs; nothing was sent",
                    recommendation="Use a build of k9o that ships zk-bundle-v2.16.3 or later.")
            self._caps_checked = True
        return w

    def _credential(self, w: Any, crypt: str, op: str) -> tuple[dict[str, Any], str]:
        t = self._federated_token(crypt, op)
        named_provider = _field(t, "provider")
        if named_provider is not None and named_provider != self._provider:
            raise _bad(f'the platform named provider "{named_provider}"; this client is configured for "{self._provider}"',
                       field="provider")
        named = _field(t, "project_id", "projectId")
        if named is not None and self._project_id is not None and named != self._project_id:
            raise _bad("the platform named a different project than the one configured", field="projectId")
        project_id = self._project_id if self._project_id is not None else (named if isinstance(named, str) else None)
        if project_id is None:
            raise _bad("project_id is not configured and the platform did not name one", field="projectId")

        subject, token = _field(t, "subject"), _field(t, "token")
        key = (crypt, op, str(subject))
        with self._lock:
            cached = self._creds.get(key)
        # A token is fetched on every call (it is bound to the call's crypt and op); the credential from the exchange is
        # reused until it is close to expiring.
        if cached is not None and cached["expires_at_unix_secs"] - _SKEW_SECS > self._now():
            return cached, project_id
        now = self._now()
        try:
            out = _with_deadline(
                lambda: w.acquire_kms_credential(
                    {"spec": self._federation, "identity_token": token, "now_unix_secs": now}),
                self._deadline)
        except KError as e:
            raise e
        except Exception as e:  # noqa: BLE001
            raise _map_core_error(e) from None
        secret = _field(out, "secret")
        expires = _field(out, "expires_at_unix_secs")
        if not isinstance(secret, str) or isinstance(expires, bool) or not isinstance(expires, int):
            raise KError(code="KIBO_WASM_DECODE_ERROR", message="the core returned a credential without secret or expiry")
        cred = {"secret": secret, "expires_at_unix_secs": expires}
        with self._lock:
            self._creds[key] = cred
        return cred, project_id

    # -- one item ----------------------------------------------------------------------------------------------------

    def _common(self, cred: dict[str, Any], project_id: str, crypt: str, context: str | None, pin: tuple[str, ...]) -> dict[str, Any]:
        pc = self._provider_config
        return {
            "kms_cred": cred,
            "provider_config": {
                "aws_region": pc.get("aws_region"),
                "vault_address": pc.get("vault_address"),
            },
            "project_id": project_id,
            "crypt": crypt,
            "context": context,
            "expected_key_refs": list(pin),
        }

    def _seal(self, w: Any, cred: dict[str, Any], project_id: str, crypt: str, plaintext: bytes, context: str | None,
              pin: tuple[str, ...]) -> str:
        out = w.app_crypto_seal({
            "provider": _PROVIDER_WIRE[self._provider], "posture": self._posture, "key_ref": self._key_ref,
            "plaintext": list(plaintext), **self._common(cred, project_id, crypt, context, pin)})
        return base64.b64encode(_to_bytes(_field(out, "container"))).decode("ascii")

    def _open(self, w: Any, cred: dict[str, Any], project_id: str, crypt: str, ciphertext: str, context: str | None,
              pin: tuple[str, ...]) -> bytes:
        try:
            container = base64.b64decode(ciphertext, validate=True)
        except (binascii.Error, ValueError, TypeError):
            raise KError(code="KIBO_WASM_ERROR", message="the ciphertext is not valid base64") from None
        out = w.app_crypto_open({"container": list(container), **self._common(cred, project_id, crypt, context, pin)})
        return _to_bytes(_field(out, "plaintext"))

    @staticmethod
    def _failure(e: BaseException) -> CryptoItemResult:
        k = _map_core_error(e)
        return CryptoItemResult(code=k.code, message=k.message)

    # -- the ClientCore surface --------------------------------------------------------------------------------------

    def encrypt(self, crypt: str, items: list[dict[str, Any]], *, expected_key_refs: Sequence[str] | None = None) -> CryptoBatch:
        pin = self._effective_pin(expected_key_refs)
        refused = self._refuse_if_key_not_pinned(pin, len(items))
        if refused is not None:
            return refused
        w = self._bound()
        cred, project_id = self._credential(w, crypt, "encrypt")
        out: list[CryptoItemResult] = []
        for it in items:
            try:
                out.append(CryptoItemResult(ciphertext=self._seal(
                    w, cred, project_id, crypt, it["plaintext"], it.get("context"), pin)))
            except Exception as e:  # noqa: BLE001
                out.append(self._failure(e))
        return CryptoBatch(items=out)

    def decrypt(self, crypt: str, items: list[dict[str, Any]], *, expected_key_refs: Sequence[str] | None = None) -> CryptoBatch:
        pin = self._effective_pin(expected_key_refs)
        refused = self._refuse_if_key_not_pinned(pin, len(items))
        if refused is not None:
            return refused
        w = self._bound()
        cred, project_id = self._credential(w, crypt, "decrypt")
        out: list[CryptoItemResult] = []
        for it in items:
            try:
                out.append(CryptoItemResult(plaintext=self._open(
                    w, cred, project_id, crypt, it["ciphertext"], it.get("context"), pin)))
            except Exception as e:  # noqa: BLE001
                out.append(self._failure(e))
        return CryptoBatch(items=out)

    def rewrap(self, crypt: str, items: list[dict[str, Any]], *, expected_key_refs: Sequence[str] | None = None) -> CryptoBatch:
        pin = self._effective_pin(expected_key_refs)
        refused = self._refuse_if_key_not_pinned(pin, len(items))
        if refused is not None:
            return refused
        w = self._bound()
        cred, project_id = self._credential(w, crypt, "rewrap")
        out: list[CryptoItemResult] = []
        for it in items:
            try:
                plaintext = self._open(w, cred, project_id, crypt, it["ciphertext"], it.get("context"), pin)
                out.append(CryptoItemResult(ciphertext=self._seal(
                    w, cred, project_id, crypt, plaintext, it.get("context"), pin)))
            except Exception as e:  # noqa: BLE001
                out.append(self._failure(e))
        return CryptoBatch(items=out)


def create_kibo_client_core(
    *,
    provider: str,
    posture: str,
    key_ref: Mapping[str, Any],
    federation: Mapping[str, Any],
    expected_key_refs: Sequence[str],
    federated_token: Callable[[str, str], Any],
    project_id: str | None = None,
    provider_config: Mapping[str, Any] | None = None,
    core: Any = None,
    exchange_deadline_secs: float = 30.0,
    now: Callable[[], int] | None = None,
) -> Any:
    """Build the client core. Register it with ``client.crypto.use_client_core(core)``.

    - ``provider``: your cloud, ``"aws"``, ``"gcp"``, ``"azure"`` or ``"vault"``; must equal ``federation["provider"]``.
    - ``posture``: ``"byok"`` or ``"hyok"``.
    - ``key_ref``: the exact key this application uses, ``{"version": 1, "external_ref": "arn:..."}``.
    - ``federation``: how to exchange the platform's identity token for a credential on your cloud (``role_arn``,
      ``session_name``, ``region``, ``key_arn``, ``duration_secs`` for AWS; ``audience`` for GCP; ``tenant_id``,
      ``client_id``, ``scope`` for Azure; ``vault_addr``, ``jwt_auth_mount``, ``role`` for Vault;
      ``jwt_auth_mount`` is the JWT auth mount: the name Vault serves the JWT login under, ``jwt`` for ``auth/jwt``,
      not the transit mount, which comes from the key reference).
    - ``expected_key_refs``: the keys this application allows. Required and non-empty: a core with no pin opens whatever key
      a stored container names, so this client never calls without one. ``key_ref`` must be in it.
    - ``federated_token``: ``lambda crypt, op: client.crypto.federated_token(crypt, op)``.
    - ``project_id``: the project id (``proj_...``), bound into what is sealed. Leave out only to take the token response's
      value; a value given here is never overridden.
    - ``provider_config``: ``aws_region`` or ``vault_address``.
    - ``core``: the loaded binding. Defaults to the bundled one, loaded on first use.
    - ``exchange_deadline_secs``: how long the exchange may take before it is abandoned. Default 30.
    - ``now``: the host clock, Unix seconds.
    """
    fed_provider = federation.get("provider") if isinstance(federation, Mapping) else None
    if provider != fed_provider:
        raise _bad("provider and federation.provider must be the same cloud", field="provider")
    if provider not in _PROVIDER_WIRE:
        raise _bad('provider must be "aws", "gcp", "azure" or "vault"', field="provider")
    if posture not in ("byok", "hyok"):
        raise _bad('posture must be "byok" or "hyok"', field="posture")
    pin = check_expected_key_refs(expected_key_refs)
    if pin is None:
        raise _bad("expected_key_refs is required: this client never seals or opens without a key pin", field="expectedKeyRefs")
    if not isinstance(key_ref, Mapping) or not isinstance(key_ref.get("external_ref"), str) or not isinstance(key_ref.get("version"), int):
        raise _bad("key_ref must be {version: int, external_ref: str}", field="key_ref")
    if not key_ref_matches(key_ref["external_ref"], pin, provider):  # type: ignore[arg-type]
        raise _bad("key_ref is not in expected_key_refs", field="key_ref")
    if not callable(federated_token):
        raise _bad("federated_token must be a function (crypt, op) -> FederatedToken", field="federated_token")
    return _KiboClientCore(
        provider=provider, posture=posture, project_id=project_id, key_ref=key_ref,
        provider_config=provider_config or {}, federation=federation, pin=pin, federated_token=federated_token,
        core=core, deadline_secs=exchange_deadline_secs, now=now or (lambda: int(time.time())))
