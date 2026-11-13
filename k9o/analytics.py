"""
Datasets: `client.analytics.*` (datasets, rollups of a dataset, schedules) and `client.rollups.*` (read and follow rollups).

Wire mapping (project plane; `<ds>` is the dataset slug):
  - analytics.list()                                  GET    /@<slug>/analytics
  - analytics.query(sql, datasets)                    POST   /@<slug>/analytics/query
  - analytics.create(ds, ...)                         POST   /@<slug>/analytics/<ds>
  - analytics.get(ds) / delete(ds)                    GET / DELETE /@<slug>/analytics/<ds>
  - analytics.materialize(ds, from_collection= | from_query=)   POST .../<ds>/materialize
  - analytics.read(ds, partition_filter=, cursor=, limit=)      POST .../<ds>/read
  - analytics.write(ds, rows, on_error=)              POST   .../<ds>/write
  - analytics.upload(ds, data, format=, ...)          POST   .../<ds>/upload   (multipart)
  - analytics.download_original(ds)                   GET    .../<ds>/upload/original   (a file, not JSON)
  - analytics.rollups.create / list / delete / reattest       .../<ds>/rollups[/<id>[/reattest]]
  - analytics.schedules.create / list / delete / reattest     .../<ds>/schedules[/<id>[/reattest]]
  - rollups.list()                                    GET    /@<slug>/rollups
  - rollups.read(rollup_slug, from_=, to=, dims=)     GET    /@<slug>/rollups/<rollup>/read
  - rollups.subscribe(rollup_slug, from_sequence=)    GET    /@<slug>/rollups/<rollup>/subscribe   (SSE)

Callers must be project members; project service tokens and extension tokens cannot call these routes. Each call
needs the dataset permission named in its docstring. Until the region's analytics storage is provisioned, reads and
writes answer 503 `K9O_ANALYTICS_UNAVAILABLE`.

Re-attesting is for people. `rollups.reattest` and `schedules.reattest` confirm that a rollup or schedule is still
wanted. A service, workload or extension token gets 403 `K9O_RBAC_DENY` with `context["reason"] == "human_only"`; a
personal access token acts as its person. A rollup or schedule that is not re-attested within its
`reattest_interval_days` (7, 30, 90 or 365) pauses; it is not deleted, and re-attesting resumes it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, AsyncIterator, Mapping, Sequence, TypedDict
from urllib.parse import quote

from .approvals import _invalid, _require_project_slug
from .envelope import Envelope
from .http import HttpClientConfig, request, request_raw
from .realtime import ResyncHandler, open_feed
from .url_grammar import project_path

REATTEST_INTERVAL_DAYS = (7, 30, 90, 365)
"""The values `reattest_interval_days` may take."""

_ON_ERROR = ("skip", "reject")
_UPLOAD_FORMATS = ("csv", "json")


@dataclass(frozen=True)
class OriginalFile:
    """The original file of an upload: `content` bytes, its `content_type` and the `filename` the server named."""

    content: bytes
    content_type: str | None = None
    filename: str | None = None


def _seg(value: Any, what: str) -> str:
    if not isinstance(value, str) or not value:
        raise _invalid(f"{what} must be a non-empty string")
    return quote(value, safe="")


def _interval_days(value: Any) -> None:
    if value is not None and (isinstance(value, bool) or value not in REATTEST_INTERVAL_DAYS):
        raise _invalid("reattest_interval_days must be 7, 30, 90 or 365", reattest_interval_days=value)


def _flag(value: bool) -> str:
    return "true" if value else "false"


class AnalyticsNamespace:
    def __init__(self, config: HttpClientConfig) -> None:
        self._config = config
        self.rollups = DatasetRollupsNamespace(self)
        self.schedules = DatasetSchedulesNamespace(self)

    def _path(self, tail: str = "") -> str:
        return project_path(_require_project_slug(self._config, "analytics"), f"analytics{tail}")

    def _ds(self, dataset_slug: str, tail: str = "") -> str:
        return self._path(f"/{_seg(dataset_slug, 'dataset_slug')}{tail}")

    def list(self) -> Envelope:
        """The project's active datasets, newest first (needs `datasets:list`)."""
        return request(self._config, method="GET", path=self._path())

    def query(self, sql: str, datasets: Sequence[str]) -> Envelope:
        """Run a read-only SQL `SELECT` over datasets. Reference each as `dataset.<slug>` and list its slug in `datasets` (1 to 64).

        `sql` is at most 100,000 characters. Needs the project admin, author or editor role and `datasets:query` on every
        dataset read; a table that is not a listed dataset answers 400 `K9O_QUERY_INVALID`.
        """
        if not isinstance(sql, str) or not sql or len(sql) > 100_000:
            raise _invalid("sql must be a string of 1 to 100000 characters")
        if (not isinstance(datasets, (list, tuple)) or not 1 <= len(datasets) <= 64
                or not all(isinstance(d, str) and d for d in datasets)):
            raise _invalid("datasets must be a list of 1 to 64 dataset slugs")
        return request(self._config, method="POST", path=self._path("/query"),
                       body={"sql": sql, "datasets": list(datasets)})

    def create(self, dataset_slug: str, *, display_name: str, columns: Sequence[Mapping[str, str]],
               residency_class: str, partition_by: Sequence[str] | None = None,
               storage_target: Mapping[str, Any] | None = None) -> Envelope:
        """Create a dataset at `dataset_slug` (needs `datasets:create`; the slug is never reissued).

        `columns` is 1 to 512 entries of `{"name", "type"}` with type `string`, `int32`, `int64`, `float64`, `bool`,
        `date`, `timestamp`, `json`, `bytes` or `decimal(p,s)`. `residency_class` is `"per-subject"` or `"shared"`.
        `partition_by` (up to 16 keys; a timestamp key needs a transform such as `created_at:day`; `region` is added for
        you). `storage_target` is `{"kind": "managed"}` or `{"kind": "byos", "bindingId": ...}`. A project holds at most
        200 datasets (429 `K9O_DATASET_LIMIT_EXCEEDED`).
        """
        if not isinstance(display_name, str) or not 1 <= len(display_name) <= 200:
            raise _invalid("display_name must be a string of 1 to 200 characters")
        if not isinstance(columns, (list, tuple)) or not 1 <= len(columns) <= 512:
            raise _invalid("columns must be a list of 1 to 512 columns")
        for i, col in enumerate(columns):
            if (not isinstance(col, Mapping) or not isinstance(col.get("name"), str) or not col["name"]
                    or not isinstance(col.get("type"), str) or not col["type"]):
                raise _invalid(f"columns[{i}] must be a dict with a name and a type")
        if residency_class not in ("per-subject", "shared"):
            raise _invalid('residency_class must be "per-subject" or "shared"', residency_class=residency_class)
        body: dict[str, Any] = {"displayName": display_name, "columns": [dict(c) for c in columns],
                                "residencyClass": residency_class}
        if partition_by is not None:
            if not isinstance(partition_by, (list, tuple)) or len(partition_by) > 16:
                raise _invalid("partition_by must be a list of up to 16 keys")
            body["partitionBy"] = list(partition_by)
        if storage_target is not None:
            body["storageTarget"] = dict(storage_target)
        return request(self._config, method="POST", path=self._ds(dataset_slug), body=body)

    def get(self, dataset_slug: str) -> Envelope:
        """One active dataset's metadata (needs `datasets:read`); 404 `K9O_DATASET_NOT_FOUND` when deleted or unknown."""
        return request(self._config, method="GET", path=self._ds(dataset_slug))

    def delete(self, dataset_slug: str) -> Envelope:
        """Retire the dataset (needs `datasets:delete`). `data == {"slug", "status": "retired"}`; the slug is never reissued."""
        return request(self._config, method="DELETE", path=self._ds(dataset_slug))

    def materialize(self, dataset_slug: str, *, from_collection: str | None = None,
                    from_query: str | None = None) -> Envelope:
        """Copy rows into the dataset from exactly one source: a collection name or a saved Studio query id.

        The source is read with your own row access. Answers `rowsRead`, `ingested`, `rejected` and the partitions
        touched. A collection with per-subject personal data cannot go into a `shared` dataset (400
        `K9O_DATASET_RESIDENCY_SOURCE_MISMATCH`). Needs `datasets:write`.
        """
        if (from_collection is None) == (from_query is None):
            raise _invalid("send exactly one of from_collection or from_query")
        body = {"fromCollection": from_collection} if from_collection is not None else {"fromQuery": from_query}
        name = from_collection if from_collection is not None else from_query
        if not isinstance(name, str) or not 1 <= len(name) <= 128:
            raise _invalid("the source must be a string of 1 to 128 characters")
        return request(self._config, method="POST", path=self._ds(dataset_slug, "/materialize"), body=body)

    def read(self, dataset_slug: str, *, partition_filter: Mapping[str, Any] | None = None, cursor: str | None = None,
             limit: int | None = None) -> Envelope:
        """One page of the dataset's rows. `meta.cursor.next` continues; the first page carries `meta.schema`.

        Each row has a stable `_k9o_row_id`. `limit` is clamped to the server's page maximum. Needs the project admin,
        author or editor role and `datasets:query`.
        """
        body: dict[str, Any] = {}
        if partition_filter is not None:
            body["partitionFilter"] = dict(partition_filter)
        if cursor is not None:
            body["cursor"] = cursor
        if limit is not None:
            if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
                raise _invalid("limit must be a whole number, 1 or more", limit=limit)
            body["limit"] = limit
        return request(self._config, method="POST", path=self._ds(dataset_slug, "/read"), body=body)

    def write(self, dataset_slug: str, rows: Sequence[Mapping[str, Any]], *, on_error: str | None = None) -> Envelope:
        """Append rows (a list of dicts). Each row is checked against the dataset's schema.

        `on_error="skip"` (default) skips bad rows and reports them; `"reject"` refuses the whole write (400
        `K9O_INGEST_SCHEMA_MISMATCH`). Answers `ingested`, `skipped`, up to 100 `skippedDetails`, the partitions touched
        and the bytes written. Needs `datasets:write`.
        """
        if not isinstance(rows, (list, tuple)) or not rows:
            raise _invalid("rows must be a non-empty list of dicts")
        if on_error is not None and on_error not in _ON_ERROR:
            raise _invalid('on_error must be "skip" or "reject"', on_error=on_error)
        return request(self._config, method="POST", path=self._ds(dataset_slug, "/write"),
                       body=[dict(r) for r in rows], query={"onError": on_error} if on_error else None)

    def upload(self, dataset_slug: str, data: bytes | str, *, format: str, filename: str | None = None,
               manifest: Mapping[str, Any] | None = None, dry_run: bool | None = None, on_error: str | None = None,
               delimiter: str | None = None, encoding: str | None = None, has_header: bool | None = None) -> Envelope:
        """Upload a CSV or JSON file of up to 25 MiB. `format` is `"csv"` or `"json"`.

        `dry_run=True` answers the detected columns and a 50-row preview without writing (it needs `datasets:create`).
        A `manifest` (the same fields as `create`) on a slug that does not exist yet creates the dataset and loads the
        file in one call. For CSV, `delimiter`, `encoding` and `has_header` describe the file. The original file is kept
        for `download_original`. Needs `datasets:write`.
        """
        if format not in _UPLOAD_FORMATS:
            raise _invalid('format must be "csv" or "json"', format=format)
        if on_error is not None and on_error not in _ON_ERROR:
            raise _invalid('on_error must be "skip" or "reject"', on_error=on_error)
        if isinstance(data, str):
            data = data.encode("utf-8")
        elif isinstance(data, (bytearray, memoryview)):
            data = bytes(data)
        if not isinstance(data, bytes):
            raise _invalid("data must be bytes or str")
        query: dict[str, Any] = {"format": format}
        if dry_run is not None:
            query["dryRun"] = _flag(dry_run)
        if on_error is not None:
            query["onError"] = on_error
        if delimiter is not None:
            query["delimiter"] = delimiter
        if encoding is not None:
            query["encoding"] = encoding
        if has_header is not None:
            query["hasHeader"] = _flag(has_header)
        name = filename or f"upload.{format}"
        mime = "text/csv" if format == "csv" else "application/json"
        mp: dict[str, Any] = {"files": {"file": (name, data, mime)}}
        if manifest is not None:
            mp["data"] = {"manifest": json.dumps(dict(manifest))}
        return request(self._config, method="POST", path=self._ds(dataset_slug, "/upload"), query=query, multipart=mp)

    def download_original(self, dataset_slug: str) -> OriginalFile:
        """The original file of the dataset's stored upload, as an `OriginalFile`. 404 `K9O_NOT_FOUND` when none is stored."""
        raw = request_raw(self._config, method="GET", path=self._ds(dataset_slug, "/upload/original"))
        return OriginalFile(content=raw.content, content_type=raw.content_type, filename=raw.filename)


class DatasetRollupsNamespace:
    """`client.analytics.rollups`: continuous rollups that fold a stream into a dataset over fixed windows."""

    def __init__(self, ns: AnalyticsNamespace) -> None:
        self._ns = ns

    def create(self, dataset_slug: str, *, slug: str, source: str, measures: Sequence[Mapping[str, str]],
               window_seconds: int, dimensions: Sequence[str] | None = None, subject_field: str | None = None,
               non_personal: bool | None = None, reattest_interval_days: int | None = None) -> Envelope:
        """Create a rollup (needs `datasets:write`). Answers the rollup with its read and subscribe endpoints.

        `measures` is 1 to 64 entries of `{"name", "agg", "column"}`; `agg` is `count` or one of `last`, `sum`, `avg`, `min`,
        `max`, `count_distinct`, `p50`, `p95`, `p99` over a `column`. `dimensions` is up to 16 columns. `window_seconds`
        is at least 60. Give exactly one of `subject_field` (per-subject data, suppressed below the cohort threshold) or
        `non_personal=True`. `reattest_interval_days` is 7, 30, 90 or 365 (default 90).

        The rollup pauses when it is not re-attested within that interval, and only a person can re-attest it: see
        the module notes.
        """
        if not isinstance(slug, str) or not 1 <= len(slug) <= 63:
            raise _invalid("slug must be a string of 1 to 63 characters")
        if not isinstance(source, str) or not 1 <= len(source) <= 128:
            raise _invalid("source must be a string of 1 to 128 characters")
        if not isinstance(measures, (list, tuple)) or not 1 <= len(measures) <= 64:
            raise _invalid("measures must be a list of 1 to 64 measures")
        if isinstance(window_seconds, bool) or not isinstance(window_seconds, int) or window_seconds < 60:
            raise _invalid("window_seconds must be a whole number, 60 or more", window_seconds=window_seconds)
        if dimensions is not None and (not isinstance(dimensions, (list, tuple)) or len(dimensions) > 16):
            raise _invalid("dimensions must be a list of up to 16 columns")
        if (subject_field is None) == (non_personal is not True):
            raise _invalid("send exactly one of subject_field or non_personal=True")
        _interval_days(reattest_interval_days)
        body: dict[str, Any] = {"slug": slug, "source": source, "measures": [dict(m) for m in measures],
                                "windowSeconds": window_seconds}
        if dimensions is not None:
            body["dimensions"] = list(dimensions)
        if subject_field is not None:
            body["subjectField"] = subject_field
        else:
            body["nonPersonal"] = True
        if reattest_interval_days is not None:
            body["reattestIntervalDays"] = reattest_interval_days
        return request(self._ns._config, method="POST", path=self._ns._ds(dataset_slug, "/rollups"), body=body)

    def list(self, dataset_slug: str) -> Envelope:
        """The dataset's rollups with `attestedAt`, `expiresAt` and `attestationStatus` (`active`, `expiring_soon` or `expired`; expired is paused)."""
        return request(self._ns._config, method="GET", path=self._ns._ds(dataset_slug, "/rollups"))

    def delete(self, dataset_slug: str, rollup_id: str) -> Envelope:
        """Delete a rollup. `data == {"id", "deleted": True}`."""
        return request(self._ns._config, method="DELETE",
                       path=self._ns._ds(dataset_slug, f"/rollups/{_seg(rollup_id, 'rollup_id')}"))

    def reattest(self, dataset_slug: str, rollup_id: str, *, reattest_interval_days: int | None = None) -> Envelope:
        """Confirm the rollup is still wanted; an expired rollup resumes. Answers the rollup with its new `expiresAt`.

        People only: a service, workload or extension token gets 403 `K9O_RBAC_DENY` with `reason == "human_only"`.
        """
        _interval_days(reattest_interval_days)
        body = {"reattestIntervalDays": reattest_interval_days} if reattest_interval_days is not None else {}
        return request(self._ns._config, method="POST",
                       path=self._ns._ds(dataset_slug, f"/rollups/{_seg(rollup_id, 'rollup_id')}/reattest"), body=body)


class DatasetSchedulesNamespace:
    """`client.analytics.schedules`: recurring materialization from a collection into a dataset."""

    def __init__(self, ns: AnalyticsNamespace) -> None:
        self._ns = ns

    def create(self, dataset_slug: str, *, from_collection: str, interval_seconds: int,
               reattest_interval_days: int | None = None) -> Envelope:
        """Create a schedule (needs `datasets:write`). Each run reads the collection with its creator's current access.

        `interval_seconds` is at least 60. The schedule pauses when it is not re-attested within
        `reattest_interval_days` (7, 30, 90 or 365), and only a person can re-attest it. A second schedule for the same
        dataset and collection answers 409.
        """
        if not isinstance(from_collection, str) or not 1 <= len(from_collection) <= 128:
            raise _invalid("from_collection must be a string of 1 to 128 characters")
        if isinstance(interval_seconds, bool) or not isinstance(interval_seconds, int) or interval_seconds < 60:
            raise _invalid("interval_seconds must be a whole number, 60 or more", interval_seconds=interval_seconds)
        _interval_days(reattest_interval_days)
        body: dict[str, Any] = {"fromCollection": from_collection, "intervalSeconds": interval_seconds}
        if reattest_interval_days is not None:
            body["reattestIntervalDays"] = reattest_interval_days
        return request(self._ns._config, method="POST", path=self._ns._ds(dataset_slug, "/schedules"), body=body)

    def list(self, dataset_slug: str) -> Envelope:
        """The dataset's schedules with `lastRunAt`, `lastRunStatus`, `nextRunAt`, `attestedAt`, `expiresAt` and `attestationStatus`."""
        return request(self._ns._config, method="GET", path=self._ns._ds(dataset_slug, "/schedules"))

    def delete(self, dataset_slug: str, schedule_id: str) -> Envelope:
        """Delete a schedule. `data == {"id", "deleted": True}`."""
        return request(self._ns._config, method="DELETE",
                       path=self._ns._ds(dataset_slug, f"/schedules/{_seg(schedule_id, 'schedule_id')}"))

    def reattest(self, dataset_slug: str, schedule_id: str, *, reattest_interval_days: int | None = None) -> Envelope:
        """Confirm the schedule is still wanted; a paused schedule resumes. Answers the schedule with its new `expiresAt`.

        People only: a service, workload or extension token gets 403 `K9O_RBAC_DENY` with `reason == "human_only"`.
        """
        _interval_days(reattest_interval_days)
        body = {"reattestIntervalDays": reattest_interval_days} if reattest_interval_days is not None else {}
        return request(self._ns._config, method="POST",
                       path=self._ns._ds(dataset_slug, f"/schedules/{_seg(schedule_id, 'schedule_id')}/reattest"),
                       body=body)


class RollupCell(TypedDict, total=False):
    """One closed cell of a rollup feed: the frame's `payload` plus `eventId` (for dedupe) and `occurredAt`.

    A normal cell has `cellKey`, `bucket`, `dims`, `final` and `measures`. A cell below the cohort threshold has
    `status == "suppressed"` and `reason == "cohort_below_k"` and no values; `suppressed` is True for it.
    """

    cellKey: str
    bucket: str
    dims: dict
    final: bool
    measures: dict
    status: str
    reason: str
    suppressed: bool
    occurredAt: str
    eventId: str


def _cell_frame(payload: str, sse_id: str, event_name: str) -> dict[str, Any] | None:
    """A `cell` frame of a rollup feed: `{topic, eventType: "cell", payload: {...cell}, occurredAt, eventId}`.
    Result: the payload's cell, `suppressed`, `occurredAt` and `eventId` (the frame's, else the SSE id). Other events are skipped."""
    if event_name not in ("", "cell"):
        return None
    try:
        frame = json.loads(payload)
    except ValueError:
        return None
    if not isinstance(frame, dict) or not isinstance(frame.get("payload"), dict):
        return None
    event_id = frame.get("eventId") if isinstance(frame.get("eventId"), str) and frame.get("eventId") else sse_id
    if not event_id:
        return None
    cell = dict(frame["payload"])
    cell["suppressed"] = cell.get("status") == "suppressed"
    if isinstance(frame.get("occurredAt"), str):
        cell.setdefault("occurredAt", frame["occurredAt"])
    cell["eventId"] = event_id
    return cell


class RollupsNamespace:
    """`client.rollups`: the project's rollups, their cells and a live feed of closed cells."""

    def __init__(self, config: HttpClientConfig) -> None:
        self._config = config

    def _path(self, tail: str = "") -> str:
        return project_path(_require_project_slug(self._config, "rollups"), f"rollups{tail}")

    def list(self) -> Envelope:
        """Every rollup in the project whose dataset you can read, with its read and subscribe endpoints (needs `datasets:list`)."""
        return request(self._config, method="GET", path=self._path())

    def read(self, rollup_slug: str, *, from_: str | None = None, to: str | None = None,
             dims: str | Sequence[str] | None = None) -> Envelope:
        """The rollup's cells for a time range (needs `datasets:read` on its dataset and `datasets:query`).

        `from_` and `to` are ISO 8601 (default the last 24 hours; at most 50,000 cells). `dims` projects onto some
        dimensions (a list or a comma-separated string); only `count`, `sum`, `min` and `max` measures combine. A cell
        below the cohort threshold is `{"status": "suppressed", "reason": "cohort_below_k"}`; `coverage` reports the
        range, the last fold and whether it is complete.
        """
        query: dict[str, Any] = {}
        if from_ is not None:
            query["from"] = from_
        if to is not None:
            query["to"] = to
        if dims is not None:
            query["dims"] = dims if isinstance(dims, str) else ",".join(dims)
        return request(self._config, method="GET", path=self._path(f"/{_seg(rollup_slug, 'rollup_slug')}/read"),
                       query=query or None)

    def subscribe(self, rollup_slug: str, *, from_sequence: int | None = None, timeout: Any = None,
                  on_resync: ResyncHandler | None = None, idle_timeout: float | None = None) -> AsyncIterator[dict[str, Any]]:
        """Follow a rollup live: `async for cell in k9o.rollups.subscribe("orders_hourly")`, one `RollupCell` per closed cell.

        Each frame's `payload` is the cell (`cellKey`, `bucket`, `dims`, `final`, `measures`); `eventId` is kept. A cell
        below the cohort threshold has `status == "suppressed"`, `reason == "cohort_below_k"` and `suppressed` True.
        It is the change-feed reader of `realtime.subscribe`: it reconnects with `Last-Event-ID` (the frame's SSE `id`),
        drops cells it already delivered by `eventId` (a resume re-sends the last 5 seconds), and calls `on_resync` when
        the position can no longer be resumed: its `action` is `reread-source`, so re-read with `read`. A connection
        ends after at most 300 s and the iterator reconnects by itself; heartbeats (`:hb`) are ignored. `from_sequence` starts from a known position on the first connection only. Cells below the cohort
        threshold arrive suppressed, without their dimension values. The stream ends when the access token expires and
        access is re-checked while it is open; at most a fixed number of streams per caller (429
        `K9O_SUBSCRIPTION_LIMIT_EXCEEDED`).
        """
        path = self._path(f"/{_seg(rollup_slug, 'rollup_slug')}/subscribe")
        if from_sequence is not None and (isinstance(from_sequence, bool) or not isinstance(from_sequence, int)
                                          or from_sequence < 0):
            raise _invalid("from_sequence must be a whole number, 0 or more", from_sequence=from_sequence)
        initial = {"fromSequence": from_sequence} if from_sequence is not None else None
        return open_feed(self._config, path, {}, initial_query=initial, timeout=timeout, on_resync=on_resync,
                         idle_timeout=idle_timeout, parse_frame=_cell_frame)
