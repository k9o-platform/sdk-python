"""
Function lifecycle namespace — `client.functions.*`.

Member-plane routes. Wire mapping:
  - upload_artifact(data, kind)   POST   /@<slug>/functions/artifacts  (raw bytes)
  - deploy(...)                   POST   /@<slug>/functions
  - list()                        GET    /@<slug>/functions
  - get(name)                     GET    /@<slug>/functions/<name>
  - delete(name)                  DELETE /@<slug>/functions/<name>
  - invoke(name, payload)         POST   /@<slug>/functions/<name>/invoke   body {payload, ...}
  - triggers.list(name)           GET    /@<slug>/functions/<name>/triggers
  - triggers.create(name, ...)    POST   /@<slug>/functions/<name>/triggers
  - triggers.update(name, id, ...) PATCH /@<slug>/functions/<name>/triggers/<id>
  - triggers.delete(name, id)     DELETE /@<slug>/functions/<name>/triggers/<id>   (204, no body)

A function's bytes are uploaded first (`upload_artifact` returns the content-addressed `artifactKey`); `deploy`
references that artifact.
"""

from __future__ import annotations

import re
from typing import Any, Literal
from urllib.parse import quote

from .approvals import _invalid
from .envelope import Envelope
from .http import HttpClientConfig, request
from .url_grammar import project_path


def _require_project_slug(config: HttpClientConfig) -> str:
    if not config.project_slug:
        raise ValueError("functions operations require project_slug bound on the client")
    return config.project_slug


_UNSET: Any = object()


def _clean(d: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in d.items() if v is not None}


_TRIGGER_EVENTS = ("insert", "update", "delete", "upsert")
_TRIGGER_COLLECTION_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]{0,127}$")


def _check_events(events: Any) -> list[str]:
    if (not isinstance(events, (list, tuple)) or not 1 <= len(events) <= 4
            or any(not isinstance(e, str) or e not in _TRIGGER_EVENTS for e in events)):
        raise _invalid("events must list 1 to 4 of insert, update, delete, upsert", events=events)
    return list(events)


def _require_name(value: Any, what: str) -> str:
    if not isinstance(value, str) or not value:
        raise _invalid(f"{what} must be a non-empty string")
    return quote(value, safe="")


class TriggersNamespace:
    """Event triggers: run a function when a record is inserted, updated, deleted or upserted in a collection.
    `client.functions.triggers`. Needs the project admin role. For triggers on a timetable see `client.schedules`."""

    def __init__(self, config: HttpClientConfig) -> None:
        self._config = config

    def _path(self, name: str, tail: str = "") -> str:
        fn = _require_name(name, "function name")
        return project_path(_require_project_slug(self._config), f"functions/{fn}/triggers{tail}")

    def list(self, name: str) -> Envelope:
        """Every trigger of the function, newest first. Not paginated."""
        return request(self._config, method="GET", path=self._path(name))

    def create(self, name: str, *, collection: str, events: list[str], filter: dict[str, Any] | None = None,
               enabled: bool | None = None, log_ok_invocations: bool | None = None) -> Envelope:
        """Create an event trigger (answers 201 with the trigger).

        `collection` is an existing collection; `events` lists 1 to 4 of `insert`, `update`, `delete`, `upsert`;
        `filter` is a policy-grammar predicate (omit it to fire on every event); `enabled` defaults to true;
        `log_ok_invocations` also records successful runs (default: only failures).
        """
        if not isinstance(collection, str) or not _TRIGGER_COLLECTION_RE.match(collection):
            raise _invalid("collection must match ^[a-zA-Z_][a-zA-Z0-9_]*$ (1 to 128 characters)", collection=collection)
        body: dict[str, Any] = {"collection": collection, "events": _check_events(events)}
        body.update(_clean({"filter": filter, "enabled": enabled, "logOkInvocations": log_ok_invocations}))
        return request(self._config, method="POST", path=self._path(name), body=body)

    def update(self, name: str, trigger_id: str, *, events: list[str] | None = None, filter: Any = _UNSET,
               enabled: bool | None = None, log_ok_invocations: bool | None = None) -> Envelope:
        """Change a trigger; send at least one field. `events` replaces the whole list. `filter` as a dict replaces
        the filter, `filter=None` clears it, leaving it out keeps it. `collection` cannot be changed."""
        patch: dict[str, Any] = {}
        if events is not None:
            patch["events"] = _check_events(events)
        if filter is not _UNSET:
            patch["filter"] = filter
        patch.update(_clean({"enabled": enabled, "logOkInvocations": log_ok_invocations}))
        if not patch:
            raise _invalid("patch needs at least one of: events, filter, enabled, log_ok_invocations")
        seg = _require_name(trigger_id, "trigger id")
        return request(self._config, method="PATCH", path=self._path(name, f"/{seg}"), body=patch)

    def delete(self, name: str, trigger_id: str) -> Envelope:
        """Delete a trigger. Answers 204 with no body, so `data` is None; deleting again is a 404."""
        seg = _require_name(trigger_id, "trigger id")
        return request(self._config, method="DELETE", path=self._path(name, f"/{seg}"))


_ARTIFACT_CONTENT_TYPES = {"wasm": "application/wasm", "tar": "application/x-tar"}


class FunctionsNamespace:
    def __init__(self, config: HttpClientConfig) -> None:
        self._config = config
        self.triggers = TriggersNamespace(config)

    def upload_artifact(self, data: bytes | bytearray | memoryview, kind: Literal["wasm", "tar"]) -> Envelope:
        """Upload a function artifact; `data` is `{artifactKey}` to pass to `deploy`.

        `POST /@<slug>/functions/artifacts` with the raw bytes: a WebAssembly module for the gateway tier (for the platform operator; a project's deploy to it is refused)
        (`kind="wasm"`, up to 16 MiB) or an OCI image archive for the sandbox tier (`kind="tar"`, up to 256 MiB).
        Uploading the same bytes again returns the same key.
        """
        slug = _require_project_slug(self._config)
        if kind not in _ARTIFACT_CONTENT_TYPES:
            raise ValueError('kind must be "wasm" or "tar"')
        return request(
            self._config,
            method="POST",
            path=project_path(slug, "functions/artifacts"),
            multipart={"content": bytes(data)},
            headers={"content-type": _ARTIFACT_CONTENT_TYPES[kind]},
        )

    def deploy(self, *, name: str, runtime: str, tier: str, artifact_key: str, version: str,
               description: str | None = None) -> Envelope:
        """Deploy or redeploy a function from an uploaded artifact. Returns the record with status `deploying`.

        Body keys are the gateway's: `name` (1 to 128 chars), `runtime` ("bun", "python", "go", ...),
        `tier` ("gateway" runs WebAssembly in the gateway; "sandbox" runs an OCI image archive),
        `artifactKey`, `version` (1 to 64 chars) and optional `description`.
        """
        slug = _require_project_slug(self._config)
        body = _clean({"name": name, "runtime": runtime, "tier": tier, "artifactKey": artifact_key,
                       "version": version, "description": description})
        return request(self._config, method="POST", path=project_path(slug, "functions"), body=body)

    def list(self, *, limit: int | None = None, cursor: str | None = None, status: str | None = None,
             tier: str | None = None, source: str | None = None) -> Envelope:
        """List deployed functions (cursor pagination)."""
        slug = _require_project_slug(self._config)
        return request(self._config, method="GET", path=project_path(slug, "functions"),
                       query=_clean({"limit": limit, "cursor": cursor, "status": status, "tier": tier,
                                     "source": source}))

    def get(self, name: str) -> Envelope:
        """Get a function by name."""
        slug = _require_project_slug(self._config)
        return request(self._config, method="GET", path=project_path(slug, f"functions/{quote(name, safe='')}"))

    def delete(self, name: str) -> Envelope:
        """Retire a function (the name enters a 24h cooldown)."""
        slug = _require_project_slug(self._config)
        return request(self._config, method="DELETE", path=project_path(slug, f"functions/{quote(name, safe='')}"))

    def invoke(self, name: str, payload: Any = None, *, background: bool | None = None,
               delegation: dict[str, Any] | None = None, on_behalf_of: str | None = None,
               confined_to: str | None = None) -> Envelope:
        """Invoke a function. `payload` is sent as the body's `payload` field and passed to the runtime verbatim.

        Optional controls (the gateway's `background`, `delegation`, `onBehalfOf`, `confinedTo`) are sent only
        when given. `payload=None` sends no `payload` key.
        """
        slug = _require_project_slug(self._config)
        body: dict[str, Any] = {}
        if payload is not None:
            body["payload"] = payload
        body.update(_clean({"background": background, "delegation": delegation, "onBehalfOf": on_behalf_of,
                            "confinedTo": confined_to}))
        return request(self._config, method="POST",
                       path=project_path(slug, f"functions/{quote(name, safe='')}/invoke"), body=body)
