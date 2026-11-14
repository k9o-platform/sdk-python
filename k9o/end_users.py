"""
End users namespace: `client.end_users.*`, the people who sign in to the project's own apps.

Wire mapping (project plane):
  - list(status=, q=, limit=, cursor=)      GET    /@<slug>/end-users
  - get(id)                                 GET    /@<slug>/end-users/<id>
  - role_grants.list(end_user_id=)          GET    /@<slug>/end-user-role-grants
  - role_grants.grant(end_user_id, role)    POST   /@<slug>/end-user-role-grants
  - role_grants.revoke(grant_id)            DELETE /@<slug>/end-user-role-grants/<grant_id>

Listing and reading end users needs the project admin role. Role grants are created and revoked by project admins;
reading them is open to a wider set of project roles. Arguments are checked before any request; a bad value raises
KError `K9O_INVALID_FIELD_VALUE`.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from .approvals import _invalid, _require_project_slug, check_limit
from .envelope import Envelope
from .http import HttpClientConfig, request
from .url_grammar import project_path

_STATUSES = ("active", "locked", "deleted", "all")


def _require_text(value: Any, what: str, maximum: int) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise _invalid(f"{what} must be a string of 1 to {maximum} characters")
    return value


class RoleGrantsNamespace:
    def __init__(self, config: HttpClientConfig) -> None:
        self._config = config

    def _path(self, tail: str = "") -> str:
        return project_path(_require_project_slug(self._config, "end_users"), "end-user-role-grants" + tail)

    def list(self, *, end_user_id: str | None = None) -> Envelope:
        """The active grants, oldest first. Not paginated. `data` is the list of grants (the `grants` wrapper the
        gateway sends is removed). An unknown `end_user_id` answers an empty list."""
        query = None if end_user_id is None else {"endUserId": end_user_id}
        r = request(self._config, method="GET", path=self._path(), query=query)
        return Envelope(data=r.data["grants"], meta=r.meta)

    def grant(self, end_user_id: str, role: str) -> Envelope:
        """Give an end user a role (`data` is the grant). `role` is any string, 1 to 128 characters.

        Granting a role the user already holds is a 400 `K9O_INVALID_FIELD_VALUE`; an unknown end user is a 404
        `K9O_NOT_FOUND`. A revoked role can be granted again.
        """
        body = {"endUserId": _require_text(end_user_id, "end_user_id", 64), "role": _require_text(role, "role", 128)}
        r = request(self._config, method="POST", path=self._path(), body=body)
        return Envelope(data=r.data["grant"], meta=r.meta)

    def revoke(self, grant_id: str) -> Envelope:
        """Revoke a grant (it is kept, marked revoked). `data` is `{"revoked": bool, "revokedAt": str | None}`.
        An unknown or already revoked id is not an error: the answer is `revoked: False`."""
        seg = quote(_require_text(grant_id, "grant_id", 256), safe="")
        return request(self._config, method="DELETE", path=self._path("/" + seg))


class EndUsersNamespace:
    def __init__(self, config: HttpClientConfig) -> None:
        self._config = config
        self.role_grants = RoleGrantsNamespace(config)

    def _path(self, tail: str = "") -> str:
        return project_path(_require_project_slug(self._config, "end_users"), "end-users" + tail)

    def list(self, *, status: str | None = None, q: str | None = None, limit: int | None = None,
             cursor: str | None = None) -> Envelope:
        """List end users, newest first, one page at a time.

        `status` is `active`, `locked`, `deleted` or `all` (default: active and locked); `q` is an email prefix
        (case-sensitive, up to 320 characters); `limit` is 1 to 200 (default 50); `cursor` is `meta.cursor.next`.
        """
        if status is not None and status not in _STATUSES:
            raise _invalid(f"status must be one of {', '.join(_STATUSES)}", status=status)
        check_limit(limit)
        query = {k: v for k, v in {"status": status, "q": q, "limit": limit, "cursor": cursor}.items() if v is not None}
        return request(self._config, method="GET", path=self._path(), query=query or None)

    def get(self, id: str) -> Envelope:
        """One end user. An unknown id is a 404 `K9O_END_USER_NOT_FOUND`."""
        seg = quote(_require_text(id, "end user id", 64), safe="")
        return request(self._config, method="GET", path=self._path("/" + seg))
