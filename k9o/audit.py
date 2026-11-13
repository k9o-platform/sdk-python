"""
Project-plane audit log namespace — `client.audit.*`.

Distinct from `client.org.audit_log` (org-plane). Wire mapping:
  - query(event_type=, actor_id=, after=, before=, limit=, cursor=)  GET /@<slug>/audit-log?...

  - tail(types=)                                     GET  /@<slug>/realtime/subscribe?topic=audit.project.*   (SSE, live)
  - replay(topic=, after_audit_event_id=, from_sequence=)  POST /@<slug>/realtime/replay   (answers an `AuditReplayPage`)

Option names map to the gateway's query parameters (`eventType`, `actorId`, `after`, `before`, `limit`, `cursor`).
`event_type` is a prefix match on the audit-event name (e.g. `data.write.access.insert`).
"""

from __future__ import annotations

import json
import re
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Sequence, TypedDict

from .approvals import _invalid
from .envelope import Envelope
from .http import HttpClientConfig, request, request_async
from .realtime import ResyncHandler, open_feed
from .url_grammar import project_path

AUDIT_TAIL_TOPIC = "audit.project.*"
_AUDIT_EVENT_ID = re.compile(r"^evt_[0-9A-Za-z]{1,40}$")
_TYPE = re.compile(r"^[A-Za-z0-9_.-]+$")


def _tail_topic(types: Sequence[str] | None) -> tuple[str, frozenset[str] | None]:
    """(topic, types to keep): `audit.project.*` for none, `audit.project.<type>` for one, the wildcard plus a filter for several."""
    if types is None or len(types) == 0:
        return AUDIT_TAIL_TOPIC, None
    if isinstance(types, str) or not isinstance(types, (list, tuple, set, frozenset)):
        raise _invalid("types must be a list of audit event types")
    names = list(dict.fromkeys(types))
    for t in names:
        if not isinstance(t, str) or not _TYPE.match(t) or t.endswith("*"):
            raise _invalid("each audit event type must be a name such as data.write.access.insert", type=t)
    if len(names) == 1:
        return f"audit.project.{names[0]}", None
    return AUDIT_TAIL_TOPIC, frozenset(names)


class AuditEvent(TypedDict, total=False):
    """One audit event as `tail` and `replay` deliver it: the five fields of the audit row's summary.

    `eventId` is the live frame's own id (the publisher's, not the audit id); it is only on events from the live tail.
    `sequence` is the log position as a string; it is only on events from `replay` (and on events the tail caught up).
    """

    auditEventId: str
    type: str
    actorType: str | None
    occurredAt: str | None
    outcome: str | None
    eventId: str
    sequence: str


@dataclass(frozen=True)
class AuditReplayPage:
    """What `audit.replay` answers with: one batch of events in log order."""

    events: list[AuditEvent]
    next_cursor: Any = None  # as the gateway sends it; page on with `after_audit_event_id=events[-1]["auditEventId"]`
    has_more: bool = False
    meta: Any = field(default=None, compare=False, repr=False)


def _flatten(frame: dict[str, Any], *, sequence: bool) -> AuditEvent | None:
    """The five fields of a frame (or replay event), taken from its `payload`. None when it holds no audit event id."""
    payload = frame.get("payload")
    if not isinstance(payload, dict):
        return None
    audit_id = payload.get("auditEventId")
    if not isinstance(audit_id, str) or not audit_id:
        return None
    type_ = payload.get("type")
    if not isinstance(type_, str):
        type_ = frame.get("eventType") if isinstance(frame.get("eventType"), str) else None
    occurred = payload.get("occurredAt", frame.get("occurredAt"))
    event: AuditEvent = {"auditEventId": audit_id, "type": type_, "actorType": payload.get("actorType"),  # type: ignore[typeddict-item]
                         "occurredAt": occurred, "outcome": payload.get("outcome")}
    if sequence and frame.get("sequence") is not None:
        event["sequence"] = str(frame["sequence"])
    return event


def _audit_frame(payload: str, sse_id: str, event_name: str) -> dict[str, Any] | None:
    """A live audit frame. The SSE `id` is the publisher's, the event name is the audit type, and the five fields sit in
    `data.payload`. Result: the five fields plus `eventId` (the frame's, falling back to the SSE id) for dedupe."""
    try:
        frame = json.loads(payload)
    except ValueError:
        return None
    if not isinstance(frame, dict):
        return None
    event = _flatten(frame, sequence=False)
    if event is None:
        return None
    if event["type"] is None and event_name:
        event["type"] = event_name
    event_id = frame.get("eventId")
    event_id = event_id if isinstance(event_id, str) and event_id else sse_id
    if event_id:
        event["eventId"] = event_id
    return event  # type: ignore[return-value]


def _replay_page(env: Envelope) -> AuditReplayPage:
    data = env.data if isinstance(env.data, dict) else {}
    raw = data.get("events")
    events = [e for e in (_flatten(x, sequence=True) for x in raw if isinstance(x, dict))
              if e is not None] if isinstance(raw, list) else []
    return AuditReplayPage(events=events, next_cursor=data.get("nextCursor"), has_more=data.get("hasMore") is True,
                           meta=env.meta)


def _require_project_slug(config: HttpClientConfig) -> str:
    if not config.project_slug:
        raise ValueError("audit operations require project_slug bound on the client")
    return config.project_slug


def _clean(d: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in d.items() if v is not None}


class AuditNamespace:
    def __init__(self, config: HttpClientConfig) -> None:
        self._config = config

    def query(self, *, event_type: str | None = None, actor_id: str | None = None, after: str | None = None,
              before: str | None = None, limit: int | None = None, cursor: str | None = None) -> Envelope:
        """Query the audit log (reverse-chronological; filters AND-compose).

        `after` / `before` are ISO 8601 bounds (events after / before that instant).
        """
        slug = _require_project_slug(self._config)
        return request(self._config, method="GET", path=project_path(slug, "audit-log"),
                       query=_clean({"eventType": event_type, "actorId": actor_id, "after": after,
                                     "before": before, "limit": limit, "cursor": cursor}))

    def recent(self, *, since_minutes: int = 1, limit: int = 100) -> Envelope:
        """Events from the last `since_minutes` in one read (`after` set), newest first. For a live feed use `tail`."""
        slug = _require_project_slug(self._config)
        after = (datetime.now(timezone.utc) - timedelta(minutes=since_minutes)).isoformat()
        return request(self._config, method="GET", path=project_path(slug, "audit-log"),
                       query={"after": after, "limit": limit})

    def replay(self, *, topic: str = AUDIT_TAIL_TOPIC, after_audit_event_id: str | None = None,
               from_sequence: int | None = None, until_sequence: int | None = None, limit: int | None = None) -> AuditReplayPage:
        """Past audit events in order from the audit log (`warden.checkpoint` included), one batch of up to 1,000 events by default (`limit` up to 10,000).

        Answers an `AuditReplayPage`: `events` (each `AuditEvent`: the five fields of a live event plus its `sequence`, a
        string), `next_cursor` and `has_more`. There is no cursor parameter: to read on, call again with
        `after_audit_event_id` set to the last event's `auditEventId`.
        Give `after_audit_event_id` (it wins over `from_sequence`; an id that is not in the log answers 400
        `K9O_INVALID_CURSOR`) or `from_sequence`. Needs the audit log's access (403 `K9O_RBAC_DENY` otherwise).
        """
        slug = _require_project_slug(self._config)
        if not isinstance(topic, str) or not topic:
            raise _invalid("topic must be a topic name such as audit.project.*")
        if after_audit_event_id is not None and (not isinstance(after_audit_event_id, str)
                                                 or not _AUDIT_EVENT_ID.match(after_audit_event_id)):
            raise _invalid("after_audit_event_id must look like evt_<letters and digits>", after_audit_event_id=after_audit_event_id)
        for name, value in (("from_sequence", from_sequence), ("until_sequence", until_sequence)):
            if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
                raise _invalid(f"{name} must be a whole number, 0 or more", **{name: value})
        if limit is None:
            limit = 1000  # the gateway answers everything it holds when none is sent
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 10000:
            raise _invalid("limit must be 1 to 10000", limit=limit)
        env = request(self._config, method="POST", path=project_path(slug, "realtime/replay"),
                      body=_clean({"topic": topic, "afterAuditEventId": after_audit_event_id,
                                   "fromSequence": from_sequence, "untilSequence": until_sequence, "limit": limit}))
        return _replay_page(env)

    def tail(self, types: Sequence[str] | None = None, *, timeout: Any = None, on_resync: ResyncHandler | None = None,
             idle_timeout: float | None = None, since_minutes: int | None = None, limit: int | None = None) -> Any:
        """Follow the project's audit events live: `async for event in k9o.audit.tail(types=["data.write.access.insert"])`.

        Each event has `auditEventId`, `type`, `actorType`, `occurredAt` and `outcome` (flattened from the frame's
        `payload`) plus `eventId`, the frame's own id; read the full row with `query`. Events caught up after a resync
        carry `sequence` instead of `eventId`. `types` is a list of audit event types: one type tails `audit.project.<type>`, none tails
        `audit.project.*`, and several tail `audit.project.*` and keep the listed types. `warden.checkpoint` events are
        not in the live tail; `query` and `replay` carry them.

        A new connection starts at the live head and may re-send the last few seconds of events (use `replay` for history); a `resync` that opens a fresh
        connection is informational and ignored. Each event is yielded once (dedupe on `auditEventId` if you keep your own state). It reconnects with
        `Last-Event-ID` after a network drop, treats `idle_timeout` seconds (default 45) without any bytes as a dead
        connection, and calls `on_resync` when the position is lost. After a `resync` (`action` is `replay-ledger`) audit
        streams cannot be resumed by sequence, so it connects fresh and holds the frames that arrive, replays from the last
        `auditEventId` it delivered (paging while `hasMore`), then yields the held and live frames, deduped: nothing is
        lost. With nothing delivered yet there is nothing to replay. Delivery is at-least-once.

        Needs ORG_OWNER, ORG_ADMIN, PROJECT_ADMIN or PROJECT_AUDITOR (re-checked every 60 s); extensions cannot subscribe.
        A 4xx raises `KError` once.

        Passing `since_minutes` or `limit` keeps the earlier behaviour (one read of recent events, an Envelope); that
        form is `recent`.
        """
        if since_minutes is not None or limit is not None:
            return self.recent(since_minutes=1 if since_minutes is None else since_minutes,
                               limit=100 if limit is None else limit)
        slug = _require_project_slug(self._config)
        topic, keep = _tail_topic(types)
        return self._tail(slug, topic, keep, timeout, on_resync, idle_timeout)

    async def _tail(self, slug: str, topic: str, keep: frozenset[str] | None, timeout: Any,
                    on_resync: ResyncHandler | None, idle_timeout: float | None) -> AsyncIterator[AuditEvent]:
        last_audit_id: list[str | None] = [None]
        delivered: OrderedDict[str, None] = OrderedDict()

        async def catch_up(info: dict) -> list[dict[str, Any]]:
            after = last_audit_id[0]
            out: list[dict[str, Any]] = []
            while after is not None:  # a replay batch is capped; page on while the log says there is more
                env = await request_async(self._config, method="POST", path=project_path(slug, "realtime/replay"),
                                          body={"topic": topic, "afterAuditEventId": after, "limit": 1000})
                page = _replay_page(env)
                if not page.events or page.events[-1]["auditEventId"] == after:
                    break
                out.extend(page.events)  # type: ignore[arg-type]
                after = page.events[-1]["auditEventId"]
                if not page.has_more:
                    break
            return out

        async for event in open_feed(self._config, project_path(slug, "realtime/subscribe"), {"topic": topic},
                                     timeout=timeout, on_resync=on_resync, idle_timeout=idle_timeout,
                                     parse_frame=_audit_frame, resync_frames=catch_up):
            audit_id = event["auditEventId"]
            if audit_id in delivered:
                continue
            delivered[audit_id] = None
            if len(delivered) > 4096:
                delivered.popitem(last=False)
            last_audit_id[0] = audit_id
            if keep is None or event.get("type") in keep:
                yield event  # type: ignore[misc]
