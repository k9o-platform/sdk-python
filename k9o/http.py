"""
HTTP client for the k9o SDK — httpx-based; sync + async.

Python idiomatic shape: returns Envelope on 2xx;
raises KError on non-2xx + transport failures + malformed envelopes.
Field-name translation at the boundary: wire camelCase JSON ↔ Python
snake_case attributes on dataclasses (Meta accessor sugar).
"""

from __future__ import annotations

import asyncio
import json
import random
import re
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, TypeVar

import httpx

from .envelope import Envelope, MetaCursor, meta_from_wire
from .errors import KError, kerror_from_wire, with_request_id
from .region import resolve_regional_origin, resolve_regional_origin_async

T = TypeVar("T")

DEFAULT_CONNECT_TIMEOUT_S = 10.0
DEFAULT_READ_TIMEOUT_S = 120.0

# `timeout` accepts a number (seconds, applied to read and write; connect stays 10), a ``(connect, read)``
# pair, or an ``httpx.Timeout`` for full control.

def resolve_timeout(timeout: Any = None) -> httpx.Timeout:
    """Normalise a timeout option to an ``httpx.Timeout``. ``None`` gives the defaults (10 s connect, 120 s read).

    The read value is per read (idle) and never a total deadline, so a stream that keeps sending data
    or keep-alive comments stays open.
    """
    if timeout is None:
        return httpx.Timeout(connect=DEFAULT_CONNECT_TIMEOUT_S, read=DEFAULT_READ_TIMEOUT_S,
                             write=DEFAULT_READ_TIMEOUT_S, pool=DEFAULT_CONNECT_TIMEOUT_S)
    if isinstance(timeout, httpx.Timeout):
        return timeout
    if isinstance(timeout, bool):
        raise TypeError("timeout must be a number of seconds, a (connect, read) pair or an httpx.Timeout")
    if isinstance(timeout, (int, float)):
        return httpx.Timeout(connect=DEFAULT_CONNECT_TIMEOUT_S, read=float(timeout),
                             write=float(timeout), pool=DEFAULT_CONNECT_TIMEOUT_S)
    if isinstance(timeout, (tuple, list)) and len(timeout) == 2:
        connect, read = timeout
        return httpx.Timeout(connect=connect, read=read, write=read, pool=connect)
    raise TypeError("timeout must be a number of seconds, a (connect, read) pair or an httpx.Timeout")

# Retry — idempotent methods only, transient failures only.
_RETRYABLE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "DELETE"})
_RETRYABLE_STATUS = frozenset({502, 503, 504})


@dataclass
class RetryConfig:
    """Retry + backoff config. Applied to idempotent methods on
    transient failures only (transport error / 502 / 503 / 504 / 429)."""

    max_retries: int = 3
    backoff: str = "exponential"  # "exponential" | "linear" | "none"
    initial_delay_ms: int = 100
    max_delay_ms: int = 5000


@dataclass
class HttpClientConfig:
    """Per-instance HTTP config."""

    base_url: str
    api_key: str | None = None
    project_slug: str | None = None
    org_slug: str | None = None
    default_headers: dict[str, str] = field(default_factory=dict)
    # Custom httpx transport for tests; if None, uses default.
    transport: httpx.BaseTransport | None = None
    # Retry config; None ⇒ the defaults (3 retries, exponential).
    retry_config: RetryConfig | None = None
    # Network timeout: number (read/write seconds), (connect, read) pair or httpx.Timeout; None ⇒ 10 s connect, 120 s read.
    timeout: Any = None
    # Key identifiers the client core may use for BYOK and HYOK (see `create_client(expected_key_refs=)`); None when not set.
    expected_key_refs: tuple[str, ...] | None = None
    # Learned regional origin cache (k9o/region.py); None until a definite probe answer.
    _resolved_origin: str | None = field(default=None, repr=False, compare=False)


def _effective_timeout(config: "HttpClientConfig", per_call: Any) -> httpx.Timeout:
    """The per-call timeout when given, else the client's, else the defaults."""
    return resolve_timeout(per_call if per_call is not None else config.timeout)


def _is_retryable(error: BaseException, method: str) -> bool:
    """A failure is retryable only on idempotent methods + transient causes."""
    if method.upper() not in _RETRYABLE_METHODS:
        return False
    if not isinstance(error, KError):
        return False
    if error.code == "K9O_UPSTREAM_UNAVAILABLE":
        return True
    if error.status in _RETRYABLE_STATUS:
        return True
    if error.status == 429:  # rate-limit
        return True
    return False


def _compute_delay_ms(config: RetryConfig, attempt: int) -> int:
    """Backoff delay for a 0-based attempt index, capped + ±25% jittered."""
    if config.backoff == "none":
        return 0
    if config.backoff == "linear":
        base = config.initial_delay_ms * (attempt + 1)
    else:
        base = config.initial_delay_ms * (2 ** attempt)
    capped = min(base, config.max_delay_ms)
    jitter = capped * 0.25 * (random.random() * 2 - 1)
    return max(0, int(capped + jitter))


def _build_headers(
    config: HttpClientConfig,
    init_headers: dict[str, str] | None,
    bearer: str | None,
    authenticated: bool = True,
) -> dict[str, str]:
    headers: dict[str, str] = {"accept": "application/json"}
    headers.update(config.default_headers)
    if config.project_slug:
        headers["x-k9o-project-slug"] = config.project_slug
    if config.org_slug:
        headers["x-k9o-org-slug"] = config.org_slug
    if init_headers:
        headers.update(init_headers)
    if not authenticated:
        # Credential-free call: no Authorization / cookie from any source.
        for k in [k for k in headers if k.lower() in ("authorization", "cookie")]:
            del headers[k]
        return headers
    token = bearer or config.api_key
    if token:
        headers["authorization"] = f"Bearer {token}"
    return headers


def _with_retry_after(error: KError, response: httpx.Response) -> KError:
    """Fill `retry_after_sec` from a numeric `Retry-After` header when the error body gave none."""
    with_request_id(error, response.headers)
    if error.retry_after_sec is None:
        raw = response.headers.get("retry-after")
        if raw is not None and raw.strip().isdigit():
            error.retry_after_sec = int(raw.strip())
    return error


def _handle_response(response: httpx.Response) -> Envelope[Any]:
    if response.status_code == 204:
        return Envelope(data=None, meta=meta_from_wire(None))

    try:
        parsed = response.json()
    except ValueError as e:
        raise KError(
            code="K9O_INTERNAL_ERROR",
            message=f"Failed to parse JSON response from {response.url}: {e}",
            context={"url": str(response.url), "status": response.status_code},
            status=response.status_code,
        ) from e

    if 200 <= response.status_code < 300:
        if not isinstance(parsed, dict) or "data" not in parsed or "meta" not in parsed:
            raise KError(
                code="K9O_INTERNAL_ERROR",
                message=f"Malformed envelope from {response.url}: expected {{data, meta}}",
                context={"url": str(response.url), "status": response.status_code},
                status=response.status_code,
            )
        meta = meta_from_wire(parsed.get("meta"))
        # A page that carries its cursor beside `data` (`nextCursor`) reads like the others: `meta.cursor.next`.
        top = parsed.get("nextCursor")
        if meta.cursor is None and (top is None or isinstance(top, str)) and "nextCursor" in parsed:
            meta.cursor = MetaCursor(next=top)
        return Envelope(data=parsed["data"], meta=meta)

    if isinstance(parsed, dict) and "code" in parsed and "message" in parsed:
        raise _with_retry_after(kerror_from_wire(parsed, response.status_code), response)
    raise KError(
        code="K9O_INTERNAL_ERROR",
        message=f"Upstream returned {response.status_code} with non-KError body",
        context={"url": str(response.url), "status": response.status_code, "body": parsed},
        status=response.status_code,
        request_id=response.headers.get("x-k9o-request-id") or None,
    )


def request(
    config: HttpClientConfig,
    *,
    method: str,
    path: str,
    body: Any = None,
    bearer: str | None = None,
    headers: dict[str, str] | None = None,
    query: dict[str, Any] | None = None,
    authenticated: bool = True,
    multipart: dict[str, Any] | None = None,
    timeout: Any = None,
    origin: str | None = None,
) -> Envelope[Any]:
    """Issue a request against the gateway, with retry on transient failures.

    `origin` sends to that origin instead of the client's regional base; only the end-user credential flows set it
    (k9o/locate.py), after its host rule.

    Returns Envelope on 2xx; raises KError on non-2xx, transport failure,
    or malformed response. Idempotent methods are retried with backoff per
    `config.retry_config`; non-idempotent / semantic failures
    raise immediately.
    """
    cfg = config.retry_config or RetryConfig()
    last_error: BaseException | None = None
    for attempt in range(cfg.max_retries + 1):
        try:
            return _request_once(
                config, method=method, path=path, body=body,
                bearer=bearer, headers=headers, query=query,
                authenticated=authenticated, multipart=multipart, timeout=timeout, origin=origin,
            )
        except KError as e:
            last_error = e
            if attempt == cfg.max_retries or not _is_retryable(e, method):
                raise
            delay_ms = _compute_delay_ms(cfg, attempt)
            if delay_ms > 0:
                time.sleep(delay_ms / 1000)
    assert last_error is not None  # loop always sets or returns
    raise last_error


def _request_once(
    config: HttpClientConfig,
    *,
    method: str,
    path: str,
    body: Any = None,
    bearer: str | None = None,
    headers: dict[str, str] | None = None,
    query: dict[str, Any] | None = None,
    authenticated: bool = True,
    multipart: dict[str, Any] | None = None,
    timeout: Any = None,
    origin: str | None = None,
) -> Envelope[Any]:
    """Single-attempt request. `request` wraps this in retry-with-backoff."""
    headers_built = _build_headers(config, headers, bearer, authenticated)
    # A multipart body carries its own content-type (with the boundary, set by httpx).
    if body is not None and multipart is None:
        headers_built["content-type"] = "application/json"

    url = f"{origin if origin is not None else resolve_regional_origin(config)}{path}"
    try:
        with httpx.Client(transport=config.transport, timeout=_effective_timeout(config, timeout)) as client:
            response = client.request(
                method=method,
                url=url,
                json=body if body is not None and multipart is None else None,
                **(multipart or {}),
                headers=headers_built,
                params=query,
            )
    except (httpx.TransportError, httpx.NetworkError, httpx.TimeoutException) as e:
        raise KError(
            code="K9O_UPSTREAM_UNAVAILABLE",
            message=f"Network error reaching {url}: {e}",
            context={"url": url},
            recommendation="Check connectivity to the gateway; retry with backoff if transient.",
        ) from e

    return _handle_response(response)


@dataclass
class RawResponse:
    """A non-JSON answer: the body bytes, the `content-type` and the file name from `content-disposition`."""

    content: bytes
    content_type: str | None = None
    filename: str | None = None


def request_raw(
    config: HttpClientConfig,
    *,
    method: str,
    path: str,
    query: dict[str, Any] | None = None,
    timeout: Any = None,
) -> RawResponse:
    """Issue a request whose 2xx answer is a file, not a JSON envelope. Errors arrive as KError, retried like `request`."""
    cfg = config.retry_config or RetryConfig()
    for attempt in range(cfg.max_retries + 1):
        try:
            return _request_raw_once(config, method=method, path=path, query=query, timeout=timeout)
        except KError as e:
            if attempt == cfg.max_retries or not _is_retryable(e, method):
                raise
            delay_ms = _compute_delay_ms(cfg, attempt)
            if delay_ms > 0:
                time.sleep(delay_ms / 1000)
    raise AssertionError("unreachable")


def _request_raw_once(config: HttpClientConfig, *, method: str, path: str, query: dict[str, Any] | None,
                      timeout: Any) -> RawResponse:
    headers = _build_headers(config, {"accept": "*/*"}, None, True)
    url = f"{resolve_regional_origin(config)}{path}"
    try:
        with httpx.Client(transport=config.transport, timeout=_effective_timeout(config, timeout)) as client:
            response = client.request(method=method, url=url, headers=headers, params=query)
    except (httpx.TransportError, httpx.NetworkError, httpx.TimeoutException) as e:
        raise KError(
            code="K9O_UPSTREAM_UNAVAILABLE",
            message=f"Network error reaching {url}: {e}",
            context={"url": url},
            recommendation="Check connectivity to the gateway; retry with backoff if transient.",
        ) from e
    if not 200 <= response.status_code < 300:
        try:
            parsed = response.json()
        except ValueError:
            parsed = None
        if isinstance(parsed, dict) and "code" in parsed and "message" in parsed:
            raise _with_retry_after(kerror_from_wire(parsed, response.status_code), response)
        raise KError(
            code="K9O_INTERNAL_ERROR",
            message=f"Upstream returned {response.status_code} with non-KError body",
            context={"url": url, "status": response.status_code},
            status=response.status_code,
            request_id=response.headers.get("x-k9o-request-id") or None,
        )
    filename = None
    match = re.search(r'filename="?([^";]+)"?', response.headers.get("content-disposition", ""))
    if match:
        filename = match.group(1)
    return RawResponse(content=response.content, content_type=response.headers.get("content-type"), filename=filename)


async def stream_sse_async(
    config: HttpClientConfig,
    *,
    method: str,
    path: str,
    body: Any = None,
    bearer: str | None = None,
    headers: dict[str, str] | None = None,
    query: dict[str, Any] | None = None,
    authenticated: bool = True,
    timeout: Any = None,
) -> AsyncIterator[Any]:
    """Open an SSE stream and yield JSON-decoded `data:` payloads.

    Used by streaming surfaces — `client.compute.llms.invoke_stream()`
    and `client.realtime.subscribe()` SSE transport. Multi-line `data:` fields concatenate with newline; the
    `[DONE]` sentinel terminates the stream. Raises KError on non-2xx.
    The consumer writes `async for chunk in ...`.

    `timeout` follows the client option; its read value is an idle timeout (per read), not a total one.
    """
    headers_built = _build_headers(config, headers, bearer, authenticated)
    headers_built["accept"] = "text/event-stream"
    if body is not None:
        headers_built["content-type"] = "application/json"

    url = f"{await resolve_regional_origin_async(config)}{path}"
    try:
        async with httpx.AsyncClient(transport=config.transport, timeout=_effective_timeout(config, timeout)) as client:
            async with client.stream(
                method,
                url,
                json=body if body is not None else None,
                headers=headers_built,
                params=query,
            ) as response:
                if not (200 <= response.status_code < 300):
                    raw = await response.aread()
                    parsed: Any = None
                    try:
                        parsed = json.loads(raw)
                    except ValueError:
                        parsed = None
                    if isinstance(parsed, dict) and "code" in parsed and "message" in parsed:
                        raise with_request_id(kerror_from_wire(parsed, response.status_code), response.headers)
                    raise KError(
                        code="K9O_INTERNAL_ERROR",
                        message=f"Upstream returned {response.status_code} opening stream",
                        context={"url": url, "status": response.status_code},
                        status=response.status_code,
                        request_id=response.headers.get("x-k9o-request-id") or None,
                    )
                data_lines: list[str] = []
                async for line in response.aiter_lines():
                    if line == "" or line == "\r":
                        if data_lines:
                            payload = "\n".join(data_lines)
                            data_lines = []
                            if payload == "[DONE]":
                                return
                            try:
                                yield json.loads(payload)
                            except ValueError:
                                yield payload
                        continue
                    if line.startswith(":"):
                        continue
                    if line.startswith("data:"):
                        data_lines.append(line[5:].lstrip(" ").rstrip("\r"))
                if data_lines:
                    payload = "\n".join(data_lines)
                    if payload != "[DONE]":
                        try:
                            yield json.loads(payload)
                        except ValueError:
                            yield payload
    except (httpx.TransportError, httpx.NetworkError, httpx.TimeoutException) as e:
        raise KError(
            code="K9O_UPSTREAM_UNAVAILABLE",
            message=f"Network error reaching {url}: {e}",
            context={"url": url},
            recommendation="Check connectivity to the gateway; retry with backoff if transient.",
        ) from e


async def request_async(
    config: HttpClientConfig,
    *,
    method: str,
    path: str,
    body: Any = None,
    bearer: str | None = None,
    headers: dict[str, str] | None = None,
    query: dict[str, Any] | None = None,
    authenticated: bool = True,
    multipart: dict[str, Any] | None = None,
    timeout: Any = None,
) -> Envelope[Any]:
    """Async variant of request() — same retry semantics; httpx.AsyncClient."""
    cfg = config.retry_config or RetryConfig()
    last_error: BaseException | None = None
    for attempt in range(cfg.max_retries + 1):
        try:
            return await _request_once_async(
                config, method=method, path=path, body=body,
                bearer=bearer, headers=headers, query=query,
                authenticated=authenticated, multipart=multipart, timeout=timeout,
            )
        except KError as e:
            last_error = e
            if attempt == cfg.max_retries or not _is_retryable(e, method):
                raise
            delay_ms = _compute_delay_ms(cfg, attempt)
            if delay_ms > 0:
                await asyncio.sleep(delay_ms / 1000)
    assert last_error is not None
    raise last_error


async def _request_once_async(
    config: HttpClientConfig,
    *,
    method: str,
    path: str,
    body: Any = None,
    bearer: str | None = None,
    headers: dict[str, str] | None = None,
    query: dict[str, Any] | None = None,
    authenticated: bool = True,
    multipart: dict[str, Any] | None = None,
    timeout: Any = None,
) -> Envelope[Any]:
    """Single-attempt async request. `request_async` wraps this in retry."""
    headers_built = _build_headers(config, headers, bearer, authenticated)
    # A multipart body carries its own content-type (with the boundary, set by httpx).
    if body is not None and multipart is None:
        headers_built["content-type"] = "application/json"

    url = f"{await resolve_regional_origin_async(config)}{path}"
    try:
        async with httpx.AsyncClient(transport=config.transport, timeout=_effective_timeout(config, timeout)) as client:
            response = await client.request(
                method=method,
                url=url,
                json=body if body is not None and multipart is None else None,
                **(multipart or {}),
                headers=headers_built,
                params=query,
            )
    except (httpx.TransportError, httpx.NetworkError, httpx.TimeoutException) as e:
        raise KError(
            code="K9O_UPSTREAM_UNAVAILABLE",
            message=f"Network error reaching {url}: {e}",
            context={"url": url},
            recommendation="Check connectivity to the gateway; retry with backoff if transient.",
        ) from e

    return _handle_response(response)
