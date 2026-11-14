"""
Rampart namespace: `client.rampart.*`, hosted content classification with a signed receipt.

Wire mapping:
  - classify(input, label=)   POST /@<slug>/rampart/classify   (any project member; counted on a 200 only)
  - jwks()                    GET  /rampart/jwks               (public: no project prefix, no token needed)

Rampart is advisory. `classify` returns a verdict (`clear`, `suspicious` or `malicious`) and blocks nothing; your code
decides what to do with it. It fails closed in the sense that an unavailable classifier raises KError
`K9O_RAMPART_CLASSIFIER_UNAVAILABLE` (HTTP 503): there is no verdict and never a default pass. Input the classifier is
unsure about comes back `suspicious` with its score.

A long input is not truncated. It is scored in overlapping windows of up to 512 tokens and the highest injection score
decides. Past 16 windows (about 6,000 tokens) the answer is 413 `K9O_RAMPART_INPUT_TOO_LONG` with no verdict; `classify`
raises `RampartInputTooLong` (a KError) for it. The receipt's `windows` claim says how many windows were scored.

`ClassifyResult` holds the verdict fields and `classification_receipt`, the signed receipt exactly as received.
`verify_receipt(receipt, input)` checks that receipt on your machine, in this order, and raises `ReceiptRefusal` with
a closed `reason` at the first failure (no partial verdict is ever returned):

  1. `malformed`      the receipt is not a three-part JWS with JSON header and claims.
  2. `key_unknown`    the header `kid` is not in the key feed (the feed is fetched once and cached for the client's
                      life, and fetched again once when a `kid` is not in it). A key carried inside the receipt is
                      never used.
  3. `alg`            the header `alg` is not `EdDSA`.
  4. `typ`            the header `typ` is not exactly `k9o.rampart.classify.v1`.
  5. `signature`      the Ed25519 signature does not verify.
  6. `issuer`         `iss` is not the expected issuer. The expected issuer is the `issuer` the key feed advertises
                      (or the `issuer=` option, which wins); with neither, the receipt is refused.
  7. `project`        `project_id` is not the expected project: the `project_id=` option, else the `project_id` of the
                      `ClassifyResult` you passed in. `project_unknown` is raised when you pass a bare receipt string
                      and no `project_id=`, so there is nothing to compare against.
  8. `not_yet_valid` / `expired`   `iat` / `exp` fall outside the clock skew (default 60 s). The window is a replay
                      limit of one hour, not a claim that the classification stops being true.
  9. `input_hash`     `input_sha256` is not the SHA-256 of the canonical form of the `input` you pass,
                      in the platform's JCS code-point profile (keys sorted by UTF-8 byte order, integers only up to
                      2^53 - 1). An input that cannot be canonicalised is refused with this reason too.

The verdict in the returned `VerifiedReceipt` is read from the verified claims, never from an unsigned response body.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import time
from base64 import urlsafe_b64decode
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

import httpx

from . import _ed25519
from ._jcs import canonicalize
from .approvals import _invalid, _require_project_slug
from .errors import KError, kerror_from_wire, with_request_id
from .http import (HttpClientConfig, RetryConfig, _build_headers, _compute_delay_ms, _effective_timeout,
                   _is_retryable, request)
from .envelope import Envelope, meta_from_wire
from .region import resolve_regional_origin
from .url_grammar import project_path

RECEIPT_TYP = "k9o.rampart.classify.v1"
DEFAULT_CLOCK_SKEW_S = 60

REFUSAL_REASONS = ("malformed", "key_unknown", "alg", "typ", "signature", "issuer", "project", "project_unknown", "not_yet_valid",
                   "expired", "input_hash")
"""The closed set of `ReceiptRefusal.reason` values."""

_CLASSIFICATIONS = ("clear", "suspicious", "malicious")
_B64URL = re.compile(r"^[A-Za-z0-9_-]*$")


class ReceiptRefusal(KError):
    """`verify_receipt` refused the receipt. `reason` is one of `REFUSAL_REASONS`; `detail` says why in a sentence.

    It is a KError (code `K9O_RAMPART_RECEIPT_INVALID`), so `except KError` catches it too.
    """

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(code="K9O_RAMPART_RECEIPT_INVALID", message=f"receipt refused ({reason}): {detail}",
                         context={"reason": reason, "detail": detail})
        self.reason = reason
        self.detail = detail


class RampartInputTooLong(KError):
    """413 `K9O_RAMPART_INPUT_TOO_LONG`: the input needs more than 16 windows (about 6,000 tokens). There is no verdict; send less."""

    def __init__(self, source: KError) -> None:
        super().__init__(code=source.code, message=source.message, context=dict(source.context),
                         recommendation=source.recommendation, status=source.status, request_id=source.request_id,
                         retry_after_sec=source.retry_after_sec)


@dataclass(frozen=True)
class ClassifyResult:
    """What `rampart.classify` answers with."""

    classification: str  # "clear" | "suspicious" | "malicious"
    risk_score: int  # basis points, 0..10000
    confidence: int  # basis points, 0..10000
    categories: list[str]
    calibration_version: str
    classification_receipt: str  # the signed receipt exactly as received; pass it to verify_receipt
    project_id: str | None = None  # `proj_...`, as the server reports it; None when the server did not send it
    meta: Any = field(default=None, compare=False, repr=False)


@dataclass(frozen=True)
class VerifiedReceipt:
    """The verdict read from a receipt whose signature, issuer, project, time window and input hash all checked out."""

    classification: str
    risk_score: int
    confidence: int
    categories: list[str]
    calibration_version: str
    project_id: str
    input_sha256: str
    issuer: str
    issued_at: float
    expires_at: float
    key_id: str
    windows: int | None = None  # windows scored (1 when the input fit one); None on a receipt that does not carry the claim


def _b64url(part: str, what: str) -> bytes:
    if not _B64URL.match(part):
        raise ReceiptRefusal("malformed", f"{what} is not base64url")
    try:
        return urlsafe_b64decode(part + "=" * (-len(part) % 4))
    except ValueError as e:
        raise ReceiptRefusal("malformed", f"{what} is not base64url") from e


def _json_object(raw: bytes, what: str) -> dict[str, Any]:
    try:
        value = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as e:
        raise ReceiptRefusal("malformed", f"{what} is not JSON") from e
    if not isinstance(value, dict):
        raise ReceiptRefusal("malformed", f"{what} is not a JSON object")
    return value


def _is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _is_num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and v == v and v not in (float("inf"), float("-inf"))


def _keys_of(jwks: Any) -> dict[str, Mapping[str, Any]]:
    keys = jwks.get("keys") if isinstance(jwks, Mapping) else jwks
    if not isinstance(keys, (list, tuple)):
        raise _invalid("jwks must be a key set ({'keys': [...]}) or a list of keys")
    return {k["kid"]: k for k in keys if isinstance(k, Mapping) and isinstance(k.get("kid"), str)}


def _public_key_bytes(jwk: Mapping[str, Any]) -> bytes:
    if jwk.get("kty") != "OKP" or jwk.get("crv") != "Ed25519" or not isinstance(jwk.get("x"), str):
        raise ReceiptRefusal("key_unknown", "the key for this kid is not an Ed25519 public key")
    if jwk.get("alg") not in (None, "EdDSA"):
        raise ReceiptRefusal("key_unknown", "the key for this kid is not an EdDSA key")
    raw = _b64url(jwk["x"], "key")
    if len(raw) != 32:
        raise ReceiptRefusal("key_unknown", "the key for this kid is not 32 bytes")
    return raw


class RampartNamespace:
    def __init__(self, config: HttpClientConfig) -> None:
        self._config = config
        self._keys: dict[str, Mapping[str, Any]] = {}
        self._issuer: str | None = None

    def classify(self, input: Mapping[str, Any], *, label: str | None = None) -> ClassifyResult:
        """Classify `input` (a dict, handed to the classifier verbatim). `label` (1 to 128 characters) names the context.

        Raises KError `K9O_RAMPART_CLASSIFIER_UNAVAILABLE` (503) when the classifier is down: there is no verdict.
        Raises `RampartInputTooLong` (413 `K9O_RAMPART_INPUT_TOO_LONG`) when the input needs more than 16 windows.
        A 200 is counted against the project's usage; a 503 and a 413 are not.
        """
        slug = _require_project_slug(self._config, "rampart")
        if not isinstance(input, Mapping):
            raise _invalid("input must be a dict")
        body: dict[str, Any] = {"input": dict(input)}
        if label is not None:
            if not isinstance(label, str) or not 1 <= len(label) <= 128:
                raise _invalid("label must be a string of 1 to 128 characters")
            body["label"] = label
        try:
            env = request(self._config, method="POST", path=project_path(slug, "rampart/classify"), body=body)
        except KError as e:
            if e.code == "K9O_RAMPART_INPUT_TOO_LONG" and not isinstance(e, RampartInputTooLong):
                raise RampartInputTooLong(e) from e
            raise
        data = env.data
        if (not isinstance(data, dict) or data.get("classification") not in _CLASSIFICATIONS
                or not isinstance(data.get("classificationReceipt"), str)):
            raise KError(code="K9O_INTERNAL_ERROR", message="Malformed Rampart classification response",
                         context={}, status=200)
        project_id = data.get("projectId")
        return ClassifyResult(
            classification=data["classification"], risk_score=data.get("riskScore"), confidence=data.get("confidence"),
            categories=list(data.get("categories") or []), calibration_version=data.get("calibrationVersion"),
            classification_receipt=data["classificationReceipt"],
            project_id=project_id if isinstance(project_id, str) and project_id else None, meta=env.meta,
        )

    def jwks(self) -> Envelope:
        """GET /rampart/jwks, the public keys that sign receipts. `data` is `{"keys": [...], "issuer": ...}`; each key has a `kid`.
        `issuer` is the deployment's receipt issuer (`<issuer>/rampart`); it is absent from the data when the server
        does not advertise one.

        The route is public and has no project prefix. The answer is not cached here (`verify_receipt` keeps its own cache).
        """
        cfg = self._config
        retry = cfg.retry_config or RetryConfig()
        last: BaseException | None = None
        for attempt in range(retry.max_retries + 1):
            try:
                return self._fetch_jwks_once()
            except KError as e:
                last = e
                if attempt == retry.max_retries or not _is_retryable(e, "GET"):
                    raise
                delay = _compute_delay_ms(retry, attempt)
                if delay > 0:
                    time.sleep(delay / 1000)
        assert last is not None
        raise last

    def _fetch_jwks_once(self) -> Envelope:
        cfg = self._config
        url = f"{resolve_regional_origin(cfg)}/rampart/jwks"
        headers = _build_headers(cfg, None, None, authenticated=False)
        try:
            with httpx.Client(transport=cfg.transport, timeout=_effective_timeout(cfg, None)) as client:
                res = client.get(url, headers=headers)
        except (httpx.TransportError, httpx.TimeoutException) as e:
            raise KError(code="K9O_UPSTREAM_UNAVAILABLE", message=f"Network error reaching {url}: {e}",
                         context={"url": url}) from e
        try:
            parsed = res.json()
        except ValueError as e:
            raise KError(code="K9O_INTERNAL_ERROR", message=f"Failed to parse JSON response from {url}",
                         context={"url": url, "status": res.status_code}, status=res.status_code) from e
        if not 200 <= res.status_code < 300:
            if isinstance(parsed, dict) and "code" in parsed and "message" in parsed:
                raise with_request_id(kerror_from_wire(parsed, res.status_code), res.headers)
            raise KError(code="K9O_INTERNAL_ERROR", message=f"Upstream returned {res.status_code}",
                         context={"url": url, "status": res.status_code}, status=res.status_code,
                         request_id=res.headers.get("x-k9o-request-id") or None)
        if not isinstance(parsed, dict) or not isinstance(parsed.get("keys"), list):
            raise KError(code="K9O_INTERNAL_ERROR", message="Malformed key feed: expected {keys: [...]}",
                         context={"url": url}, status=res.status_code)
        feed: dict[str, Any] = {"keys": parsed["keys"]}
        if isinstance(parsed.get("issuer"), str) and parsed["issuer"]:
            feed["issuer"] = parsed["issuer"]
        return Envelope(data=feed, meta=meta_from_wire(parsed.get("meta")))

    def _key_for(self, kid: str, injected: dict[str, Mapping[str, Any]] | None) -> Mapping[str, Any]:
        if injected is not None:
            if kid in injected:
                return injected[kid]
            raise ReceiptRefusal("key_unknown", f"no key with kid {kid!r} in the key set you passed")
        if kid in self._keys:
            return self._keys[kid]
        # Unknown kid: refetch the feed once (the first fetch counts as that one).
        feed = self.jwks().data
        self._keys = _keys_of(feed)
        self._issuer = feed.get("issuer")
        if kid in self._keys:
            return self._keys[kid]
        raise ReceiptRefusal("key_unknown", f"no key with kid {kid!r} in the key feed")

    def verify_receipt(
        self,
        receipt: str | ClassifyResult,
        input: Mapping[str, Any],
        *,
        jwks: Mapping[str, Any] | list[Any] | None = None,
        issuer: str | None = None,
        project_id: str | None = None,
        clock_skew_s: float = DEFAULT_CLOCK_SKEW_S,
        clock: Callable[[], float] | None = None,
    ) -> VerifiedReceipt:
        """Verify `receipt` against the exact `input` you classified. Raises `ReceiptRefusal`; see the module notes.

        `receipt` is the `ClassifyResult` that `classify` returned, or a bare receipt string.

        Options: `jwks` a key set you already fetched (`{"keys": [...], "issuer": ...}` or a list of keys; no network
        is used then); `issuer` the expected `iss` (default: the `issuer` the key feed advertises; with neither, the
        call is refused with `issuer`); `project_id` the expected project id (default: the `project_id` of the
        `ClassifyResult` passed in; a bare receipt string with neither is refused with `project_unknown`); `clock_skew_s` (default 60); `clock` a
        function returning the current time in seconds (default `time.time`).
        """
        carried_project: str | None = None
        if isinstance(receipt, ClassifyResult):
            carried_project = receipt.project_id
            receipt = receipt.classification_receipt
        if not isinstance(receipt, str):
            raise ReceiptRefusal("malformed", "the receipt is not a string")
        parts = receipt.split(".")
        if len(parts) != 3 or not all(parts):
            raise ReceiptRefusal("malformed", "the receipt is not a three-part JWS")
        header = _json_object(_b64url(parts[0], "header"), "header")
        payload_raw = _b64url(parts[1], "claims")
        signature = _b64url(parts[2], "signature")
        kid = header.get("kid")
        if not isinstance(kid, str) or not kid:
            raise ReceiptRefusal("malformed", "the header has no kid")

        injected = _keys_of(jwks) if jwks is not None else None
        jwk = self._key_for(kid, injected)  # 1: key by kid, from the feed only
        feed_issuer = (jwks.get("issuer") if isinstance(jwks, Mapping) else None) if jwks is not None else self._issuer
        if header.get("alg") != "EdDSA":  # 2
            raise ReceiptRefusal("alg", "the header alg is not EdDSA")
        if header.get("typ") != RECEIPT_TYP:  # 3
            raise ReceiptRefusal("typ", f"the header typ is not {RECEIPT_TYP}")
        public = _public_key_bytes(jwk)
        if not _ed25519.verify(public, f"{parts[0]}.{parts[1]}".encode("ascii"), signature):
            raise ReceiptRefusal("signature", "the signature does not verify")
        claims = _json_object(payload_raw, "claims")

        expected_iss = issuer if issuer is not None else feed_issuer
        if not isinstance(expected_iss, str) or not expected_iss:
            raise ReceiptRefusal("issuer", "no issuer is advertised by the key feed and none was given")
        if claims.get("iss") != expected_iss:  # 4
            raise ReceiptRefusal("issuer", f"iss is not {expected_iss}")
        expected_project = project_id if project_id is not None else carried_project
        if expected_project is None:
            raise ReceiptRefusal("project_unknown",
                                 "the project id is unknown: pass the classify result or project_id=")
        if claims.get("project_id") != expected_project:
            raise ReceiptRefusal("project", "project_id is not this project")
        iat, exp = claims.get("iat"), claims.get("exp")
        if not _is_num(iat) or not _is_num(exp):
            raise ReceiptRefusal("malformed", "iat and exp must be numbers")
        now = (clock or time.time)()
        if iat > now + clock_skew_s:
            raise ReceiptRefusal("not_yet_valid", "iat is in the future")
        if exp < now - clock_skew_s:
            raise ReceiptRefusal("expired", "the receipt has expired")

        try:  # 5: recompute the digest from the input the caller holds
            digest = hashlib.sha256(canonicalize(input)).hexdigest()
        except ValueError as e:
            raise ReceiptRefusal("input_hash", f"the input cannot be canonicalised: {e}") from e
        claimed = claims.get("input_sha256")
        if not isinstance(claimed, str) or not hmac.compare_digest(claimed.encode(), digest.encode()):
            raise ReceiptRefusal("input_hash", "the receipt does not attest this input")

        categories = claims.get("categories")
        if (claims.get("classification") not in _CLASSIFICATIONS or not _is_int(claims.get("risk_bp"))
                or not _is_int(claims.get("confidence_bp")) or not isinstance(categories, list)
                or not all(isinstance(c, str) for c in categories)
                or not isinstance(claims.get("calibration_version"), str)):
            raise ReceiptRefusal("malformed", "the verdict claims are missing or have the wrong type")
        windows = claims.get("windows")  # unknown claims are ignored; this one is read when present
        if windows is not None and (not _is_int(windows) or windows < 1):
            raise ReceiptRefusal("malformed", "windows must be a whole number, 1 or more")
        return VerifiedReceipt(
            classification=claims["classification"], risk_score=claims["risk_bp"], confidence=claims["confidence_bp"],
            categories=list(categories), calibration_version=claims["calibration_version"],
            project_id=claims["project_id"], input_sha256=claimed, issuer=claims["iss"], issued_at=iat,
            expires_at=exp, key_id=kid, windows=windows,
        )
