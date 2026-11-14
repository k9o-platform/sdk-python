"""
Schema namespace: `client.schema.*`.

Wire mapping (project plane):
  - collections.list(limit=, cursor=)   GET /@<slug>/schema/collections
  - collections.get(name)               GET /@<slug>/schema/collections/<name>

The list is cursor-paginated and ordered by collection name: pass `meta.cursor.next` of one page as `cursor` of the
next, until it is None.
"""

from __future__ import annotations

import re
from urllib.parse import quote

from .approvals import check_limit
from .envelope import Envelope
from .errors import KError
from .http import HttpClientConfig, request
from .url_grammar import project_path


_NAME_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]{0,62}$")


class SchemaCollectionsNamespace:
    def __init__(self, config: HttpClientConfig) -> None:
        self._config = config

    def list(self, *, limit: int | None = None, cursor: str | None = None) -> Envelope:
        """List the project's collections by name, one page at a time. `limit` is 1 to 200 (default 50)."""
        if not self._config.project_slug:
            raise ValueError("schema operations require project_slug bound on the client")
        check_limit(limit)
        if cursor is not None and not isinstance(cursor, str):
            raise KError(code="K9O_INVALID_FIELD_VALUE", message="cursor must be a string", context={"cursor": cursor})
        query = {k: v for k, v in {"limit": limit, "cursor": cursor}.items() if v is not None}
        return request(self._config, method="GET", path=project_path(self._config.project_slug, "schema/collections"),
                       query=query or None)

    def get(self, name: str) -> Envelope:
        """One collection with its columns, indexes and relationships. An unknown name is KError `K9O_COLLECTION_NOT_FOUND` (404)."""
        if not self._config.project_slug:
            raise ValueError("schema operations require project_slug bound on the client")
        if not isinstance(name, str) or not _NAME_RE.match(name):
            raise KError(code="K9O_INVALID_FIELD_VALUE", message="collection name must match ^[a-zA-Z_][a-zA-Z0-9_]{0,62}$",
                         context={"name": name})
        return request(self._config, method="GET",
                       path=project_path(self._config.project_slug, "schema/collections/" + quote(name, safe="")))


class SchemaNamespace:
    def __init__(self, config: HttpClientConfig) -> None:
        self.collections = SchemaCollectionsNamespace(config)
