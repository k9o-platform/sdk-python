"""
Realtime Subscriptions namespace — `client.realtime.*`.

`subscribe()` returns an async iterator — `async for event in client.realtime.subscribe(topic): ...` — over SSE:
`GET /@<slug>/realtime/subscribe?topic=<t>&filter=<json>`. The gateway serves no WebSocket upgrade.

The iterator resumes by itself: when the stream ends (the server closes a stream after 300 s), the connection drops, or
nothing arrives for 45 s, it reconnects with `Last-Event-ID` set to the SSE `id` of the last frame (the frame's
`eventId` when it carried no `id`), and drops events whose `eventId` it has already delivered. An `event: resync` frame means the position is lost: it is not yielded; `on_resync` is called (it may
be async) and the client reconnects without `Last-Event-ID`. Re-read current state from the data API inside `on_resync`.
Delivery is at-least-once. A 4xx answer raises `KError` once; 5xx and network errors are retried.

`replay(topic, from_sequence=, until_sequence=, limit=)` is the catch-up call: `POST /@<slug>/realtime/replay`.

`filter` and RLS compose (filter narrows, RLS gates). Surfaces K9O_INVALID_FILTER, K9O_SUBSCRIPTION_LIMIT_EXCEEDED and
K9O_TIER_FEATURE_GATED.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Awaitable, Callable, Union

import httpx

from .errors import KError, kerror_from_wire, with_request_id
from .envelope import Envelope
from .http import HttpClientConfig, _build_headers, _effective_timeout, request
from .region import resolve_regional_origin_async
from .url_grammar import project_path

# Reconnect and dedupe tuning.
_DEFAULT_RETRY_S = 1.0       # used when the server sends no `retry:` hint
_MAX_BACKOFF_S = 30.0
_IDLE_TIMEOUT_S = 45.0       # no bytes (frames, comments or heartbeats) for this long: reconnect
_DEDUPE_SIZE = 1024

ResyncHandler = Callable[[dict], Union[None, Awaitable[None]]]
# Reads one SSE frame: (data payload, SSE `id`, SSE `event` name) -> the frame to deliver, or None to skip it.
FrameParser = Callable[[str, str, str], Union[dict, None]]
# Called after a resync with its info; returns frames to deliver before the feed reconnects (an audit tail uses it to catch up).
ResyncFrames = Callable[[dict], Awaitable[list]]

# The 6 closed topic families.
REALTIME_TOPIC_FAMILIES = (
    "collections",
    "studio.workflows.runs",
    "studio.queries.executed",
    "studio.dashboards.refresh",
    "forge.functions.invocations",
    "audit.org",
)


class realtime_topic:
    """Typed builders for the 6 topic families."""

    @staticmethod
    def collection(collection: str) -> str:
        return f"collections.{collection}.*"

    @staticmethod
    def workflow_runs() -> str:
        return "studio.workflows.runs.*"

    @staticmethod
    def query_executed(query_id: str) -> str:
        return f"studio.queries.executed.{query_id}"

    @staticmethod
    def dashboard_refresh(dashboard_id: str) -> str:
        return f"studio.dashboards.refresh.{dashboard_id}"

    @staticmethod
    def function_invocations(function_id: str) -> str:
        return f"forge.functions.invocations.{function_id}"

    @staticmethod
    def audit_org(event_family: str) -> str:
        return f"audit.org.{event_family}"


def _require_project_slug(config: HttpClientConfig) -> str:
    if not config.project_slug:
        raise ValueError("realtime operations require project_slug bound on the client")
    return config.project_slug


@dataclass
class RealtimeReplayPage:
    """What `realtime.replay` answers with: one batch of past events in order.

    Each event is a dict in the shape `subscribe` yields (`topic`, `eventType`, `payload`, `occurredAt`) plus the
    `sequence` it was stored at, a string. `resync` is `"reread-source"` when the topic keeps no history to replay
    (then `events` is empty): re-read current state through the data API instead.
    """

    topic: str
    events: list[dict[str, Any]]
    next_cursor: Any = None
    has_more: bool = False
    resync: str | None = None
    meta: Any = field(default=None, compare=False, repr=False)


def _replay_page(env: Envelope, topic: str) -> RealtimeReplayPage:
    data = env.data if isinstance(env.data, dict) else {}
    raw = data.get("events")
    events = [e for e in raw if isinstance(e, dict)] if isinstance(raw, list) else []
    resync = data.get("resync")
    return RealtimeReplayPage(topic=data.get("topic") if isinstance(data.get("topic"), str) else topic, events=events,
                              next_cursor=data.get("nextCursor"), has_more=data.get("hasMore") is True,
                              resync=resync if isinstance(resync, str) else None, meta=env.meta)


def _bad(message: str, **context: Any) -> KError:
    return KError(code="K9O_INVALID_FIELD_VALUE", message=message, context=context)


class RealtimeNamespace:
    def __init__(self, config: HttpClientConfig) -> None:
        self._config = config

    def replay(
        self,
        topic: str,
        *,
        from_sequence: int | None = None,
        until_sequence: int | None = None,
        limit: int | None = None,
    ) -> RealtimeReplayPage:
        """Catch up on past events for a topic: `POST /@<slug>/realtime/replay`. One batch, in order.

        `topic` is required (a realtime topic name, up to 200 characters). `from_sequence` (events after it) and `until_sequence`
        (up to and including it) bound the range (whole numbers, 0 or more); `limit` is 1 to 10000 (10000 when not given).
        To read on, pass `next_cursor` as `from_sequence` while `has_more`.
        Answers a `RealtimeReplayPage` (`topic`, `events`, `next_cursor`, `has_more`, `resync`). Only topics the gateway keeps
        a log for return events (the project's audit events, `audit.project.*`, needing the audit log's access); for the
        others `events` is empty and `resync` is `"reread-source"`. An unknown topic raises 400 `K9O_INVALID_FILTER`.
        Use `audit.replay` for audit events in the audit shape.
        """
        slug = _require_project_slug(self._config)
        if not isinstance(topic, str) or not 1 <= len(topic) <= 200:
            raise _bad("topic must be a topic name of 1 to 200 characters", topic=topic)
        for name, value in (("from_sequence", from_sequence), ("until_sequence", until_sequence)):
            if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
                raise _bad(f"{name} must be a whole number, 0 or more", **{name: value})
        if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 10000):
            raise _bad("limit must be 1 to 10000", limit=limit)
        body: dict[str, Any] = {"topic": topic}
        if from_sequence is not None:
            body["fromSequence"] = from_sequence
        if until_sequence is not None:
            body["untilSequence"] = until_sequence
        if limit is not None:
            body["limit"] = limit
        env = request(self._config, method="POST", path=project_path(slug, "realtime/replay"), body=body)
        return _replay_page(env, topic)

    async def subscribe(
        self,
        topic: str,
        *,
        filter: dict[str, Any] | None = None,
        timeout: Any = None,
        on_resync: ResyncHandler | None = None,
        idle_timeout: float | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Subscribe to a topic over SSE; yields event dicts with `topic`, `eventType`, `payload`, `occurredAt`, `eventId`.

        Reconnects with `Last-Event-ID` and drops duplicates by `eventId`. `on_resync(info)` (sync or async) is called
        with `{"reason", "action"}` when the position is lost; the next connection starts fresh. `idle_timeout` is the
        number of seconds without any bytes before the connection is abandoned (default 45).
        """
        slug = _require_project_slug(self._config)
        query: dict[str, Any] = {"topic": topic}
        if filter is not None:
            query["filter"] = json.dumps(filter)
        async for frame in open_feed(self._config, project_path(slug, "realtime/subscribe"), query,
                                     timeout=timeout, on_resync=on_resync, idle_timeout=idle_timeout):
            yield frame


async def open_feed(
    config: HttpClientConfig,
    path: str,
    query: dict[str, Any],
    *,
    initial_query: dict[str, Any] | None = None,
    timeout: Any = None,
    on_resync: ResyncHandler | None = None,
    idle_timeout: float | None = None,
    parse_frame: FrameParser | None = None,
    resync_frames: ResyncFrames | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """The reconnecting change-feed reader behind `realtime.subscribe`, `metadata.subscribe`, `rollups.subscribe` and `audit.tail`.

    `parse_frame` replaces the default frame reader (which wants `eventType`); every frame it returns must carry an
    `eventId` string, which is what dedupe uses; resume uses the SSE `id` of the frame, else its `eventId`. `resync_frames(info)` runs after `on_resync`, on the next fresh connection once it is open (frames
    that arrive meanwhile are held), and returns frames to deliver (deduped by `eventId`) before the held and live frames.

    A `resync` that is the first data on a connection opened with neither `Last-Event-ID` nor a start position (`initial_query`) is informational (the gateway sends one
    `reread-source` frame with the head on a fresh connection, and live frames follow on the same connection): it is ignored
    and the stream is kept. A `resync` after data was received on the connection, or on a connection that sent
    `Last-Event-ID` or a start position, is a lost position: `on_resync` is called and the feed reconnects fresh.

    `initial_query` is added to the query only until the feed has a position of its own: it is dropped once a
    connection has been answered or the position is resynced, and never sent together with `Last-Event-ID`.
    """
    idle = _IDLE_TIMEOUT_S if idle_timeout is None else idle_timeout

    last_id: str | None = None
    seen: OrderedDict[str, None] = OrderedDict()
    retry_s = _DEFAULT_RETRY_S
    failures = 0
    use_initial = initial_query is not None
    resyncs_in_row = 0
    catchup_info: dict | None = None  # set by a resync: the next connection runs `resync_frames` once it is open
    buffer: list[dict[str, Any]] = []

    while True:
        params = {**query, **(initial_query or {})} if use_initial and last_id is None else query
        headers = _build_headers(config, {"last-event-id": last_id} if last_id else None, None, True)
        headers["accept"] = "text/event-stream"
        url = f"{await resolve_regional_origin_async(config)}{path}"
        resync: dict | None = None
        failed = False
        # No Last-Event-ID and no start position (`initial_query`, e.g. fromSequence) sent, and no data seen yet on this connection.
        fresh_head = last_id is None and not (use_initial and initial_query is not None)
        catchup: asyncio.Future | None = None
        nxt: asyncio.Future | None = None
        try:
            async with httpx.AsyncClient(transport=config.transport,
                                         timeout=_effective_timeout(config, timeout)) as http:
                async with http.stream("GET", url, headers=headers, params=params) as response:
                    if not (200 <= response.status_code < 300):
                        raw = await response.aread()
                        err = _status_error(response.status_code, raw, url, response.headers)
                        if response.status_code < 500:
                            raise err
                        failed = True
                    else:
                        use_initial = False
                        lines = response.aiter_lines().__aiter__()

                        async def read_line() -> str | None:
                            try:
                                return await lines.__anext__()
                            except StopAsyncIteration:
                                return None

                        if catchup_info is not None and resync_frames is not None:
                            # The connection is open: catch up from the log now, and hold the frames that arrive meanwhile.
                            catchup = asyncio.ensure_future(resync_frames(catchup_info))
                        event_name = ""
                        event_id = ""
                        data_lines: list[str] = []
                        while True:
                            if nxt is None:
                                nxt = asyncio.ensure_future(read_line())
                            waiting = {nxt} if catchup is None else {nxt, catchup}
                            done, _ = await asyncio.wait(waiting, timeout=idle)
                            if not done:
                                failed = True
                                break
                            if catchup is not None and catchup in done:
                                finished, catchup = catchup, None
                                extras = finished.result()
                                catchup_info = None
                                out: list[dict[str, Any]] = []
                                for extra in extras:
                                    eid = extra.get("eventId")
                                    if isinstance(eid, str) and eid:
                                        if eid in seen:
                                            continue
                                        seen[eid] = None
                                    out.append(extra)
                                out.extend(buffer)
                                buffer.clear()
                                for item in out:
                                    yield item
                            if nxt not in done:
                                continue
                            finished_line, nxt = nxt, None
                            line = finished_line.result()
                            if line is None:
                                break
                            line = line.rstrip("\r")
                            if line == "":
                                if data_lines or event_name:
                                    payload = "\n".join(data_lines)
                                    if event_name == "resync":
                                        try:
                                            info = json.loads(payload)
                                        except ValueError:
                                            info = {}
                                        info = info if isinstance(info, dict) else {}
                                        if fresh_head:
                                            # The first data on a fresh connection (no Last-Event-ID sent, nothing delivered on it)
                                            # is informational: live frames follow on this connection. Keep reading it.
                                            resyncs_in_row += 1  # a frame resets it; a stream that ends with none backs off
                                            fresh_head = False
                                            event_name = ""
                                            event_id = ""
                                            data_lines = []
                                            continue
                                        resync = info
                                        break
                                    frame = (parse_frame or _parse_frame)(payload, event_id, event_name)
                                    fresh_head = False
                                    if frame is not None:
                                        failures = 0
                                        resyncs_in_row = 0
                                        eid = frame.get("eventId")
                                        if isinstance(eid, str) and eid:
                                            # Resume from the SSE `id` of the frame (it can differ from the frame's `eventId`);
                                            # dedupe on `eventId`.
                                            if event_id:
                                                last_id = event_id
                                            if eid in seen:
                                                frame = None
                                            else:
                                                seen[eid] = None
                                                if len(seen) > _DEDUPE_SIZE:
                                                    seen.popitem(last=False)
                                                if not event_id:
                                                    last_id = eid
                                        if frame is not None:
                                            if catchup_info is not None:
                                                buffer.append(frame)  # delivered after the catch-up events
                                            else:
                                                yield frame
                                event_name = ""
                                event_id = ""
                                data_lines = []
                                continue
                            if line.startswith(":"):
                                continue
                            name, _, value = line.partition(":")
                            if value.startswith(" "):
                                value = value[1:]
                            if name == "data":
                                data_lines.append(value)
                            elif name == "event":
                                event_name = value
                            elif name == "id":
                                event_id = value
                            elif name == "retry" and value.isdigit():
                                retry_s = int(value) / 1000
        except (httpx.TransportError, httpx.NetworkError, httpx.TimeoutException):
            failed = True
        finally:
            for task in (nxt, catchup):
                if task is not None and not task.done():
                    task.cancel()
                    try:
                        await task
                    except BaseException:  # noqa: BLE001 - a cancelled helper; nothing to report
                        pass

        if resync is not None:
            use_initial = False
            last_id = None
            seen.clear()
            resyncs_in_row += 1
            if on_resync is not None:
                result = on_resync(resync)
                if inspect.isawaitable(result):
                    await result
            if resync_frames is not None:
                catchup_info = resync  # run on the next (fresh) connection, after it is open
            # A fresh connection answered with resync again, with no event in between: back off, never spin.
            await asyncio.sleep(retry_s if resyncs_in_row < 2 else min(_MAX_BACKOFF_S, retry_s * (2 ** (resyncs_in_row - 1))))
            continue
        if failed:
            failures += 1
            await asyncio.sleep(min(_MAX_BACKOFF_S, retry_s * (2 ** (failures - 1))))
        elif resyncs_in_row >= 2:
            await asyncio.sleep(min(_MAX_BACKOFF_S, retry_s * (2 ** (resyncs_in_row - 1))))  # resync again, no event between
        else:
            await asyncio.sleep(retry_s)


def _parse_frame(payload: str, sse_id: str, event_name: str = "") -> dict[str, Any] | None:
    try:
        frame = json.loads(payload)
    except ValueError:
        return None
    if not isinstance(frame, dict) or not isinstance(frame.get("eventType"), str):
        return None
    if not isinstance(frame.get("eventId"), str) and sse_id:
        frame["eventId"] = sse_id
    return frame


def _status_error(status: int, raw: bytes, url: str, headers: Any = None) -> KError:
    try:
        parsed = json.loads(raw)
    except ValueError:
        parsed = None
    if isinstance(parsed, dict) and "code" in parsed and "message" in parsed:
        return with_request_id(kerror_from_wire(parsed, status), headers)
    return with_request_id(KError(
        code="K9O_INTERNAL_ERROR",
        message=f"Upstream returned {status} opening stream",
        context={"url": url, "status": status},
        status=status,
    ), headers)
