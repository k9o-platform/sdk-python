"""
Custody namespace: `client.custody.*`, the read-only view of how a project's encryption keys are held.

Wire mapping (project plane):
  - custody.status()    GET /@<slug>/custody/status

`status()` answers `data == {"mode", "attached", "provider", "lastJob", "hsm"}` (see `CustodyStatus`):
  - `mode`: how the keys are held, for example `"PLATFORM_MANAGED"`.
  - `attached`: True when an external key manager is attached to the project.
  - `provider`: `"platform"`, `"external_kms"`, `"hsm"` or None.
  - `lastJob`: the latest custody change (`kind`, `status`, `completedAt`), or None.
  - `hsm`: None unless `provider` is `"hsm"`; then `vendor`, `clusterId`, `region`, `attestation` and `keys`.
    `attestation["status"]`, `attestation["verifiedAt"]` and `keys` are always None. A response without
    an `hsm` key is read as `hsm` None.

Needs the project admin or project auditor role. Other roles get 403, a caller who is not a project member gets 404,
and 502 `K9O_UPSTREAM_UNAVAILABLE` means the key manager could not answer for an attached project.
Changing custody is done in the Console, not here.
"""

from __future__ import annotations

from typing import Any, TypedDict

from .approvals import _require_project_slug
from .envelope import Envelope
from .http import HttpClientConfig, request
from .url_grammar import project_path


class CustodyLastJob(TypedDict):
    """The latest custody change."""

    kind: str  # "transition" | "external_transition"
    status: str  # "pending" | "running" | "completed" | "failed"
    completedAt: str | None  # ISO 8601, None until it finishes


class HsmAttestation(TypedDict):
    status: str | None  # always None
    verifiedAt: str | None  # always None


class HsmDetails(TypedDict):
    """Present when `provider` is `"hsm"`."""

    vendor: str | None  # "thales-luna" | "entrust-nshield" | "aws-cloudhsm" | None
    clusterId: str
    region: str
    attestation: HsmAttestation
    keys: list[dict[str, Any]] | None  # always None


class CustodyStatus(TypedDict):
    """What `custody.status()` answers with (`envelope.data`)."""

    mode: str
    attached: bool
    provider: str | None  # "platform" | "external_kms" | "hsm" | None
    lastJob: CustodyLastJob | None
    hsm: HsmDetails | None  # None unless provider is "hsm"


class CustodyNamespace:
    def __init__(self, config: HttpClientConfig) -> None:
        self._config = config

    def status(self) -> Envelope:
        """GET /@<slug>/custody/status. `data` is a `CustodyStatus`; a missing `hsm` is returned as None."""
        slug = _require_project_slug(self._config, "custody")
        env = request(self._config, method="GET", path=project_path(slug, "custody/status"))
        if isinstance(env.data, dict):
            env.data.setdefault("hsm", None)
        return env
