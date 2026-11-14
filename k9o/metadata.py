"""
Metadata namespace: `client.metadata.*`. Metadata is a set of columns you declare on a collection (or on another data
surface) so that they can be filtered, searched and followed live through one API, with the same access rules as the
data itself.

Wire mapping (project plane):
  - filter(surfaces, where=, limit=, cursor=)                 POST  /@<slug>/metadata/query
  - set(collection, id, metadata)                             PATCH /@<slug>/metadata/collection/<collection>/<id>
  - search(surface, field, q, limit=)                         POST  /@<slug>/metadata/search
  - vectors.nearest(surface, column, embedding, k=, ...)      POST  /@<slug>/metadata/vectors/nearest
  - subscribe(surface, from_sequence=, on_resync=, ...)       GET   /@<slug>/metadata/subscribe?surface=  (SSE)
  - index.get(collection)                                     GET   /@<slug>/metadata/index/<collection>
  - index.declare(collection, columns)                        PUT   /@<slug>/metadata/index/<collection>

Columns are declared first (`index.declare`); `set` writes declared columns only, and `filter`, `search` and
`vectors.nearest` read declared columns only. Column names given to `filter` (`where[].field`), `search` (`field`) and
`vectors.nearest` (`column`) are sent as `metadata.<name>`; the prefix is added when you leave it out. Rows come back
with bare column names under `metadata`. A `where` clause is a dict `{"field", "op", "value"}`; `op` is one of
`eq`, `neq`, `gt`, `gte`, `lt`, `lte`, `contains`, `startsWith`, `endsWith`, `in`, `notIn` (`and`, `or` and `not` are part
of the grammar, and the gateway refuses them today). Rows keep the gateway's field names (`recordId`, `eventId`).

`filter`, `search` and `vectors.nearest` answer a `MetadataPage`: an `Envelope` whose `data` is the list of rows
(`{"surface", "scope", "recordId" or "eventId", "metadata"}`) and whose `cursor` is the cursor of the next page, or None.
Arguments are checked before any request; a bad value raises KError `K9O_INVALID_FIELD_VALUE` (a bad surface raises
`K9O_UNKNOWN_SURFACE`) and nothing is sent.

Errors arrive as `KError` with the gateway's code. The ones to expect:
  - 400 `K9O_UNKNOWN_SURFACE`: the surface is not `collection:<name>`, `telemetry:<event>` or `storage:<bucket>`.
  - 400 `K9O_METADATA_FIELD_NOT_INDEXED`: the column is not declared (`context["field"]`).
  - 400 `K9O_ENCRYPTED_COLUMN_OPERATION_UNSUPPORTED`: an encrypted column supports only `eq` (not null) and `in`, no
    search or nearest.
  - 400 `K9O_INVALID_FIELD_VALUE`, `K9O_INVALID_CURSOR`: a bad body field or cursor.
  - 403 `K9O_ACCESS_DENIED`, `K9O_EXTENSION_PERMISSION_DENIED`: no access to the surface.
  - 404 `K9O_COLLECTION_NOT_FOUND` (declare on a missing collection), `K9O_NOT_FOUND` (set on a missing record).
  - 413 `K9O_QUERY_RESULT_TOO_LARGE`: more than 10000 rows across the surfaces (`context["cap"]`, `context["surfaces"]`).
  - 501 `K9O_NOT_IMPLEMENTED`: `storage:` surfaces are accepted by the SDK, and the gateway answers 501 for them.
"""

from __future__ import annotations

import math
import re
from typing import Any, AsyncIterator, Mapping, Sequence
from urllib.parse import quote

from .approvals import _invalid, _require_project_slug
from .envelope import Envelope
from .errors import KError
from .http import HttpClientConfig, request
from .realtime import ResyncHandler, open_feed
from .url_grammar import project_path

_OPS = ("eq", "neq", "gt", "gte", "lt", "lte", "contains", "startsWith", "endsWith", "in", "notIn", "and", "or", "not")
_METRICS = ("cosine", "euclidean", "dot")
_SLICES = ("collection", "telemetry", "storage")
_SCOPE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_IDENT = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]{0,62}$")
_PREFIX = "metadata."
_MAX_COLUMNS = 64
_MAX_LIMIT = 1000
# On a telemetry surface these where fields are bare, never prefixed.
_TELEMETRY_FIELDS = ("createdAt", "actorId")


class MetadataPage(Envelope):
    """An `Envelope` of rows. `data` is the list of rows; `cursor` is the next page's cursor, or None."""

    @property
    def cursor(self) -> str | None:
        cursor = self.meta.cursor
        return cursor.next if cursor is not None and isinstance(cursor.next, str) else None


def _page(env: Envelope) -> MetadataPage:
    return MetadataPage(data=env.data, meta=env.meta)


def _slice_of(surface: str) -> str:
    return surface[: surface.index(":")]


def _check_surface(surface: Any, name: str) -> str:
    if not isinstance(surface, str):
        raise _invalid(f"{name} must be a string such as collection:posts", surface=surface)
    slice_, sep, scope = surface.partition(":")
    if (not sep or slice_ not in _SLICES or not _SCOPE.match(scope)
            or (slice_ == "collection" and not _IDENT.match(scope))):
        raise KError(
            code="K9O_UNKNOWN_SURFACE",
            message=f'{name} "{surface}" is not a surface: use collection:<name>, telemetry:<event> or storage:<bucket>',
            context={"surface": surface},
        )
    return surface


def _check_collection_surface(surface: Any, name: str) -> str:
    checked = _check_surface(surface, name)
    if _slice_of(checked) != "collection":
        raise _invalid(f"{name} must be a collection surface (collection:<name>)", surface=checked)
    return checked


def _check_limit(value: Any, name: str) -> None:
    if value is not None and (isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= _MAX_LIMIT):
        raise _invalid(f"{name} must be a whole number from 1 to {_MAX_LIMIT}", **{name: value})


def _prefixed(name: Any, label: str) -> str:
    if not isinstance(name, str) or not name or name == _PREFIX:
        raise _invalid(f"{label} must be a column name", **{"value": name})
    return name if name.startswith(_PREFIX) else _PREFIX + name


def _wire_where(where: Any, telemetry: bool) -> list[dict[str, Any]] | None:
    if where is None:
        return None
    if not isinstance(where, (list, tuple)):
        raise _invalid("where must be a list of {field, op, value}")
    out: list[dict[str, Any]] = []
    for index, clause in enumerate(where):
        if not isinstance(clause, Mapping):
            raise _invalid(f"where[{index}] must be a dict")
        op = clause.get("op")
        if not isinstance(op, str) or op not in _OPS:
            raise _invalid(f"where[{index}].op must be one of: {', '.join(_OPS)}", op=op)
        field = clause.get("field")
        bare = telemetry and isinstance(field, str) and field in _TELEMETRY_FIELDS
        out.append({**clause, "field": field if bare else _prefixed(field, f"where[{index}].field")})
    return out


def _collection_name(collection: Any) -> str:
    if not isinstance(collection, str) or not _IDENT.match(collection):
        raise _invalid("collection must be a collection name", collection=collection)
    return collection


class MetadataVectorsNamespace:
    def __init__(self, namespace: "MetadataNamespace") -> None:
        self._ns = namespace

    def nearest(self, *, surface: str, column: str, embedding: Sequence[float], k: int | None = None,
                metric: str | None = None, where: Sequence[Mapping[str, Any]] | None = None) -> MetadataPage:
        """The rows whose declared vector column is nearest to `embedding`, closest first.

        `surface` is a `collection:<name>` surface; `column` is the declared vector column (`embedding` or
        `metadata.embedding`); `embedding` is a non-empty list of finite numbers; `k` is 1 to 1000 (the gateway defaults
        to 10); `metric` is `cosine` (default), `euclidean` or `dot`; `where` uses the grammar of `filter`.
        Rows carry no score. At most 50 rows come back whatever `k` is, until the gateway forwards a larger page size.
        """
        path = self._ns._path("vectors/nearest")
        target = _check_collection_surface(surface, "surface")
        wire_column = _prefixed(column, "column")
        if not isinstance(embedding, (list, tuple)) or len(embedding) < 1:
            raise _invalid("embedding must be a non-empty list of numbers")
        if not all(isinstance(n, (int, float)) and not isinstance(n, bool) and math.isfinite(n) for n in embedding):
            raise _invalid("embedding must contain only finite numbers")
        _check_limit(k, "k")
        if metric is not None and metric not in _METRICS:
            raise _invalid(f"metric must be one of: {', '.join(_METRICS)}", metric=metric)
        wire = _wire_where(where, False)
        body: dict[str, Any] = {"surface": target, "column": wire_column, "embedding": list(embedding)}
        if k is not None:
            body["k"] = k
        if metric is not None:
            body["metric"] = metric
        if wire is not None:
            body["where"] = wire
        return _page(request(self._ns._config, method="POST", path=path, body=body))


class MetadataIndexNamespace:
    def __init__(self, namespace: "MetadataNamespace") -> None:
        self._ns = namespace

    def get(self, collection: str) -> Envelope:
        """The columns declared on a collection, ordered by name. `data` is `{"collection", "columns": [{"column", "indexed"}]}`."""
        path = self._ns._path(f"index/{quote(_collection_name(collection), safe='')}")
        return request(self._ns._config, method="GET", path=path)

    def declare(self, collection: str, columns: Sequence[Mapping[str, Any]]) -> Envelope:
        """Declare the columns of a collection that metadata may use. Each entry is `{"column": name, "indexed": bool}`
        (`indexed` defaults to False). The columns must already exist on the collection.

        The list replaces the previous declaration (an empty list clears it); at most 64 columns, no `id`, no
        duplicates. Needs the project-admin role and the migrations permission.
        """
        path = self._ns._path(f"index/{quote(_collection_name(collection), safe='')}")
        if not isinstance(columns, (list, tuple)):
            raise _invalid("columns must be a list of {column, indexed}")
        if len(columns) > _MAX_COLUMNS:
            raise _invalid(f"at most {_MAX_COLUMNS} columns can be declared", count=len(columns))
        seen: set[str] = set()
        for index, entry in enumerate(columns):
            if not isinstance(entry, Mapping):
                raise _invalid(f"columns[{index}] must be a dict")
            name = entry.get("column")
            if not isinstance(name, str) or not _IDENT.match(name) or name == "id":
                raise _invalid(f"columns[{index}].column must be a column name other than id", column=name)
            if "indexed" in entry and not isinstance(entry["indexed"], bool):
                raise _invalid(f"columns[{index}].indexed must be True or False")
            if name in seen:
                raise _invalid(f'column "{name}" is listed twice', column=name)
            seen.add(name)
        return request(self._ns._config, method="PUT", path=path, body={"columns": [dict(c) for c in columns]})


class MetadataNamespace:
    def __init__(self, config: HttpClientConfig) -> None:
        self._config = config
        self.vectors = MetadataVectorsNamespace(self)
        self.index = MetadataIndexNamespace(self)

    def _path(self, tail: str) -> str:
        return project_path(_require_project_slug(self._config, "metadata"), f"metadata/{tail}")

    def filter(self, surfaces: Sequence[str], *, where: Sequence[Mapping[str, Any]] | None = None,
               limit: int | None = None, cursor: str | None = None) -> MetadataPage:
        """Rows of one or more surfaces whose declared columns meet `where`, in request order.

        `surfaces` are `collection:<name>`, `telemetry:<event-prefix>` or `storage:<bucket>`. `limit` is 1 to 1000 rows per
        surface (the gateway defaults to 100). `cursor` is the `cursor` of the previous page and is allowed with exactly
        one surface; `page.cursor` is None on the last page. More than 10000 rows in all answers 413
        `K9O_QUERY_RESULT_TOO_LARGE`; a `storage:` surface answers 501 `K9O_NOT_IMPLEMENTED`.
        """
        path = self._path("query")
        if not isinstance(surfaces, (list, tuple)) or len(surfaces) < 1:
            raise _invalid("surfaces must be a non-empty list")
        checked = [_check_surface(s, f"surfaces[{i}]") for i, s in enumerate(surfaces)]
        _check_limit(limit, "limit")
        if cursor is not None:
            if not isinstance(cursor, str) or not cursor:
                raise _invalid("cursor must be the cursor of a previous page")
            if len(checked) != 1:
                raise _invalid("cursor is allowed only with exactly one surface", field="cursor")
        wire = _wire_where(where, any(_slice_of(s) == "telemetry" for s in checked))
        body: dict[str, Any] = {"surfaces": checked}
        if wire is not None:
            body["where"] = wire
        if limit is not None:
            body["limit"] = limit
        if cursor is not None:
            body["cursor"] = cursor
        return _page(request(self._config, method="POST", path=path, body=body))

    def set(self, collection: str, id: str, metadata: Mapping[str, Any]) -> Envelope:
        """Write declared columns of one record: `set("posts", "p2", {"title": "New", "priority": 9})`.

        Keys are bare column names (a leading `metadata.` is dropped); a column that is not declared answers 400
        `K9O_METADATA_FIELD_NOT_INDEXED`. `data` is the whole updated record, including columns that are not declared.
        The write runs through the record API's rules, so a write that needs approval is queued as it is there.
        """
        name = _collection_name(collection)
        if not isinstance(id, str) or not id:
            raise _invalid("id must be a record id", id=id)
        path = self._path(f"collection/{quote(name, safe='')}/{quote(id, safe='')}")
        if not isinstance(metadata, Mapping) or len(metadata) == 0 or not all(isinstance(k, str) for k in metadata):
            raise _invalid("metadata must be a dict with at least one column")
        bare = {(k[len(_PREFIX):] if k.startswith(_PREFIX) else k): v for k, v in metadata.items()}
        return request(self._config, method="PATCH", path=path, body={"metadata": bare})

    def search(self, *, surface: str, field: str, q: str, limit: int | None = None) -> MetadataPage:
        """Full-text search over one declared text column of a collection.

        `field` is the column: `body` or `metadata.body`. Matching is by word stem, so "finance" does not match
        "financial". `limit` is 1 to 1000 (the gateway defaults to 50). Returns the first page only (there is no cursor
        input) and no rank.
        """
        path = self._path("search")
        target = _check_collection_surface(surface, "surface")
        wire_field = _prefixed(field, "field")
        if not isinstance(q, str) or not q:
            raise _invalid("q must be a non-empty string")
        _check_limit(limit, "limit")
        body: dict[str, Any] = {"surface": target, "field": wire_field, "q": q}
        if limit is not None:
            body["limit"] = limit
        return _page(request(self._config, method="POST", path=path, body=body))

    def subscribe(self, surface: str, *, from_sequence: int | None = None, timeout: Any = None,
                  on_resync: ResyncHandler | None = None, idle_timeout: float | None = None) -> AsyncIterator[dict[str, Any]]:
        """Follow changes of a `collection:<name>` surface live: `async for event in k9o.metadata.subscribe(...)`.

        Each event is `row_inserted`, `row_updated` or `row_deleted` with `payload == {"collection", "id"}` only, so read
        the record by id for its values; it also carries `eventId`. It is the change-feed reader of
        `realtime.subscribe`: it reconnects with `Last-Event-ID`, drops events it already delivered and calls
        `on_resync` when the position is lost. `from_sequence` starts from a known position and is sent on the first
        connection only. Changes to undeclared columns are reported too.
        """
        path = self._path("subscribe")
        target = _check_collection_surface(surface, "surface")
        if from_sequence is not None and (isinstance(from_sequence, bool) or not isinstance(from_sequence, int)
                                          or from_sequence < 0):
            raise _invalid("from_sequence must be a whole number, 0 or more", from_sequence=from_sequence)
        initial = {"fromSequence": from_sequence} if from_sequence is not None else None
        return open_feed(self._config, path, {"surface": target}, initial_query=initial, timeout=timeout,
                         on_resync=on_resync, idle_timeout=idle_timeout)
