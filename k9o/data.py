"""
Data-plane DSL, Python idiomatic shape:

    response = k9o.from_("posts").select()
    for row in response.data:
        print(row)

`from` is a Python keyword so the method is `from_("collection")`. Returns a chainable filter builder that resolves
to the Envelope on `.execute()` (sync) or `await .execute_async()`.

For ergonomic parity with the JS shape (where the builder is awaited
directly), Python's builder supports `__iter__` returning the data
list when used as an iterable AND `.execute()` to get the full
Envelope. Most consumers use `.execute()` explicitly for clarity.
"""

from __future__ import annotations

import json
import math
import re
from typing import Any, Sequence
from urllib.parse import quote

from .envelope import Envelope
from .errors import KError
from .http import HttpClientConfig, request, request_async
from .url_grammar import project_path


_FILTER_OPS = {"eq", "neq", "gt", "gte", "lt", "lte", "in", "notIn", "like", "ilike", "is"}


class _QueryBuilder:
    """Shared filter/page state across verb builders."""

    def __init__(self) -> None:
        self._filters: list[tuple[str, str, Any]] = []
        self._limit: int | None = None
        self._cursor: str | None = None
        self._count: str | None = None

    def eq(self, column: str, value: Any) -> "_QueryBuilder":
        self._filters.append((column, "eq", value))
        return self

    def neq(self, column: str, value: Any) -> "_QueryBuilder":
        self._filters.append((column, "neq", value))
        return self

    def gt(self, column: str, value: Any) -> "_QueryBuilder":
        self._filters.append((column, "gt", value))
        return self

    def gte(self, column: str, value: Any) -> "_QueryBuilder":
        self._filters.append((column, "gte", value))
        return self

    def lt(self, column: str, value: Any) -> "_QueryBuilder":
        self._filters.append((column, "lt", value))
        return self

    def lte(self, column: str, value: Any) -> "_QueryBuilder":
        self._filters.append((column, "lte", value))
        return self

    def in_(self, column: str, values: Sequence[Any]) -> "_QueryBuilder":
        self._filters.append((column, "in", list(values)))
        return self

    def not_in(self, column: str, values: Sequence[Any]) -> "_QueryBuilder":
        self._filters.append((column, "notIn", list(values)))
        return self

    def like(self, column: str, pattern: str) -> "_QueryBuilder":
        self._filters.append((column, "like", pattern))
        return self

    def ilike(self, column: str, pattern: str) -> "_QueryBuilder":
        self._filters.append((column, "ilike", pattern))
        return self

    def is_(self, column: str, value: str) -> "_QueryBuilder":
        # value must be "null" | "true" | "false"
        self._filters.append((column, "is", value))
        return self

    def filter(self, column: str, op: str, value: Any) -> "_QueryBuilder":
        if op not in _FILTER_OPS:
            raise KError(
                code="K9O_INVALID_FIELD_VALUE",
                message=f'unknown filter op "{op}"; valid: {sorted(_FILTER_OPS)}',
                context={"op": op},
            )
        self._filters.append((column, op, value))
        return self

    def limit(self, n: int) -> "_QueryBuilder":
        self._limit = n
        return self

    def cursor(self, opaque: str) -> "_QueryBuilder":
        self._cursor = opaque
        return self

    def count(self, mode: str) -> "_QueryBuilder":
        # mode: "none" | "exact" | "estimated"
        self._count = mode
        return self

    def _to_query(self) -> dict[str, Any]:
        q: dict[str, Any] = {}
        for col, op, val in self._filters:
            if isinstance(val, list):
                q[col] = f"{op}.({','.join(_wire(v) for v in val)})"
            else:
                q[col] = f"{op}.{_wire(val)}"
        if self._limit is not None:
            q["limit"] = self._limit
        if self._cursor is not None:
            q["cursor"] = self._cursor
        if self._count is not None:
            q["count"] = self._count
        return q


def _wire(value: Any) -> str:
    """Filter value as the gateway reads it: booleans are lower-case `true` / `false` (Python's `str(True)` is `True`)."""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _approval_query(require_approval: bool) -> dict[str, str] | None:
    """`{"requireApproval": "true"}` for a write that asks to be queued, no query at all otherwise."""
    return {"requireApproval": "true"} if require_approval else None


def _collection_path(config: HttpClientConfig, collection: str) -> str:
    if not config.project_slug:
        raise KError(
            code="K9O_MISSING_PROJECT_CONTEXT",
            message="data-plane operations require project_slug to be bound on the client",
            context={"collection": collection},
            recommendation="Pass project_slug= in create_client(); or use a project-scoped sub-client.",
        )
    if not re.match(r"^[a-z][a-z0-9_-]{0,62}$", collection):
        raise KError(
            code="K9O_INVALID_FIELD_VALUE",
            message=f'collection name "{collection}" does not match required format',
            context={"collection": collection},
            recommendation="Lowercase letters/digits/underscores/hyphens; start with letter; 1-63 chars.",
        )
    return project_path(config.project_slug, f"data/{collection}")


_SEARCH_COLUMN = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]{0,62}$")
_SEARCH_LANGUAGE = re.compile(r"^[a-zA-Z_]+$")
_TEXT_TYPES = ("plain", "phrase", "websearch")
_VECTOR_METRICS = ("cosine", "euclidean", "dot")
_BODY_TOP_LEVEL = ("select", "order", "limit", "cursor", "count", "or")


def _invalid(message: str, **context: Any) -> KError:
    return KError(code="K9O_INVALID_FIELD_VALUE", message=message, context=context)


def _check_column(column: Any) -> str:
    if not isinstance(column, str) or not _SEARCH_COLUMN.match(column):
        raise _invalid("search column must be a column name: letters, digits and underscores, starting with a letter or underscore")
    return column


class SelectBuilder(_QueryBuilder):
    """SELECT query builder. Call `.execute()` to issue the request."""

    def __init__(self, config: HttpClientConfig, collection: str) -> None:
        super().__init__()
        self._config = config
        self._collection = collection
        self._search: dict[str, str] = {}
        self._vector: tuple[str, list[float]] | None = None

    def text_search(self, column: str, query: str, *, language: str = "english", type: str = "plain") -> "SelectBuilder":
        """Full-text search over one text column. `type` is `plain`, `phrase` or `websearch`."""
        _check_column(column)
        if not isinstance(query, str) or query == "":
            raise _invalid("text search query must be a non-empty string")
        if not isinstance(language, str) or not _SEARCH_LANGUAGE.match(language):
            raise _invalid("text search language must be letters and underscores only")
        if type not in _TEXT_TYPES:
            raise _invalid(f"text search type must be one of {', '.join(_TEXT_TYPES)}")
        self._search["textSearch"] = f"{column}:{query}"
        self._search.pop("textSearchLanguage", None)
        self._search.pop("textSearchType", None)
        if language != "english":
            self._search["textSearchLanguage"] = language
        if type != "plain":
            self._search["textSearchType"] = type
        return self

    def vector_search(
        self, column: str, embedding: Sequence[float], *, metric: str = "cosine", limit: int | None = None
    ) -> "SelectBuilder":
        """Nearest-neighbour search over one vector column; results come back closest first.
        `metric` is `cosine`, `euclidean` or `dot`; `limit` is 1 to 1000 (server default 10).
        The embedding is sent in the request body, so real embeddings work. A query returns at most
        min(limit(), vector limit) rows (defaults 50 and 10): set both to get N rows. Cannot be combined
        with `text_search`."""
        _check_column(column)
        values = list(embedding) if embedding is not None else []
        if not values or any(
            isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in values
        ):
            raise _invalid("embedding must be a non-empty list of finite numbers")
        if metric not in _VECTOR_METRICS:
            raise _invalid(f"vector metric must be one of {', '.join(_VECTOR_METRICS)}")
        if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or limit < 1 or limit > 1000):
            raise _invalid("vector limit must be an integer from 1 to 1000")
        self._vector = (column, values)
        self._search["vectorSearch"] = f"{column}:{json.dumps(values, separators=(',', ':'))}"
        self._search.pop("vectorMetric", None)
        self._search.pop("vectorLimit", None)
        if metric != "cosine":
            self._search["vectorMetric"] = metric
        if limit is not None:
            self._search["vectorLimit"] = str(limit)
        return self

    def _to_query(self) -> dict[str, Any]:
        q = super()._to_query()
        q.update(self._search)
        return q

    def _search_body(self) -> dict[str, Any]:
        """Request body for `POST data/<collection>/search`, built from the same state as the GET query."""
        if "textSearch" in self._search:
            raise _invalid("vector_search and text_search cannot be combined on one query")
        assert self._vector is not None
        column, values = self._vector
        vector: dict[str, Any] = {"column": column, "vector": values}
        if "vectorMetric" in self._search:
            vector["metric"] = self._search["vectorMetric"]
        if "vectorLimit" in self._search:
            vector["limit"] = int(self._search["vectorLimit"])
        body: dict[str, Any] = {"vectorSearch": vector}
        filters: dict[str, Any] = {}
        for key, value in self._to_query().items():
            if key in ("vectorSearch", "vectorMetric", "vectorLimit"):
                continue
            if key in _BODY_TOP_LEVEL:
                body[key] = value
            else:
                filters[key] = value
        if filters:
            body["filter"] = filters
        return body

    def _request_args(self) -> dict[str, Any]:
        path = _collection_path(self._config, self._collection)
        if self._vector is not None:
            return {"method": "POST", "path": f"{path}/search", "body": self._search_body()}
        return {"method": "GET", "path": path, "query": self._to_query()}

    def execute(self) -> Envelope:
        return request(self._config, **self._request_args())

    async def execute_async(self) -> Envelope:
        return await request_async(self._config, **self._request_args())


class UpdateBuilder(_QueryBuilder):
    def __init__(self, config: HttpClientConfig, collection: str, patch: dict[str, Any]) -> None:
        super().__init__()
        self._config = config
        self._collection = collection
        self._patch = patch
        self._require_approval = False

    def _resolve_id(self) -> str:
        for col, op, val in self._filters:
            if col == "id" and op == "eq":
                return str(val)
        raise KError(
            code="K9O_INVALID_FIELD_VALUE",
            message="update() requires .eq('id', ...) filter; changing many rows in one call is not supported",
            context={"collection": self._collection},
            recommendation="Chain .eq('id', 'rec_…') before .execute(); to change several rows, call update once per id.",
        )

    def require_approval(self) -> "UpdateBuilder":
        """Ask for this update to wait for approval (`?requireApproval=true`). The answer is then either the queued
        change (HTTP 202) or, when an `auto_approve` policy covers the write, the updated row: check with `is_queued_write`."""
        self._require_approval = True
        return self

    def execute(self) -> Envelope:
        id_val = self._resolve_id()
        return request(
            self._config,
            method="PATCH",
            path=f"{_collection_path(self._config, self._collection)}/{quote(str(id_val), safe='')}",
            body=self._patch,
            query=_approval_query(self._require_approval),
        )


class DeleteBuilder(_QueryBuilder):
    def __init__(self, config: HttpClientConfig, collection: str) -> None:
        super().__init__()
        self._config = config
        self._collection = collection
        self._require_approval = False

    def require_approval(self) -> "DeleteBuilder":
        """Ask for this delete to wait for approval (`?requireApproval=true`). The answer is then either the queued
        change (HTTP 202) or, when an `auto_approve` policy covers the write, the deleted record: check with `is_queued_write`."""
        self._require_approval = True
        return self

    def _resolve_id(self) -> str:
        for col, op, val in self._filters:
            if col == "id" and op == "eq":
                return str(val)
        raise KError(
            code="K9O_INVALID_FIELD_VALUE",
            message="delete() requires .eq('id', ...) filter; deleting many rows in one call is not supported",
            context={"collection": self._collection},
        )

    def execute(self) -> Envelope:
        id_val = self._resolve_id()
        return request(
            self._config,
            method="DELETE",
            path=f"{_collection_path(self._config, self._collection)}/{quote(str(id_val), safe='')}",
            query=_approval_query(self._require_approval),
        )


class FromBuilder:
    """Terminal-verb builder returned by `k9o.from_(collection)`."""

    def __init__(self, config: HttpClientConfig, collection: str) -> None:
        self._config = config
        self._collection = collection

    def select(self) -> SelectBuilder:
        return SelectBuilder(self._config, self._collection)

    def insert(self, record: dict[str, Any], *, require_approval: bool = False) -> Envelope:
        """Insert a single record; returns Envelope with inserted record + meta.

        With `require_approval=True` the write asks to wait for approval (`?requireApproval=true`). The answer is then
        either the queued change (HTTP 202) or, when an `auto_approve` policy covers the write, the inserted row:
        check with `is_queued_write`.
        """
        return request(
            self._config,
            method="POST",
            path=_collection_path(self._config, self._collection),
            body=record,
            query=_approval_query(require_approval),
        )

    def update(self, patch: dict[str, Any]) -> UpdateBuilder:
        return UpdateBuilder(self._config, self._collection, patch)

    def delete(self) -> DeleteBuilder:
        return DeleteBuilder(self._config, self._collection)
