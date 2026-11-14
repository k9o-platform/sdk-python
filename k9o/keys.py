"""
Keys namespace: `client.keys.*`, the inventory of a project's encryption keys and key rotation.

Wire mapping (project plane):
  - keys.list(status=)       GET  /@<slug>/vault/keys[?status=all]
  - keys.get(key_id)         GET  /@<slug>/vault/keys/<key id>
  - keys.rotate(key_id)      POST /@<slug>/vault/keys/<key id>/rotate

A key is `{"id", "version", "status", "custodyMode", "createdAt", "rotatedAt", "byokRef", "hyokRef"}` (see `VaultKey`):
metadata only, never key material and never the identifier of your external key. `byokRef` is set for a key you
bring (BYOK), `hyokRef` for a key held in your HSM cluster (HYOK); otherwise both are None. `externalKeyFingerprint`
is the first 16 characters of base64url(SHA-256) of your key's exact identifier, so you can match it to yours.

Reading needs the project admin or project auditor role; rotating needs the project admin role and a fresh step-up
proof. Only a person's credential works: end-user, extension, service and workload tokens are refused.

`rotate` is never retried by the SDK. Its refusals arrive as `KError`:
  - 401 `K9O_STEP_UP_REQUIRED` (raised as `StepUpRequired`): the person has no fresh step-up proof. The challenge is
    `error.step_up` (a `StepUpChallenge`; also `error.context["stepUp"]`). Give `rotate` a `step_up` hook and it answers
    the challenge and sends the call again, once; see `rotate`. A proof is used once.
  - 409 `K9O_STATE_CONFLICT`: `context["reason"]` is `key_not_active` or `rotation_in_flight`.
  - 429 `K9O_RATE_LIMITED`: `context["reason"]` is `rotation_rate_cap` (at most 3 rotations in any rolling 24 hours) or
    `key_service_busy`; `retry_after_sec` says when to try again.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, TypedDict
from urllib.parse import quote

from .approvals import _invalid, _require_project_slug
from .envelope import Envelope
from .errors import KError
from .http import HttpClientConfig, request
from .region import resolve_regional_origin
from .url_grammar import project_path


class ByokRef(TypedDict):
    provider: str
    region: str
    externalKeyFingerprint: str


class HyokRef(TypedDict):
    clusterId: str
    clusterName: str
    externalKeyFingerprint: str


class VaultKey(TypedDict):
    """One key version, what `keys.get` answers with and the items of `keys.list`."""

    id: str  # "vkey_<uuid>"
    version: int | None  # starts at 1, goes up by one on each rotation
    status: str | None  # "active" | "rotating" | "revoked"
    custodyMode: str | None  # "PLATFORM_MANAGED" | "SKR" | "BYOK" | "HYOK"
    createdAt: str | None
    rotatedAt: str | None
    byokRef: ByokRef | None
    hyokRef: HyokRef | None


class KeyRotation(TypedDict):
    """What `keys.rotate` answers with."""

    keyId: str
    previousVersion: int | None
    newVersion: int | None
    status: str | None  # "rotated"


STEP_UP_HEADER = "x-k9o-step-up-proof"


@dataclass(frozen=True)
class StepUpChallenge:
    """The step-up challenge of a 401 `K9O_STEP_UP_REQUIRED` (`context.stepUp`)."""

    scope_set_root: str
    nonce: str
    methods: tuple[str, ...]  # offered proof methods: "webauthn", "dpop", "mtls"
    max_age_sec: int | None = None  # how long the challenge lives (120)
    rp_id: str | None = None  # the relying-party id, when the method needs one
    min_tier: str | None = None  # "T3_KEY_POSSESSION"
    single_use: bool = True


@dataclass(frozen=True)
class StepUpProof:
    """What a step-up hook returns: one of the challenge's `methods` and that method's own proof string."""

    method: str
    proof: str = field(repr=False)  # never shown in a repr or a log


# A hook is called as `hook(challenge, http_method, url)` and returns a `StepUpProof` or `{"method", "proof"}`.
StepUpHook = Callable[[StepUpChallenge, str, str], "StepUpProof | Mapping[str, Any]"]


class StepUpRequired(KError):
    """401 `K9O_STEP_UP_REQUIRED`. `step_up` is the typed challenge (None if the gateway sent none)."""

    def __init__(self, source: KError) -> None:
        super().__init__(code=source.code, message=source.message, context=dict(source.context),
                         recommendation=source.recommendation, status=source.status, request_id=source.request_id,
                         retry_after_sec=source.retry_after_sec)
        self.step_up = _challenge_of(source)


def _challenge_of(error: KError) -> StepUpChallenge | None:
    raw = error.context.get("stepUp")
    if not isinstance(raw, Mapping):
        return None
    root, nonce = raw.get("scopeSetRoot"), raw.get("nonce")
    if not isinstance(root, str) or not isinstance(nonce, str):
        return None
    methods = raw.get("methods")
    age = raw.get("maxAgeSec")
    rp = raw.get("rpId")
    return StepUpChallenge(
        scope_set_root=root, nonce=nonce,
        methods=tuple(m for m in methods if isinstance(m, str)) if isinstance(methods, list) else (),
        max_age_sec=age if isinstance(age, int) and not isinstance(age, bool) else None,
        rp_id=rp if isinstance(rp, str) else None,
        min_tier=raw.get("minTier") if isinstance(raw.get("minTier"), str) else None,
        single_use=raw.get("singleUse") is not False)


def _proof_header(challenge: StepUpChallenge, answer: Any) -> str:
    """base64url (no padding) of the JSON `{scopeSetRoot, nonce, method, proof}`; echoes the challenge's root and nonce."""
    if isinstance(answer, StepUpProof):
        method, proof = answer.method, answer.proof
    elif isinstance(answer, Mapping):
        method, proof = answer.get("method"), answer.get("proof")
    else:
        raise _invalid("the step_up hook must return a StepUpProof or {'method', 'proof'}")
    if not isinstance(method, str) or not method or not isinstance(proof, str) or not proof:
        raise _invalid("the step_up hook must return a method and a non-empty proof string")
    if challenge.methods and method not in challenge.methods:
        raise _invalid("the step_up hook answered with a method the challenge did not offer",
                       method=method, offered=list(challenge.methods))
    body = json.dumps({"scopeSetRoot": challenge.scope_set_root, "nonce": challenge.nonce, "method": method,
                       "proof": proof}, separators=(",", ":"), ensure_ascii=False)
    return base64.urlsafe_b64encode(body.encode("utf-8")).rstrip(b"=").decode("ascii")


def _key_id(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise _invalid("key_id must be a non-empty string (vkey_...)")
    return quote(value, safe="")


class KeysNamespace:
    def __init__(self, config: HttpClientConfig) -> None:
        self._config = config

    def _path(self, tail: str) -> str:
        return project_path(_require_project_slug(self._config, "keys"), f"vault/keys{tail}")

    def list(self, *, status: str | None = None) -> Envelope:
        """The project's keys. `data == {"items": [VaultKey, ...]}`.

        The list is not paged (there is no cursor): by default only the active key is listed; `status="all"` adds the
        rotating and revoked versions. A `nextCursor` the gateway may still send is dropped.
        """
        if status is not None and status not in ("active", "all"):
            raise _invalid('status must be "active" or "all"', status=status)
        env = request(self._config, method="GET", path=self._path(""),
                      query={"status": status} if status is not None else None)
        if isinstance(env.data, dict) and "nextCursor" in env.data:
            env = Envelope(data={k: v for k, v in env.data.items() if k != "nextCursor"}, meta=env.meta)
        return env

    def get(self, key_id: str) -> Envelope:
        """One key by id (`vkey_<uuid>`). `data` is a `VaultKey`; 404 `K9O_NOT_FOUND` when the project has no such key."""
        return request(self._config, method="GET", path=self._path(f"/{_key_id(key_id)}"))

    def rotate(self, key_id: str, *, step_up: StepUpHook | None = None) -> Envelope:
        """Rotate the project's active key. `data` is a `KeyRotation`. Project admin only, with a fresh step-up proof.

        A new version is created, every data key is re-wrapped under it and the old version is revoked; if the re-wrap
        fails nothing changes. Writes to the project's encrypted data wait while it runs. See the module notes for
        the refusals (`K9O_STEP_UP_REQUIRED`, `K9O_STATE_CONFLICT`, `K9O_RATE_LIMITED`).

        The gateway first answers 401 `K9O_STEP_UP_REQUIRED` with a challenge. `step_up` is your hook for it: it is called
        as `step_up(challenge, http_method, url)` with the `StepUpChallenge` (`scope_set_root`, `nonce`, `methods`,
        `max_age_sec`, `rp_id`) and the method and URL being called (a DPoP proof binds to both), and returns a
        `StepUpProof(method, proof)` or `{"method": ..., "proof": ...}` where `method` is one of `challenge.methods`
        (`webauthn`, `dpop`, `mtls`) and `proof` is that method's own proof. The SDK sends the same request again with
        the `x-k9o-step-up-proof` header, exactly once, and never logs the proof. No route mints a proof for you.
        Without a hook the `StepUpRequired` error is raised (challenge at `error.step_up`). A second 401, or an
        exception from your hook, ends the call; nothing loops. A bad proof is a 401 `K9O_STEP_UP_PROOF_MALFORMED`.
        """
        path = self._path(f"/{_key_id(key_id)}/rotate")
        try:
            return request(self._config, method="POST", path=path)
        except KError as first:
            if first.code != "K9O_STEP_UP_REQUIRED" or first.status != 401:
                raise
            challenge = _challenge_of(first)
            if step_up is None or challenge is None:
                raise StepUpRequired(first) from first
            url = f"{resolve_regional_origin(self._config)}{path}"
            header = _proof_header(challenge, step_up(challenge, "POST", url))
        try:
            return request(self._config, method="POST", path=path, headers={STEP_UP_HEADER: header})
        except KError as second:
            if second.code == "K9O_STEP_UP_REQUIRED":
                raise StepUpRequired(second) from second
            raise
