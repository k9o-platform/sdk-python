"""
Server-side encryption: `client.crypto.*`. The platform encrypts and decrypts at the API with your project's key.

Wire mapping (project plane):
  - crypto.encrypt(crypt, items)   POST /@<slug>/crypto/encrypt
  - crypto.decrypt(crypt, items)   POST /@<slug>/crypto/decrypt
  - crypto.rewrap(crypt, items)    POST /@<slug>/crypto/rewrap

You never name a key. Encrypt checks the project's custody mode on every call (`GET /@<slug>/crypto/mode`, never cached) and
routes by it: platform-managed, confidential-workload and SKR custody go to the server route; a key held in your own KMS or
HSM (BYOK or HYOK) goes to the client core you register with `crypto.use_client_core(core)`. With no client core such a call
raises `K9O_ZK_CLIENT_CORE_UNAVAILABLE` and no plaintext is sent. An unknown mode, or a failed mode check, raises and sends
nothing. When you set `expected_key_refs` on the client, the core receives it on every call and refuses a key that is not in
it (version ignored). Decrypt and rewrap route by the ciphertext: the platform is tried first and, when it answers 409
`K9O_ZK_STORE_FLOW_REQUIRED` with route `client`, the registered client core opens it.
A call carries a `crypt` (a registered name, `[a-z0-9-]{1,64}`, not `actions`; the unit your access rules are written against) and 1 to 100 items. Each
item has its own answer, in order; one item that cannot be opened does not fail the others.

  - encrypt items: `bytes`, a `str` (encoded as UTF-8) or `{"plaintext": bytes | str, "context": str}`.
  - decrypt and rewrap items: the ciphertext `str` exactly as encrypt returned it, or `{"ciphertext": str, "context": str}`.
  - `context` is optional, at most 256 bytes (UTF-8), and must be the same on decrypt as it was on encrypt, as must the
    crypt.

Plaintext is base64 on the wire; the SDK encodes and decodes it for you. At most 64 KiB per item and 1 MiB per call:
the SDK refuses more before sending (`K9O_PAYLOAD_TOO_LARGE`), and a bad crypt, item count, type or context is
`K9O_INVALID_FIELD_VALUE`. The ciphertext is one opaque string; the SDK never parses it.

Errors arrive as `KError` and nothing is retried by the SDK, a 5xx included:
  - 400 `K9O_ENVELOPE_INTEGRITY_FAILURE`: the ciphertext could not be opened (wrong crypt or context, tampered, not
    yours). It is one code for every cause by design, so the SDK cannot tell you why.
  - 409 `K9O_SEALED_UNDER_RETIRED_KEY`: sealed under a key version that has retired; `context["resealableUntil"]` while
    the window is open. `rewrap` is the fix.
  - 404 `K9O_CRYPT_NOT_FOUND` (encrypt): the crypt name is not registered. 409 `K9O_CRYPT_RETIRED` (encrypt): the name is
    retired. Both reach you as the gateway sent them; decrypt and rewrap are refused for neither.
  - 413 `K9O_PAYLOAD_TOO_LARGE`, 429 `K9O_RATE_LIMITED` (`retry_after_sec`).
  - 409 `K9O_ZK_STORE_FLOW_REQUIRED`: the key is held in the customer's KMS or HSM (or custody moved during the call); the SDK
    reroutes it itself, so it reaches you only after one re-route (encrypt) or from a raw HTTP call.
  - 501 `K9O_ZK_CLIENT_CORE_UNAVAILABLE`: the call needs the client route and no client core is registered.
  - `K9O_CRYPTO_KEY_PIN_UNENFORCED` (no HTTP status): `expected_key_refs` is set and the client core does not declare `enforces_expected_key_refs = True`.
  - 503 `K9O_KEY_SERVICE_UNAVAILABLE`: fails the whole call, never a pass-through.

The shape of one batch item follows the gateway: `{"ciphertext": ...}`, `{"plaintext": ...}` or `{"code", "message"}`.
Fields the SDK does not know are kept in `CryptoItemResult.raw`.
"""

from __future__ import annotations

import base64
import binascii
import re
from dataclasses import dataclass, field
from typing import Any, Literal, Mapping, Protocol, Sequence, runtime_checkable

from .approvals import _invalid, _require_project_slug
from .envelope import Envelope
from .errors import KError
from .http import HttpClientConfig, request
from .url_grammar import project_path

MAX_ITEMS = 100
MAX_ITEM_BYTES = 64 * 1024
MAX_CALL_BYTES = 1024 * 1024
MAX_CONTEXT_BYTES = 256
_CRYPT = re.compile(r"^(?!actions$)[a-z0-9-]{1,64}$")

ZK_STORE_FLOW_REQUIRED = "K9O_ZK_STORE_FLOW_REQUIRED"
ZK_CLIENT_CORE_UNAVAILABLE = "K9O_ZK_CLIENT_CORE_UNAVAILABLE"
CUSTODY_MODE_UNKNOWN = "K9O_CRYPTO_CUSTODY_UNKNOWN"
KEY_PIN_UNENFORCED = "K9O_CRYPTO_KEY_PIN_UNENFORCED"
_SERVER_MODES = frozenset({"PLATFORM_MANAGED", "CONFIDENTIAL_WORKLOAD", "SKR"})
_CLIENT_MODES = frozenset({"BYOK", "HYOK"})


@dataclass(frozen=True)
class CryptoItemResult:
    """The answer for one item. `ok` is True when it carries a result and no `code`."""

    ciphertext: str | None = None  # encrypt and rewrap: the opaque ciphertext, as received
    plaintext: bytes | None = None  # decrypt: the decoded bytes
    code: str | None = None  # a per-item error code (the batch itself succeeded)
    message: str | None = None
    raw: Mapping[str, Any] = field(default_factory=dict, compare=False, repr=False)  # the item as the gateway sent it

    @property
    def ok(self) -> bool:
        return self.code is None

    def text(self, encoding: str = "utf-8") -> str:
        """The decrypted plaintext as text. Raises if this item failed or carries no plaintext."""
        if self.plaintext is None:
            raise _invalid("this item carries no plaintext", code=self.code)
        return self.plaintext.decode(encoding)


@dataclass(frozen=True)
class CryptoBatch:
    """What a crypto call answers with: one `CryptoItemResult` per request item, in order."""

    items: list[CryptoItemResult]
    meta: Any = field(default=None, compare=False, repr=False)

    @property
    def ok(self) -> bool:
        return all(i.ok for i in self.items)


def _payload_too_large(message: str, **context: Any) -> KError:
    return KError(code="K9O_PAYLOAD_TOO_LARGE", message=message, context=context)


def _crypt(value: Any) -> str:
    if not isinstance(value, str) or not _CRYPT.match(value):
        raise _invalid("crypt must be 1 to 64 characters of a-z, 0-9 and -, and not \"actions\"", crypt=value if isinstance(value, str) else None)
    return value


def _context(value: Any, index: int) -> str | None:
    if value is None:
        return None
    if isinstance(value, (bytes, bytearray)):
        try:
            value = bytes(value).decode("utf-8")
        except UnicodeDecodeError:
            raise _invalid("context must be text (UTF-8)", index=index) from None
    if not isinstance(value, str):
        raise _invalid("context must be a string", index=index)
    size = len(value.encode("utf-8"))
    if size > MAX_CONTEXT_BYTES:
        raise _invalid(f"context is at most {MAX_CONTEXT_BYTES} bytes", index=index, bytes=size)
    return value


def _items(items: Any, field_name: str, total: list[int], wire: bool = True) -> list[dict[str, Any]]:
    if isinstance(items, (str, bytes, bytearray, Mapping)) or not isinstance(items, Sequence):
        raise _invalid("items must be a list")
    if not 1 <= len(items) <= MAX_ITEMS:
        raise _invalid(f"items must hold 1 to {MAX_ITEMS} entries", count=len(items))
    out: list[dict[str, Any]] = []
    for i, item in enumerate(items):
        context = None
        if isinstance(item, Mapping):
            unknown = set(item) - {field_name, "context"}
            if unknown:
                raise _invalid(f"an item takes {field_name} and context only", index=i, unknown=sorted(unknown))
            value, context = item.get(field_name), _context(item.get("context"), i)
        else:
            value = item
        entry: dict[str, Any]
        if field_name == "plaintext":
            if isinstance(value, str):
                value = value.encode("utf-8")
            if isinstance(value, (bytearray, memoryview)):
                value = bytes(value)
            if not isinstance(value, bytes):
                raise _invalid("plaintext must be bytes or a string", index=i)
            if len(value) > MAX_ITEM_BYTES:
                raise _payload_too_large(f"an item is at most {MAX_ITEM_BYTES} bytes", index=i, bytes=len(value), limit=MAX_ITEM_BYTES)
            total[0] += len(value)
            if total[0] > MAX_CALL_BYTES:
                raise _payload_too_large(f"a call is at most {MAX_CALL_BYTES} bytes of plaintext", limit=MAX_CALL_BYTES)
            entry = {"plaintext": base64.b64encode(value).decode("ascii") if wire else value}
        else:
            if not isinstance(value, str) or not value:
                raise _invalid("ciphertext must be the string encrypt returned", index=i)
            entry = {"ciphertext": value}
        if context is not None:
            entry["context"] = context
        out.append(entry)
    return out


def _decode_b64(value: str) -> bytes | None:
    for decoder in (base64.b64decode, base64.urlsafe_b64decode):
        try:
            return decoder(value + "=" * (-len(value) % 4))
        except (binascii.Error, ValueError):
            continue
    return None


def _decode(env: Envelope) -> CryptoBatch:
    data = env.data
    raw_items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(raw_items, list):
        raise KError(code="K9O_INTERNAL_ERROR", message="Malformed crypto response: expected {items: [...]}",
                     context={}, status=200)
    out: list[CryptoItemResult] = []
    for item in raw_items:
        if not isinstance(item, dict):
            out.append(CryptoItemResult(code="K9O_INTERNAL_ERROR", message="Malformed batch item", raw={}))
            continue
        ct, pt = item.get("ciphertext"), item.get("plaintext")
        out.append(CryptoItemResult(
            ciphertext=ct if isinstance(ct, str) else None,
            plaintext=_decode_b64(pt) if isinstance(pt, str) else None,
            code=item["code"] if isinstance(item.get("code"), str) else None,
            message=item["message"] if isinstance(item.get("message"), str) else None,
            raw=dict(item)))
    return CryptoBatch(items=out, meta=env.meta)


@dataclass(frozen=True)
class CryptoMode:
    """The project's custody mode and an opaque `epoch` that changes with every custody transition.

    `route` is `"client"` or `"server"` when the gateway named one, else `None` (an older gateway, or a value this SDK does not
    know). Follow it; do not infer the route from `custody_mode`."""

    custody_mode: str
    epoch: str
    route: str | None = None


_FEDERATED_OPS = ("encrypt", "decrypt", "rewrap")


@dataclass(frozen=True)
class FederatedToken:
    """A short-lived identity token (RS256 JWT) for your own cloud's token exchange.

    `expires_at` is at most 15 minutes after issue. `issuer` is the project's issuer URL and never changes; `subject` is
    `<project>:crypt:<crypt>:op:<op>`; `audience` is the one set on your key attachment. The token is a credential:
    it is left out of `repr`, so a log line or traceback does not carry it. `provider` and `project_id` are set only when
    the gateway sent them as non-empty strings.
    """

    token: str = field(repr=False)
    expires_at: str
    issuer: str
    subject: str
    audience: str
    kid: str
    provider: str | None = None
    project_id: str | None = None


@dataclass(frozen=True)
class Crypt:
    """A registered crypt name. `status` is `"active"` or `"retired"`; `retired_at` is `None` while active.

    A retired name only makes `encrypt` refuse it; `decrypt` and `rewrap` never read the registry.
    """

    name: str
    status: Literal["active", "retired"]
    created_at: str
    retired_at: str | None = None


def _bad_crypts(op: str, why: str) -> KError:
    return KError(code="K9O_INTERNAL_ERROR", message=f"crypto.crypts.{op}: the gateway answered {why}", context={}, status=200)


def _crypt_of(d: Any, op: str) -> Crypt:
    if not isinstance(d, dict):
        raise _bad_crypts(op, "without a crypt")
    if not isinstance(d.get("name"), str) or d["name"] == "":
        raise _bad_crypts(op, "without a name")
    if d.get("status") not in ("active", "retired"):
        raise _bad_crypts(op, "with an unknown status")
    if not isinstance(d.get("createdAt"), str) or d["createdAt"] == "":
        raise _bad_crypts(op, "without createdAt")
    retired = d.get("retiredAt")
    if retired is not None and not isinstance(retired, str):
        raise _bad_crypts(op, "with a retiredAt that is neither a date nor null")
    return Crypt(name=d["name"], status=d["status"], created_at=d["createdAt"], retired_at=retired)


def _check_crypt_name(name: Any, op: str) -> str:
    if not isinstance(name, str) or not _CRYPT.match(name):
        raise _invalid(f'crypto.crypts.{op}: name must be 1 to 64 characters of a-z, 0-9 and -, and not "actions"', field="name")
    return name


class CryptsNamespace:
    """`client.crypto.crypts`: the project's registered crypt names.

    A registered name is what `encrypt` accepts. Retiring or never registering a name only makes `encrypt` refuse it;
    `decrypt` and `rewrap` never read the registry, so data stays readable. This does not enforce, isolate or destroy
    anything. Never retried by the SDK.
    """

    def __init__(self, config: HttpClientConfig) -> None:
        self._config = config

    def list(self) -> "list[Crypt]":
        """The crypts you hold: all of them with a `*` grant on crypts, none for a member with no grant (`GET /crypto/crypts`).
        Errors: 403 `K9O_ACCESS_DENIED` (not a project member), 503 if the registry cannot be read."""
        slug = _require_project_slug(self._config, "crypto")
        env = request(self._config, method="GET", path=project_path(slug, "crypto/crypts"))
        data = env.data
        if not isinstance(data, dict) or not isinstance(data.get("crypts"), list):
            raise _bad_crypts("list", "without crypts")
        return [_crypt_of(c, "list") for c in data["crypts"]]

    def create(self, name: str) -> Crypt:
        """Register a crypt so `encrypt` accepts it (`POST /crypto/crypts`, sends only `{name}`). `name` is 1 to 64 characters
        of a-z, 0-9 and `-`, and not `actions`; a bad name is refused (`K9O_INVALID_FIELD_VALUE`) before anything is sent.
        Needs `crypto:manage_crypts` (project admins by default). Errors: 409 `K9O_CRYPT_ALREADY_EXISTS`,
        409 `K9O_CRYPT_LIMIT_EXCEEDED` (256 per project, retired ones counted), 409 `K9O_CRYPT_RETIRED`."""
        slug = _require_project_slug(self._config, "crypto")
        n = _check_crypt_name(name, "create")
        env = request(self._config, method="POST", path=project_path(slug, "crypto/crypts"), body={"name": n})
        return _crypt_of(env.data, "create")

    def retire(self, name: str) -> Crypt:
        """Retire a crypt (`POST /crypto/crypts/<name>/retire`, no body): `encrypt` then refuses the name; `decrypt` and
        `rewrap` still work. Needs `crypto:manage_crypts`; any personal access token is refused (`K9O_RBAC_DENY`).
        Errors: 404 `K9O_CRYPT_NOT_FOUND`, 409 `K9O_CRYPT_RETIRED`."""
        slug = _require_project_slug(self._config, "crypto")
        n = _check_crypt_name(name, "retire")
        env = request(self._config, method="POST", path=project_path(slug, f"crypto/crypts/{n}/retire"))
        return _crypt_of(env.data, "retire")


@runtime_checkable
class ClientCore(Protocol):
    """The client-side crypto core: the hook the Kibo binding fills, so the same calls work when the key is in your own KMS or HSM.

    Items are the normalized request items: encrypt takes `{"plaintext": bytes, "context"?: str}`, decrypt and rewrap take
    `{"ciphertext": str, "context"?: str}`. Each method returns a `CryptoBatch` (or a list of `CryptoItemResult`), one
    result per item, in order. A core never sees a platform epoch.

    `expected_key_refs` is the tuple of key identifiers you set with `create_client(expected_key_refs=...)`. The router
    passes it as a keyword argument only when you set it, so a core written without the parameter keeps working while the
    option is unset. When it is passed, the core must refuse a key whose reference (version ignored, see `key_ref_matches`)
    is not in it, before it uses any token or makes any network call. A core that is used with the option set must accept it.

    A core declares that it does the refusing with the attribute `enforces_expected_key_refs = True` (class or instance
    attribute). When you set `expected_key_refs` and the registered core does not declare it, the call is refused with
    `K9O_CRYPTO_KEY_PIN_UNENFORCED` before it reaches the core, because a pin the core ignores would look like a pin.

    """

    def encrypt(self, crypt: str, items: list[dict[str, Any]], *, expected_key_refs: Sequence[str] | None = None) -> CryptoBatch | list[CryptoItemResult]: ...

    def decrypt(self, crypt: str, items: list[dict[str, Any]], *, expected_key_refs: Sequence[str] | None = None) -> CryptoBatch | list[CryptoItemResult]: ...

    def rewrap(self, crypt: str, items: list[dict[str, Any]], *, expected_key_refs: Sequence[str] | None = None) -> CryptoBatch | list[CryptoItemResult]: ...


def check_expected_key_refs(refs: Sequence[str] | None) -> tuple[str, ...] | None:
    """Check `expected_key_refs` from the client options and return it as an immutable tuple (`None` when not set).

    It must be a non-empty list of non-blank strings without whitespace, and none may contain `*`, since a wildcard pins
    nothing. Raises `K9O_INVALID_FIELD_VALUE` otherwise.
    """
    if refs is None:
        return None
    def bad(message: str) -> KError:
        return _invalid(f"expectedKeyRefs: {message}", field="expectedKeyRefs")
    if isinstance(refs, (str, bytes, bytearray, Mapping)) or not isinstance(refs, Sequence) or len(refs) == 0:
        raise bad("pass a non-empty list of key identifiers, or leave it out")
    for r in refs:
        if not isinstance(r, str) or r.strip() == "" or re.search(r"\s", r):
            raise bad("each entry must be a key identifier without spaces")
        if "*" in r:
            raise bad('an entry cannot contain "*"; name the exact key')
    return tuple(refs)


KeyRefProvider = Literal["aws", "gcp", "azure", "vault"]


def _key_identity(ref: str, provider: str) -> str:
    if provider == "aws":
        return ref
    if provider == "gcp":
        return ref.split("/cryptoKeyVersions/")[0]
    if provider == "azure":
        return "/".join(ref.split("/")[:5]).lower()
    cut = ref.rfind("/")
    path = "" if cut < 0 else ref[:cut]
    key = ref if cut < 0 else ref[cut + 1:]
    return f"{path}/{key.split(':')[0]}"


def key_ref_matches(key_ref: str, expected: Sequence[str], provider: KeyRefProvider) -> bool:
    """Whether `key_ref` is one of `expected`, ignoring the key version, by the rule the Kibo client core applies.

    `provider` is required and is never inferred from the reference: "aws", "gcp", "azure" or "vault" (anything else
    raises `K9O_INVALID_FIELD_VALUE`). AWS compares the whole reference; GCP drops `/cryptoKeyVersions/<n>` and what
    follows; Azure compares the first five `/`-separated parts (`https://<host>/keys/<key>`) in lower case, so a version
    and the case of the host and key name are ignored; Vault compares `<path>/<key>` without a `:<version>` suffix. A
    Vault reference can look like a URL, so the caller must name the provider. For authors of a client core; the SDK
    does not call it.
    """
    if provider not in ("aws", "gcp", "azure", "vault"):
        raise _invalid('provider must be "aws", "gcp", "azure" or "vault"', field="provider")
    k = _key_identity(key_ref, provider)
    return any(_key_identity(e, provider) == k for e in expected)


def _client_core_unavailable(custody_mode: Any) -> KError:
    return KError(
        code=ZK_CLIENT_CORE_UNAVAILABLE,
        message=("this project's key is held in your own KMS; install and register the Kibo client core for this language "
                 "(crypto.use_client_core), or call kibo.encrypt/decrypt directly"),
        context={"custodyMode": custody_mode if isinstance(custody_mode, str) else None, "requirement": "kibo-client-core"},
        status=501)


def _from_core(out: Any) -> CryptoBatch:
    if isinstance(out, CryptoBatch):
        return out
    if isinstance(out, (list, tuple)) and all(isinstance(i, CryptoItemResult) for i in out):
        return CryptoBatch(items=list(out))
    raise KError(code="K9O_INTERNAL_ERROR", message="The client core returned an unexpected result", context={})


def _is_client_route_409(e: KError) -> bool:
    return e.status == 409 and e.code == ZK_STORE_FLOW_REQUIRED and e.context.get("route") == "client"


class CryptoNamespace:
    def __init__(self, config: HttpClientConfig) -> None:
        self._config = config
        self._core: ClientCore | None = None
        self.crypts = CryptsNamespace(config)

    def _core_kwargs(self) -> dict[str, Any]:
        refs = self._config.expected_key_refs
        out: dict[str, Any] = {} if refs is None else {"expected_key_refs": refs}
        return out

    def _need_core(self, custody_mode: Any) -> ClientCore:
        core = self._core
        if core is None:
            raise _client_core_unavailable(custody_mode)
        if self._config.expected_key_refs is not None and getattr(core, "enforces_expected_key_refs", False) is not True:
            raise KError(
                code=KEY_PIN_UNENFORCED,
                message="expectedKeyRefs is set but the client core does not declare that it enforces it; nothing was sent",
                context={"custodyMode": custody_mode if isinstance(custody_mode, str) else None,
                         "requirement": "client-core-enforces-expected-key-refs"},
                recommendation="Use a client core that enforces the pin (it sets enforces_expected_key_refs to True), or leave expected_key_refs out.",
                status=None)
        return core

    def use_client_core(self, core: ClientCore | None) -> None:
        """Register the client core that serves keys held in your own KMS or HSM (the hook the Kibo binding fills). `None` removes it."""
        if core is not None:
            for name in ("encrypt", "decrypt", "rewrap"):
                if not callable(getattr(core, name, None)):
                    raise _invalid(f"a client core needs encrypt, decrypt and rewrap; {name} is missing")
        self._core = core

    def mode(self, crypt: str | None = None) -> CryptoMode:
        """The project's custody mode, its opaque `epoch` and the `route` (`"client"`, `"server"` or `None`; `GET /crypto/mode`). Needs a `crypto` grant; no audit row.

        Pass a `crypt` to ask about that crypt (`?crypt=`): a personal access token or workload policy scoped to named
        crypts is refused without one. A bad name is refused (`K9O_INVALID_FIELD_VALUE`) before anything is sent."""
        if crypt is not None and (not isinstance(crypt, str) or not _CRYPT.match(crypt)):
            raise _invalid('crypto.mode: crypt must be 1 to 64 characters of a-z, 0-9 and -, and not "actions"', field="crypt")
        slug = _require_project_slug(self._config, "crypto")
        env = request(self._config, method="GET", path=project_path(slug, "crypto/mode"),
                      **({} if crypt is None else {"query": {"crypt": crypt}}))
        data = env.data
        mode = data.get("custodyMode") if isinstance(data, dict) else None
        epoch = data.get("epoch") if isinstance(data, dict) else None
        if not isinstance(mode, str) or not mode or not isinstance(epoch, str) or not epoch:
            raise KError(code="K9O_INTERNAL_ERROR", message="Malformed crypto mode response: expected {custodyMode, epoch}",
                         context={}, status=200)
        raw_route = data.get("route")
        return CryptoMode(custody_mode=mode, epoch=epoch, route=raw_route if raw_route in ("client", "server") else None)

    def _check_mode(self, crypt: str) -> tuple[str, str, str]:
        """The fresh check before every encrypt: `(custody_mode, epoch, route)`. Any answer this SDK cannot route raises
        `K9O_CRYPTO_CUSTODY_UNKNOWN`, so no plaintext goes anywhere. Only an explicit `route: "server"` sends BYOK or HYOK
        plaintext to the platform; a missing route means the client core."""
        slug = _require_project_slug(self._config, "crypto")
        data = request(self._config, method="GET", path=project_path(slug, "crypto/mode"), query={"crypt": crypt}).data
        d = data if isinstance(data, dict) else {}
        seen = d.get("custodyMode")
        if not isinstance(seen, str) or not seen:
            raise KError(code=CUSTODY_MODE_UNKNOWN, message="The project's custody mode answer has no custodyMode; nothing was sent", context={})
        context: dict[str, Any] = {"custodyMode": seen}
        raw = d.get("route")
        if "route" in d and not (isinstance(raw, str) and raw in ("server", "client")):
            if isinstance(raw, str):
                context["route"] = raw
            raise KError(code=CUSTODY_MODE_UNKNOWN, message="The project's custody mode answer has an unknown route; nothing was sent", context=context)
        epoch = d.get("epoch") if isinstance(d.get("epoch"), str) else ""
        if seen in _SERVER_MODES:
            route = "server"
        elif seen in _CLIENT_MODES:
            route = "server" if raw == "server" else "client"
        else:
            raise KError(code=CUSTODY_MODE_UNKNOWN, message="The project's custody mode is not one this SDK can route; nothing was sent", context=context)
        if route == "server" and not epoch:
            raise KError(code=CUSTODY_MODE_UNKNOWN, message="The project's custody mode answer for a server route has no epoch; nothing was sent", context=context)
        return seen, epoch, route

    def federated_token(self, crypt: str, op: str) -> FederatedToken:
        """A short-lived identity token for a crypt and an operation, to present to your own cloud's token exchange
        (`POST /crypto/federated-token`). Needs the project editor role. `op` is `"encrypt"`, `"decrypt"` or `"rewrap"`; a bad
        `crypt` or `op` is refused (`K9O_INVALID_FIELD_VALUE`) before anything is sent. Errors: 409 `K9O_CUSTODY_SERVER_ROUTE` (`context["route"]` is `server` or `client-idp`),
        429 `K9O_RATE_LIMITED`, 503 `K9O_KEY_SERVICE_UNAVAILABLE` (nothing was issued). Never retried by the SDK."""
        slug = _require_project_slug(self._config, "crypto")
        ring = _crypt(crypt)
        if op not in _FEDERATED_OPS:
            raise _invalid('op must be "encrypt", "decrypt" or "rewrap"', op=op if isinstance(op, str) else None)
        env = request(self._config, method="POST", path=project_path(slug, "crypto/federated-token"), body={"crypt": ring, "op": op})
        data = env.data
        for key in ("token", "expiresAt", "issuer", "subject", "audience", "kid"):
            if not isinstance(data, dict) or not isinstance(data.get(key), str) or not data[key]:
                raise KError(code="K9O_INTERNAL_ERROR", message=f"Malformed federated token response: no {key}", context={}, status=200)
        extra = {k: data[src] for k, src in (("provider", "provider"), ("project_id", "projectId"))
                 if isinstance(data.get(src), str) and data[src]}
        return FederatedToken(token=data["token"], expires_at=data["expiresAt"], issuer=data["issuer"], subject=data["subject"],
                              audience=data["audience"], kid=data["kid"], **extra)

    def _post(self, verb: str, slug: str, body: dict[str, Any]) -> CryptoBatch:
        # A POST: the SDK never sends it again by itself, whatever the answer was.
        return _decode(request(self._config, method="POST", path=project_path(slug, f"crypto/{verb}"), body=body))

    def _by_ciphertext(self, verb: str, crypt: str, items: Any) -> CryptoBatch:
        slug = _require_project_slug(self._config, "crypto")
        ring = _crypt(crypt)
        total = [0]
        wire = _items(items, "ciphertext", total)
        try:
            return self._post(verb, slug, {"crypt": ring, "items": wire})
        except KError as e:
            if not _is_client_route_409(e):
                raise
            core = self._need_core(e.context.get("custodyMode"))
            return _from_core(getattr(core, verb)(ring, _items(items, "ciphertext", [0], wire=False), **self._core_kwargs()))

    def encrypt(self, crypt: str, items: Sequence[Any]) -> CryptoBatch:
        """Encrypt 1 to 100 items under the project key. Each result's `ciphertext` is the string to store.

        An item is `bytes`, a `str` (UTF-8) or `{"plaintext": ..., "context": "..."}`. Keep the crypt and the context:
        decrypt needs both. Needs a role with encrypt on the crypt (default PROJECT_EDITOR); service tokens and personal
        access tokens only, not extensions or end users.

        Checks the project's custody mode first, on every call, and never sends plaintext to the platform when the key is in
        your own KMS or HSM unless the mode answer names `route: "server"`: that call goes to the registered client core (`use_client_core`), or raises
        `K9O_ZK_CLIENT_CORE_UNAVAILABLE` when there is none.
        """
        slug = _require_project_slug(self._config, "crypto")
        ring = _crypt(crypt)
        wire = _items(items, "plaintext", [0])
        for attempt in (1, 2):
            custody_mode, epoch, route = self._check_mode(ring)
            if route == "server":
                try:
                    return self._post("encrypt", slug, {"crypt": ring, "custodyEpoch": epoch, "items": wire})
                except KError as e:
                    # Custody moved between the check and the send: check and route once more, then give up.
                    if attempt == 1 and e.status == 409 and e.code == ZK_STORE_FLOW_REQUIRED:
                        continue
                    raise
            core = self._need_core(custody_mode)
            return _from_core(core.encrypt(ring, _items(items, "plaintext", [0], wire=False), **self._core_kwargs()))
        raise AssertionError("unreachable")

    def decrypt(self, crypt: str, items: Sequence[Any]) -> CryptoBatch:
        """Decrypt ciphertexts that `encrypt` produced. Each result's `plaintext` is `bytes` (`.text()` for a string).

        An item is the ciphertext `str` or `{"ciphertext": ..., "context": "..."}` with the crypt and context used to
        encrypt. 400 `K9O_ENVELOPE_INTEGRITY_FAILURE` for every ciphertext that cannot be opened, whatever the reason.
        Routes by the ciphertext: the platform first, then the client core when the ciphertext belongs to your own key.
        """
        return self._by_ciphertext("decrypt", crypt, items)

    def rewrap(self, crypt: str, items: Sequence[Any]) -> CryptoBatch:
        """Re-seal ciphertexts under the project's current key version without the plaintext leaving the platform.

        Same items as `decrypt`; each result's `ciphertext` replaces the old one. The fix for 409
        `K9O_SEALED_UNDER_RETIRED_KEY` while `resealableUntil` is open. Routed like `decrypt`.
        """
        return self._by_ciphertext("rewrap", crypt, items)
