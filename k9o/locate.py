"""
Locating the end user's region for the credential flows.

``region.py`` finds the org's regional API host. An end user can live in another region of the project, so password
sign-in first asks the project which region holds the address (``POST /@<project>/gate/locate {"email": ...}`` at
that regional base) and sends the password to the origin it names. The answer is the same for a known and an unknown
address, so it tells a caller nothing about who has an account.

Rules:
  - The located origin is used for that one attempt. It is not the client's base for other calls and is never cached
    as authority. The one thing kept is a bounded hint per refresh token, so that refresh and sign-out reach the
    region that issued the session; a stale hint gets a 401.
  - An origin is used only if it is ``https`` (``http`` only when the base is itself ``http`` on a loopback host), has
    no userinfo, port, path, query or fragment, and its host is exactly the base host's org, family and apex with the
    region code ``[a-z]{2}\\d{2}`` added or swapped in the service label, and that code equals the answer's
    ``region``. Anything else, or ``origin: null``, means the base is used. A password never goes to a host that
    fails this.
  - The gateway is the authority on regions; a wrong region is a 401.
"""

from __future__ import annotations

import re
from collections import OrderedDict
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from .errors import KError
from .region import resolve_regional_origin
from .url_grammar import parse_api_host, project_path

if TYPE_CHECKING:  # pragma: no cover
    from .http import HttpClientConfig

_REGION_CODE = re.compile(r"^[a-z]{2}[0-9]{2}\Z")
_HINT_LIMIT = 64


def _is_loopback(hostname: str) -> bool:
    h = hostname.lower()
    return h == "localhost" or h.endswith(".localhost") or h == "127.0.0.1" or h in ("::1", "[::1]")


def choose_located_origin(base: str, answer: Any) -> str:
    """The origin the credential goes to: the located one if it passes the rule, else ``base``. Pure; never raises.

    ``answer`` is the ``data`` of the locate envelope.
    """
    try:
        if not isinstance(answer, dict):
            return base
        region = answer.get("region")
        origin = answer.get("origin")
        if not isinstance(origin, str) or not isinstance(region, str) or _REGION_CODE.match(region) is None:
            return base
        b = urlsplit(base)
        u = urlsplit(origin)
        b_host = b.hostname
        u_host = u.hostname
        if b_host is None or u_host is None:
            return base
        if u.port is not None or u.username is not None or u.password is not None or "@" in u.netloc:
            return base
        if ":" in u.netloc.replace("[::1]", "") or u.query != "" or u.fragment != "" or "?" in origin or "#" in origin:
            return base
        if u.path not in ("", "/"):
            return base
        if u.scheme != "https" and not (
            u.scheme == "http" and b.scheme == "http" and _is_loopback(b_host) and _is_loopback(u_host)
        ):
            return base
        if u.scheme == b.scheme and u.netloc.lower() == b.netloc.lower():
            return f"{b.scheme}://{b.netloc.lower()}"
        frm = parse_api_host(b_host)
        to = parse_api_host(u_host)
        if frm is None or to is None or to.region != region:
            return base
        if frm.org_slug != to.org_slug or frm.family != to.family or frm.apex != to.apex:
            return base
        return f"{u.scheme}://{u_host}"
    except ValueError:
        return base


def locate_origin(config: "HttpClientConfig", project_slug: str, email: str) -> str:
    """Ask the project which region holds ``email`` and return the origin the credential goes to.

    Raises the door's own error (``KError``) on any non-2xx except 404; nothing is retried. A 404 means the
    project has no locate step, so the base is returned. The door call carries no ``Authorization`` or cookie header.
    """
    from .http import request  # late: http imports region, not this module

    base = resolve_regional_origin(config)
    try:
        env = request(
            config,
            method="POST",
            path=project_path(project_slug, "gate/locate"),
            body={"email": email},
            authenticated=False,
        )
    except KError as e:
        if e.status == 404:
            return base
        raise
    return choose_located_origin(base, env.data)


# refresh token -> origin that issued the session (a hint, never authority); bounded, oldest dropped first.
_hints: "OrderedDict[str, str]" = OrderedDict()


def remember_session_origin(refresh_token: Any, origin: str) -> None:
    """Remember which origin issued the session with this refresh token."""
    if not isinstance(refresh_token, str) or refresh_token == "":
        return
    _hints.pop(refresh_token, None)
    _hints[refresh_token] = origin
    while len(_hints) > _HINT_LIMIT:
        _hints.popitem(last=False)


def session_origin_hint(refresh_token: str) -> str | None:
    return _hints.get(refresh_token)


def forget_session_origin(refresh_token: str) -> None:
    _hints.pop(refresh_token, None)


def session_refresh_token(data: Any) -> str | None:
    """The refresh token of an auth answer's ``session``, or None."""
    session = data.get("session") if isinstance(data, dict) else None
    token = session.get("refreshToken") if isinstance(session, dict) else None
    return token if isinstance(token, str) else None


def rotate_session_origin(old_refresh_token: str, data: Any) -> None:
    """A refresh answers with a new refresh token and the old one stops working: the hint follows the session."""
    token = data.get("refreshToken") if isinstance(data, dict) else None
    origin = session_origin_hint(old_refresh_token)
    if isinstance(token, str) and token != "" and origin is not None:
        forget_session_origin(old_refresh_token)
        remember_session_origin(token, origin)
