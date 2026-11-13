"""
`create_client(...)` factory + `K9oClient`.

Canonical usage:

    from k9o import create_client, KError

    k9o = create_client(
        base_url="https://acme.api.k9o.io",
        api_key=os.environ["K9O_API_KEY"],
        org_slug="acme",
        project_slug="member-portal",
    )

    try:
        response = k9o.from_("posts").select().limit(10).execute()
        for row in response.data:
            print(row)
        if response.meta.cursor:
            print("next cursor:", response.meta.cursor.next)
    except KError as e:
        print(f"[{e.code}] {e.message}: {e.recommendation}")
        raise
"""

from __future__ import annotations

import dataclasses
from typing import Sequence

import httpx

from .ai import AiNamespace
from .approvals import ApprovalPoliciesNamespace, ApprovalsNamespace
from .analytics import AnalyticsNamespace, RollupsNamespace
from .audit import AuditNamespace
from .crypto import CryptoNamespace, check_expected_key_refs
from .compute import ComputeNamespace
from .data import FromBuilder
from .end_users import EndUsersNamespace
from .functions import FunctionsNamespace
from .gate import GateNamespace
from .http import HttpClientConfig, RetryConfig
from .keys import KeysNamespace
from .kibo_vault import KiboNamespace
from .me import MeNamespace
from .metadata import MetadataNamespace
from .migrations import MigrationsNamespace
from .org import OrgNamespace
from .orgs import OrgsNamespace
from .policies import PoliciesNamespace
from .projects import ProjectsNamespace
from .query import QueryMixin
from .rampart import RampartNamespace
from .realtime import RealtimeNamespace
from .schedules import SchedulesNamespace
from .schema import SchemaNamespace
from .streams import StreamsNamespace
from .storage import StorageNamespace
from .studio import StudioNamespace
from .url_grammar import resolve_base_url
from .vectors import VectorsNamespace
from .custody import CustodyNamespace


class K9oClient(QueryMixin):
    """The client returned by `create_client(...)`.

    Namespaces hang off the instance — composed from per-resource
    factories sharing the bound HTTP config.

    Attributes:
      projects  — `client.projects.list()` / `.create(display_name=...)` / `.get(slug)` / `.update(slug, ...)`
      gate      — `client.gate.sign_in(...)` / `.sign_up(...)` / `.whoami()` / etc. (requires project_slug)
      kibo      — `client.kibo.vault.secrets.{list,get,create,reveal_challenge,reveal,update,rotate,move,delete}`
                  (ZK vault-secrets surface; requires project_slug)
      storage   — `client.storage.{create_bucket,get_bucket,list_buckets,update_bucket,delete_bucket}` and
                  `client.storage.from_(bucket).{upload,create_signed_url,list,remove}` (each with an `_async`
                  twin; requires project_slug)

    Methods:
      from_(collection) — data-plane DSL; returns a FromBuilder
      query(sql, params=None) — one read-only SQL statement; list params are positional, dict params are named
      search(collection, column=..., q=...) — full-text search over one text column
    """

    def __init__(self, config: HttpClientConfig) -> None:
        self._config = config
        self.projects = ProjectsNamespace(config)
        self.gate = GateNamespace(config)
        self.migrations = MigrationsNamespace(config)
        self.functions = FunctionsNamespace(config)
        self.policies = PoliciesNamespace(config)
        self.audit = AuditNamespace(config)
        self.analytics = AnalyticsNamespace(config)
        self.rollups = RollupsNamespace(config)
        self.keys = KeysNamespace(config)
        self.crypto = CryptoNamespace(config)
        self.orgs = OrgsNamespace(config)
        self.me = MeNamespace(config)
        self.studio = StudioNamespace(config)
        self.compute = ComputeNamespace(config)
        self.ai = AiNamespace(config)
        self.realtime = RealtimeNamespace(config)
        self.streams = StreamsNamespace(config)
        self.approvals = ApprovalsNamespace(config)
        self.approval_policies = ApprovalPoliciesNamespace(config)
        self.schedules = SchedulesNamespace(config)
        self.end_users = EndUsersNamespace(config)
        self.vectors = VectorsNamespace(config)
        self.custody = CustodyNamespace(config)
        self.rampart = RampartNamespace(config)
        self.metadata = MetadataNamespace(config)
        self.schema = SchemaNamespace(config)
        self.org = OrgNamespace(config)
        self.kibo = KiboNamespace(config)
        self.storage = StorageNamespace(config)

    @property
    def config(self) -> HttpClientConfig:
        """Bound config (read-only access for advanced use)."""
        return self._config

    def with_options(self, *, timeout: float | tuple[float, float] | httpx.Timeout | None = None) -> "K9oClient":
        """A client with the same settings and another network timeout (per-call override for any method).

        ``client.with_options(timeout=300).storage.create_bucket(...)``. The new client shares the learned regional origin.
        """
        config = dataclasses.replace(self._config, timeout=timeout)
        return K9oClient(config)

    def from_(self, collection: str) -> FromBuilder:
        """Data-plane DSL entry point. `from` is a Python keyword so we use `from_`."""
        return FromBuilder(self._config, collection)


def create_client(
    *,
    base_url: str | None = None,
    api_key: str | None = None,
    org_slug: str | None = None,
    project_slug: str | None = None,
    default_headers: dict[str, str] | None = None,
    retry_config: RetryConfig | None = None,
    timeout: float | tuple[float, float] | httpx.Timeout | None = None,
    expected_key_refs: Sequence[str] | None = None,
) -> K9oClient:
    """Construct a K9oClient bound to the supplied gateway baseUrl + auth + context.

    base_url is OPTIONAL: when omitted it defaults to the public cloud
    ('https://api.k9o.io', a convenience only). The host is never matched
    against a list of known domains: when it is a platform host
    ('api.<apex>' or 'api-staging.<apex>', any apex) and org_slug is set, the
    SDK composes the org host '<orgSlug>.<host>'. Any other host (an org
    host, an internal or air-gapped host, localhost) is used unchanged.
    Validates base_url at construct time — path-bearing URLs raise
    KError(K9O_INVALID_BASE_URL).

    Args:
      base_url: optional scheme + host (e.g. 'https://acme.api.corp.internal'
        or 'https://api.corp.internal'); defaults to the public cloud
      api_key: bearer token for default auth (per-call override is supported)
      org_slug: active org context; sent as 'x-k9o-org-slug' header
      project_slug: active project context; sent as 'x-k9o-project-slug' header
      default_headers: headers merged into every request
      retry_config: retry/backoff policy; None ⇒ defaults
        (3 retries, exponential) on idempotent methods + transient failures
      timeout: network timeout in seconds; None ⇒ 10 s connect, 120 s read. A number sets the read
        (and write) timeout and leaves connect at 10; a ``(connect, read)`` pair or an ``httpx.Timeout``
        sets both. The read value is per read (idle), so a stream that keeps sending is never cut off.
        Methods that wait on a model accept a per-call ``timeout`` too, and ``client.with_options(timeout=...)``
        returns a client with another timeout for any method.
      expected_key_refs: the key identifiers (ARNs, resource names, key URLs) your own KMS or HSM keys have. Set it
        when the project uses BYOK or HYOK: the client core refuses, before it uses any token or makes any network
        call, to work with a key that is not in this list. The key version is ignored in the comparison. Take the
        values from your own cloud configuration, never from the platform, and scope the cloud role to those keys,
        never ``*``. Must not be empty when given; no entry may be blank, contain whitespace or ``*``.
    """
    resolved = resolve_base_url(base_url, org_slug)
    config = HttpClientConfig(
        base_url=resolved,
        api_key=api_key,
        org_slug=org_slug,
        project_slug=project_slug,
        default_headers=dict(default_headers or {}),
        retry_config=retry_config,
        timeout=timeout,
        expected_key_refs=check_expected_key_refs(expected_key_refs),
    )
    return K9oClient(config)
