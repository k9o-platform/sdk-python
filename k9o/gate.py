"""
Gate (Identity-Aware Kernel) namespace — `client.gate.*`.

Python idiomatic shape: snake_case methods (sign_up / sign_in / sign_out /
whoami / refresh_token) + raise KError on error.
"""

from __future__ import annotations

from typing import Any

from .envelope import Envelope
from .errors import KError
from .http import HttpClientConfig, request
from .locate import (
    forget_session_origin,
    locate_origin,
    remember_session_origin,
    rotate_session_origin,
    session_origin_hint,
    session_refresh_token,
)
from .url_grammar import project_path


class GateNamespace:
    """`client.gate.*` — Gate auth surface.

    Requires `project_slug` bound on the client (project-scoped auth).
    """

    def __init__(self, config: HttpClientConfig) -> None:
        self._config = config

    def _require_project(self) -> str:
        if not self._config.project_slug:
            raise KError(
                code="K9O_MISSING_PROJECT_CONTEXT",
                message="client.gate operations require project_slug bound on the client",
                context={},
                recommendation="Pass project_slug= in create_client().",
            )
        return self._config.project_slug

    def sign_up(self, *, email: str, password: str, display_name: str | None = None) -> Envelope:
        """Create a new end-user identity for the configured project."""
        slug = self._require_project()
        body: dict[str, Any] = {"email": email, "password": password}
        if display_name is not None:
            body["displayName"] = display_name
        return request(self._config, method="POST", path=project_path(slug, "gate/signup"), body=body)

    def sign_in(self, *, email: str, password: str) -> Envelope:
        """Sign in an existing end-user with email + password.

        Asks the project which region holds the email first (one request carrying only the email), then sends the
        password to that region's host when it is a region host of your own org and apex, else to the base URL. A 429
        or 503 from that first step is raised as the usual `KError` and the password is not sent; it is safe to retry.
        """
        slug = self._require_project()
        origin = locate_origin(self._config, slug, email)
        out = request(
            self._config,
            method="POST",
            path=project_path(slug, "gate/signin"),
            body={"email": email, "password": password},
            origin=origin,
        )
        remember_session_origin(session_refresh_token(out.data), origin)
        return out

    def sign_out(self, *, refresh_token: str) -> Envelope:
        """Sign out the session by revoking its refresh token (the gateway requires `refreshToken`; a sign-out
        without one is a 400)."""
        slug = self._require_project()
        out = request(self._config, method="POST", path=project_path(slug, "gate/signout"),
                      body={"refreshToken": refresh_token}, origin=session_origin_hint(refresh_token))
        forget_session_origin(refresh_token)
        return out

    def whoami(self) -> Envelope:
        """Print the active end-user identity."""
        slug = self._require_project()
        return request(self._config, method="GET", path=project_path(slug, "gate/whoami"))

    def revoke_all_sessions(self) -> Envelope:
        """End every live session of the signed-in end user, on every device, this one included
        (`POST /@<project>/auth/sessions/revoke-all`).

        Call it with the end user's access token as the client's bearer token (the token `whoami` uses), not a
        refresh token. The answer's `data` is `{revokedCount}`. Each ended session's next refresh raises `KError`
        with code `K9O_SESSION_EXPIRED` and `context["reason"]` of `"revoked"`; access tokens already issued keep
        working until they expire. An operator's token raises `K9O_TRUST_ZONE_VIOLATION` (403), and a token issued
        for another project raises `K9O_PROJECT_SCOPE_MISMATCH` (403).
        """
        slug = self._require_project()
        return request(self._config, method="POST", path=project_path(slug, "auth/sessions/revoke-all"))

    def refresh_token(self, *, refresh_token: str) -> Envelope:
        """Exchange the refresh token for a new access token and a new refresh token.

        The answer's `data` is `{accessToken, refreshToken, expiresAt}`. The refresh token you sent stops working:
        store the returned `refreshToken` before the next refresh. Refresh one at a time per session. A second call
        with a secret that was already used is a replay: the session is revoked and the call raises `KError` with code
        `K9O_SESSION_EXPIRED` and `context["reason"]` of `"replay"`. After that the newer secret gets `"revoked"`.
        The other reasons are `"inactive"` and `"max_length"`; sign in again.
        """
        slug = self._require_project()
        out = request(
            self._config,
            method="POST",
            path=project_path(slug, "gate/refresh"),
            body={"refreshToken": refresh_token},
            origin=session_origin_hint(refresh_token),
        )
        rotate_session_origin(refresh_token, out.data)
        return out
