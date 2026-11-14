"""
Vector collections namespace: `client.vectors.*`. A vector collection stores embeddings you computed yourself, next to
the text and metadata they came from, and answers nearest-neighbour searches over them.

Wire mapping (project plane):
  - nearest(collection, query_vector, top_k=, min_score=, metric=, filters=)  POST /@<slug>/vectors/<collection>/search
  - upsert(collection, rows)                                                  POST /@<slug>/vectors/<collection>/upsert

Both send the vector in the request body, so they take real embeddings of any common size (up to 16000 numbers).
The collection name must start with `_`. The first upsert creates the collection and fixes its dimension; every later
vector must have the same length. Searching a collection that was never upserted answers 404
`K9O_COLLECTION_NOT_FOUND`; a query of another length answers 400 `K9O_INVALID_VECTOR_DIMENSION`.

`nearest` answers `data == {"results": [...], "collection": "_notes"}`, closest first. Each result has `id`, `score`
(higher is more similar), `_source`, `contentHash`, `text`, `metadata`, `model` and `chunk`; `contentHash`, `text`,
`model` and `chunk` are None when not set. Rows passed to `upsert` are dicts with the gateway's field names:
`vector` and `_source` (`{"handle": ..., "chunkIndex": ...}`) are required; `id`, `contentHash`, `text`, `metadata`
and `subjectIds` (`["eu_..."]`) are optional. Arguments are checked before any request; a bad value raises KError
`K9O_INVALID_FIELD_VALUE`.
"""

from __future__ import annotations

import math
from typing import Any, Mapping, Sequence
from urllib.parse import quote

from .approvals import _invalid, _require_project_slug
from .envelope import Envelope
from .http import HttpClientConfig, request
from .url_grammar import project_path

_METRICS = ("cosine", "euclidean", "dot")
_MAX_VECTOR = 16000
_MAX_ROWS = 1000
_MAX_TOP_K = 1000


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _check_vector(vector: Any, name: str) -> None:
    if not isinstance(vector, (list, tuple)) or not 1 <= len(vector) <= _MAX_VECTOR:
        raise _invalid(f"{name} must be a list of 1 to {_MAX_VECTOR} numbers")
    if not all(_is_number(n) for n in vector):
        raise _invalid(f"{name} must contain only finite numbers")


class VectorsNamespace:
    def __init__(self, config: HttpClientConfig) -> None:
        self._config = config

    def _path(self, collection: Any, tail: str) -> str:
        if not isinstance(collection, str) or not collection.startswith("_"):
            raise _invalid("a vector collection name must start with _", collection=collection)
        slug = _require_project_slug(self._config, "vectors")
        return project_path(slug, f"vectors/{quote(collection, safe='')}/{tail}")

    def nearest(self, collection: str, query_vector: Sequence[float], *, top_k: int | None = None,
                min_score: float | None = None, metric: str | None = None,
                filters: Mapping[str, str | int | float | bool] | None = None) -> Envelope:
        """The rows nearest to `query_vector`, closest first. Answers 404 when the collection has never been upserted.

        `query_vector` is 1 to 16000 numbers, the same length as the stored vectors. `top_k` is 1 to 1000 (the gateway
        defaults to 10). `min_score` keeps only results whose score is at least that. `metric` is `cosine` (default),
        `euclidean` or `dot`. `filters` are equality checks on `id`, `contentHash`, `source.<key>` and
        `metadata.<key>`; every one must match.
        """
        path = self._path(collection, "search")
        _check_vector(query_vector, "query_vector")
        if top_k is not None and (isinstance(top_k, bool) or not isinstance(top_k, int)
                                  or not 1 <= top_k <= _MAX_TOP_K):
            raise _invalid(f"top_k must be a whole number from 1 to {_MAX_TOP_K}", top_k=top_k)
        if min_score is not None and not _is_number(min_score):
            raise _invalid("min_score must be a number", min_score=min_score)
        if metric is not None and metric not in _METRICS:
            raise _invalid(f"metric must be one of {', '.join(_METRICS)}", metric=metric)
        if filters is not None and not isinstance(filters, Mapping):
            raise _invalid("filters must be a dict of path to value")
        body: dict[str, Any] = {"queryVector": list(query_vector)}
        for key, value in (("topK", top_k), ("minScore", min_score), ("metric", metric),
                           ("filters", dict(filters) if filters is not None else None)):
            if value is not None:
                body[key] = value
        return request(self._config, method="POST", path=path, body=body)

    def upsert(self, collection: str, rows: Sequence[Mapping[str, Any]]) -> Envelope:
        """Insert or overwrite 1 to 1000 rows in one request. The first upsert creates the collection.

        Answers `data == {"upserted": <count>, "collection": <name>}`. An existing `id` is overwritten. Keep each
        request under the 2 MiB body limit.
        """
        path = self._path(collection, "upsert")
        if not isinstance(rows, (list, tuple)) or not 1 <= len(rows) <= _MAX_ROWS:
            raise _invalid(f"rows must be a list of 1 to {_MAX_ROWS} rows")
        for index, row in enumerate(rows):
            if not isinstance(row, Mapping):
                raise _invalid(f"rows[{index}] must be a dict")
            _check_vector(row.get("vector"), f"rows[{index}].vector")
        return request(self._config, method="POST", path=path, body={"rows": [dict(r) for r in rows]})
