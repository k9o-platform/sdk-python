"""client.vectors: wire shape, live-confirmed bodies, client-side validation and error decoding."""

from __future__ import annotations

import json

import httpx
import pytest

from k9o import KError, create_client

BASE = "https://acme.api.k9o.io"
META = {"cost": {}, "timing": {}, "context": {"requestId": "req_1"}}
SEARCH = "/@portal/vectors/_vec_example/search"
UPSERT = "/@portal/vectors/_vec_example/upsert"

UPSERT_OK = {"data": {"upserted": 3, "collection": "_vec_example"}, "meta": META}
NEAREST_OK = {
    "data": {
        "results": [
            {"id": "p1", "score": 1, "_source": {"handle": "probe", "chunkIndex": 0}, "contentHash": None,
             "text": "x axis", "metadata": {}, "model": None, "chunk": 0},
            {"id": "p2", "score": 0.8, "_source": {"handle": "probe", "chunkIndex": 1}, "contentHash": None,
             "text": "mostly x", "metadata": {}, "model": None, "chunk": 1},
        ],
        "collection": "_vec_example",
    },
    "meta": META,
}
NOT_FOUND = {"code": "K9O_COLLECTION_NOT_FOUND",
             "message": "derived vector collection '_vec_example' does not exist", "context": {}}
BAD_DIM = {"code": "K9O_INVALID_VECTOR_DIMENSION",
           "message": "query vector dimension does not match collection '_vec_example'",
           "context": {"collection": "_vec_example", "queryDim": 2}}


def _client(status=200, body=None):
    seen: list[httpx.Request] = []

    def handler(req):
        seen.append(req)
        return httpx.Response(status, json=body if body is not None else {"data": {}, "meta": META})

    c = create_client(base_url=BASE, project_slug="portal", api_key="k")
    c.config.transport = httpx.MockTransport(handler)
    return c, seen


def _only(seen):
    reqs = [r for r in seen if r.method != "HEAD"]
    assert len(reqs) == 1
    return reqs[0]


def test_nearest_minimal_body_and_result():
    c, seen = _client(200, NEAREST_OK)
    r = c.vectors.nearest("_vec_example", [1, 0, 0], top_k=2)
    req = _only(seen)
    assert (req.method, req.url.path, req.url.query) == ("POST", SEARCH, b"")
    assert json.loads(req.content) == {"queryVector": [1, 0, 0], "topK": 2}
    assert [h["id"] for h in r.data["results"]] == ["p1", "p2"]
    assert r.data["results"][0]["contentHash"] is None and r.data["results"][0]["model"] is None
    assert r.data["collection"] == "_vec_example"


def test_nearest_all_options():
    c, seen = _client(200, NEAREST_OK)
    c.vectors.nearest("_vec_example", (1.5, 0), top_k=5, min_score=0.25, metric="euclidean",
                      filters={"metadata.lang": "en", "id": "p1"})
    assert json.loads(_only(seen).content) == {
        "queryVector": [1.5, 0], "topK": 5, "minScore": 0.25, "metric": "euclidean",
        "filters": {"metadata.lang": "en", "id": "p1"}}


def test_nearest_min_score_zero_is_sent():
    c, seen = _client(200, NEAREST_OK)
    c.vectors.nearest("_vec_example", [1], min_score=0)
    assert json.loads(_only(seen).content) == {"queryVector": [1], "minScore": 0}


def test_upsert_body_and_result():
    c, seen = _client(200, UPSERT_OK)
    rows = [{"id": "p1", "vector": [1, 0, 0], "_source": {"handle": "probe", "chunkIndex": 0}, "text": "x axis",
             "metadata": {"lang": "en"}, "subjectIds": ["eu_YmGE3Z6nkv5i"]}]
    r = c.vectors.upsert("_vec_example", rows)
    req = _only(seen)
    assert (req.method, req.url.path) == ("POST", UPSERT)
    assert json.loads(req.content) == {"rows": rows}
    assert r.data == {"upserted": 3, "collection": "_vec_example"}


ROW = {"vector": [1, 0], "_source": {"handle": "h", "chunkIndex": 0}}
BAD_CALLS = {
    "collection without underscore": lambda c: c.vectors.nearest("notes", [1]),
    "collection not a string": lambda c: c.vectors.upsert(None, [ROW]),
    "empty vector": lambda c: c.vectors.nearest("_n", []),
    "vector too long": lambda c: c.vectors.nearest("_n", [0] * 16001),
    "vector not a list": lambda c: c.vectors.nearest("_n", "abc"),
    "nan": lambda c: c.vectors.nearest("_n", [1, float("nan")]),
    "infinity": lambda c: c.vectors.nearest("_n", [float("inf")]),
    "bool element": lambda c: c.vectors.nearest("_n", [True]),
    "string element": lambda c: c.vectors.nearest("_n", ["1"]),
    "top_k zero": lambda c: c.vectors.nearest("_n", [1], top_k=0),
    "top_k 1001": lambda c: c.vectors.nearest("_n", [1], top_k=1001),
    "top_k float": lambda c: c.vectors.nearest("_n", [1], top_k=2.5),
    "min_score nan": lambda c: c.vectors.nearest("_n", [1], min_score=float("nan")),
    "metric": lambda c: c.vectors.nearest("_n", [1], metric="manhattan"),
    "filters list": lambda c: c.vectors.nearest("_n", [1], filters=[("a", 1)]),
    "no rows": lambda c: c.vectors.upsert("_n", []),
    "1001 rows": lambda c: c.vectors.upsert("_n", [ROW] * 1001),
    "row not a dict": lambda c: c.vectors.upsert("_n", [ROW, 5]),
    "row without vector": lambda c: c.vectors.upsert("_n", [{"_source": ROW["_source"]}]),
    "row vector too long": lambda c: c.vectors.upsert("_n", [{**ROW, "vector": [0] * 16001}]),
    "row vector nan": lambda c: c.vectors.upsert("_n", [ROW, {**ROW, "vector": [float("nan")]}]),
}


@pytest.mark.parametrize("name", list(BAD_CALLS))
def test_validation_sends_nothing(name):
    c, seen = _client()
    with pytest.raises(KError) as e:
        BAD_CALLS[name](c)
    assert e.value.code == "K9O_INVALID_FIELD_VALUE"
    assert seen == []


def test_limits_accepted():
    c, seen = _client(200, UPSERT_OK)
    c.vectors.nearest("_n", [0] * 16000, top_k=1000)
    c.vectors.upsert("_n", [ROW] * 1000)
    assert len([r for r in seen if r.method != "HEAD"]) == 2


def test_requires_project_slug():
    c = create_client(base_url=BASE, api_key="k")
    with pytest.raises(ValueError):
        c.vectors.nearest("_n", [1])


def test_missing_collection_404_decodes():
    c, _ = _client(404, NOT_FOUND)
    with pytest.raises(KError) as e:
        c.vectors.nearest("_vec_example", [1, 0, 0])
    assert e.value.code == "K9O_COLLECTION_NOT_FOUND"
    assert e.value.status == 404
    assert "does not exist" in e.value.message


def test_wrong_dimension_400_decodes():
    c, _ = _client(400, BAD_DIM)
    with pytest.raises(KError) as e:
        c.vectors.nearest("_vec_example", [1, 0])
    assert e.value.code == "K9O_INVALID_VECTOR_DIMENSION"
    assert e.value.status == 400
    assert e.value.context == {"collection": "_vec_example", "queryDim": 2}
