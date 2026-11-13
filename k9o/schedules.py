"""
Scheduled triggers namespace: `client.schedules.*`. A schedule runs a function on a cron timetable.

Wire mapping (project plane):
  - list(status=, function_name=, limit=, cursor=)   GET    /@<slug>/forge/schedules   (`function_name` is sent as is)
  - get(id)                                          GET    /@<slug>/forge/schedules/<id>
  - create(function_name, schedule_expression, ...)  POST   /@<slug>/forge/schedules
  - update(id, ...)                                  PATCH  /@<slug>/forge/schedules/<id>
  - delete(id)                                       DELETE /@<slug>/forge/schedules/<id>        (204, no body)
  - runs(id, limit=, cursor=)                        GET    /@<slug>/forge/schedules/<id>/runs
  - run_now(id)                                      POST   /@<slug>/forge/schedules/<id>/run-now  (202)

Lists are cursor-paginated, newest first: pass `meta.cursor.next` of one page as `cursor` of the next, until it is None.

An interval under 5 minutes, or `catchup_policy="run_all"`, can be refused with 403 `K9O_TIER_FEATURE_GATED`. Arguments are checked before any request; a bad value raises KError
`K9O_INVALID_FIELD_VALUE`.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from .approvals import _invalid, _require_project_slug, check_limit
from .envelope import Envelope
from .http import HttpClientConfig, request
from .url_grammar import project_path

_CATCHUP_POLICIES = ("skip", "run_once", "run_all")
_STATUSES = ("active", "paused")


def _clean(d: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in d.items() if v is not None}


def _check_enum(value: Any, allowed: tuple[str, ...], what: str) -> None:
    if value is not None and value not in allowed:
        raise _invalid(f"{what} must be one of {', '.join(allowed)}", **{what: value})


class SchedulesNamespace:
    def __init__(self, config: HttpClientConfig) -> None:
        self._config = config

    def _path(self, tail: str = "") -> str:
        return project_path(_require_project_slug(self._config, "schedules"), "forge/schedules" + tail)

    def _one(self, id: Any, tail: str = "") -> str:
        if not isinstance(id, str) or not id:
            raise _invalid("schedule id must be a non-empty string")
        return self._path("/" + quote(id, safe="") + tail)

    def list(self, *, status: str | None = None, function_name: str | None = None, limit: int | None = None,
             cursor: str | None = None) -> Envelope:
        """List schedules, newest first, one page at a time. `status` is `active` or `paused`; `function_name`
        keeps only the schedules of that function; `limit` is 1 to 200 (default 50)."""
        _check_enum(status, _STATUSES, "status")
        check_limit(limit)
        query = _clean({"status": status, "function_name": function_name, "limit": limit, "cursor": cursor})
        return request(self._config, method="GET", path=self._path(), query=query or None)

    def get(self, id: str) -> Envelope:
        """One schedule."""
        return request(self._config, method="GET", path=self._one(id))

    def create(self, *, function_name: str, schedule_expression: str, schedule_timezone: str | None = None,
               catchup_policy: str | None = None, collection: str | None = None, enabled: bool | None = None,
               log_ok_invocations: bool | None = None) -> Envelope:
        """Create a schedule (answers 201 with the schedule).

        `schedule_expression` is a five-field cron or `@yearly`, `@annually`, `@monthly`, `@weekly`, `@daily`,
        `@midnight`, `@hourly` (stored and returned expanded). `schedule_timezone` is `UTC` (default) or `Region/City`.
        `catchup_policy` is `skip` (default), `run_once` or `run_all`. `collection` is passed to the function with each run.
        """
        _check_enum(catchup_policy, _CATCHUP_POLICIES, "catchup_policy")
        body = _clean({
            "functionName": function_name,
            "scheduleExpression": schedule_expression,
            "scheduleTimezone": schedule_timezone,
            "catchupPolicy": catchup_policy,
            "collection": collection,
            "enabled": enabled,
            "logOkInvocations": log_ok_invocations,
        })
        return request(self._config, method="POST", path=self._path(), body=body)

    def update(self, id: str, *, schedule_expression: str | None = None, schedule_timezone: str | None = None,
               catchup_policy: str | None = None, status: str | None = None, enabled: bool | None = None,
               log_ok_invocations: bool | None = None) -> Envelope:
        """Change a schedule; send at least one field. `status="paused"` stops it, `"active"` resumes it.
        The function cannot be changed."""
        _check_enum(catchup_policy, _CATCHUP_POLICIES, "catchup_policy")
        _check_enum(status, _STATUSES, "status")
        patch = _clean({
            "scheduleExpression": schedule_expression,
            "scheduleTimezone": schedule_timezone,
            "catchupPolicy": catchup_policy,
            "status": status,
            "enabled": enabled,
            "logOkInvocations": log_ok_invocations,
        })
        if not patch:
            raise _invalid("patch needs at least one of: schedule_expression, schedule_timezone, catchup_policy, "
                           "status, enabled, log_ok_invocations")
        return request(self._config, method="PATCH", path=self._one(id), body=patch)

    def delete(self, id: str) -> Envelope:
        """Delete a schedule. Answers 204 with no body, so `data` is None."""
        return request(self._config, method="DELETE", path=self._one(id))

    def runs(self, id: str, *, limit: int | None = None, cursor: str | None = None) -> Envelope:
        """The schedule's runs, newest first, one page at a time."""
        check_limit(limit)
        query = _clean({"limit": limit, "cursor": cursor})
        return request(self._config, method="GET", path=self._one(id, "/runs"), query=query or None)

    def run_now(self, id: str) -> Envelope:
        """Start one run now, outside the timetable. Answers 202; `data` is returned as the gateway sent it."""
        return request(self._config, method="POST", path=self._one(id, "/run-now"))
