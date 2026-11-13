"""The locate door: password sign-in asks the project which region holds the address, and sends the password only to
an origin that passes the rule in k9o/locate.py."""

import json

import httpx
import pytest

from k9o import KError, create_client
from k9o.locate import choose_located_origin

BASE = "https://acme.api-de01.k9o.io"

OK = [
    ("a sibling region of the same org, family and apex", "https://acme.api-us01.k9o.io",
     {"region": "us01", "origin": "https://acme.api-us01.k9o.io"}),
    ("the base's own region", BASE, {"region": "de01", "origin": BASE}),
    ("a trailing slash", "https://acme.api-us01.k9o.io", {"region": "us01", "origin": "https://acme.api-us01.k9o.io/"}),
]


@pytest.mark.parametrize("name,want,answer", OK, ids=[o[0] for o in OK])
def test_choose_uses(name, want, answer):
    assert choose_located_origin(BASE, answer) == want


REFUSED = [
    ("null origin (single host, self-hosted)", {"region": "de01", "origin": None}),
    ("a missing answer", None),
    ("http", {"region": "us01", "origin": "http://acme.api-us01.k9o.io"}),
    ("userinfo", {"region": "us01", "origin": "https://u:p@acme.api-us01.k9o.io"}),
    ("a port", {"region": "us01", "origin": "https://acme.api-us01.k9o.io:8443"}),
    ("a path", {"region": "us01", "origin": "https://acme.api-us01.k9o.io/x"}),
    ("a query", {"region": "us01", "origin": "https://acme.api-us01.k9o.io/?a=1"}),
    ("another org", {"region": "us01", "origin": "https://evil.api-us01.k9o.io"}),
    ("another apex", {"region": "us01", "origin": "https://acme.api-us01.evil.io"}),
    ("another family", {"region": "us01", "origin": "https://acme.api-staging-us01.k9o.io"}),
    ("a code that differs from region", {"region": "us01", "origin": "https://acme.api-fr01.k9o.io"}),
    ("a region that is not a code", {"region": "usa", "origin": "https://acme.api-usa.k9o.io"}),
    ("a host with no region", {"region": "us01", "origin": "https://acme.api.k9o.io"}),
    ("a region as a subdomain", {"region": "us01", "origin": "https://us01.acme.api-de01.k9o.io"}),
    ("a look-alike apex", {"region": "us01", "origin": "https://acme.api-us01.k9o.io.evil.io"}),
    ("a fragment", {"region": "us01", "origin": "https://acme.api-us01.k9o.io/#x"}),
    ("a non-string origin", {"region": "us01", "origin": 5}),
    ("an answer that is not an object", "us01"),
]


@pytest.mark.parametrize("name,answer", REFUSED, ids=[r[0] for r in REFUSED])
def test_choose_falls_back_to_the_base_for(name, answer):
    assert choose_located_origin(BASE, answer) == BASE


def test_http_only_when_the_base_is_http_on_loopback_and_then_only_the_same_host():
    assert choose_located_origin("http://localhost:8080", {"region": "de01", "origin": "http://localhost:8080"}) == "http://localhost:8080"
    assert choose_located_origin("http://localhost:8080", {"region": "de01", "origin": "http://evil.localhost:8080"}) == "http://localhost:8080"
    assert choose_located_origin("https://localhost", {"region": "de01", "origin": "http://localhost"}) == "https://localhost"


def test_base_that_is_not_a_gateway_host_shape_is_kept():
    assert choose_located_origin("https://gw.internal.example", {"region": "us01", "origin": "https://gw-us01.internal.example"}) == "https://gw.internal.example"


# ---- flows ----

SESSION = {"accessToken": "a", "refreshToken": "r-1", "expiresAt": "x", "refreshExpiresAt": "x"}
AUTH = {"user": {"id": "eu_1"}, "session": SESSION}


def harness(answer, **kw):
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return answer(req)

    c = create_client(base_url=BASE, project_slug="portal", api_key="secret-key",
                      default_headers={"x-extra": "1"}, **kw)
    c.config.transport = httpx.MockTransport(handler)
    return c, seen


def ok(data):
    return httpx.Response(200, json={"data": data, "meta": {}})


def door(region, origin):
    return ok({"region": region, "origin": origin})


def urls(seen):
    return [str(r.url) for r in seen if r.method != "HEAD"]


def test_sends_only_the_email_to_the_door_then_the_password_to_the_located_origin():
    c, seen = harness(lambda r: door("us01", "https://acme.api-us01.k9o.io") if r.url.path.endswith("/gate/locate") else ok(AUTH))
    c.gate.sign_in(email="a@b.co", password="pw")
    assert urls(seen) == [f"{BASE}/@portal/gate/locate", "https://acme.api-us01.k9o.io/@portal/gate/signin"]
    assert json.loads(seen[0].content) == {"email": "a@b.co"}
    assert "authorization" not in seen[0].headers and "cookie" not in seen[0].headers
    assert json.loads(seen[1].content) == {"email": "a@b.co", "password": "pw"}


def test_never_sends_the_password_to_an_origin_that_fails_the_rule():
    c, seen = harness(lambda r: door("us01", "https://evil.example") if r.url.path.endswith("/gate/locate") else ok(AUTH))
    c.gate.sign_in(email="a@b.co", password="pw")
    assert urls(seen)[1] == f"{BASE}/@portal/gate/signin"
    assert not any("evil.example" in u for u in urls(seen))


def test_uses_the_base_when_the_door_answers_origin_null():
    c, seen = harness(lambda r: door("de01", None) if r.url.path.endswith("/gate/locate") else ok(AUTH))
    c.gate.sign_in(email="a@b.co", password="pw")
    assert urls(seen)[1] == f"{BASE}/@portal/gate/signin"


def test_uses_the_base_when_the_door_answer_is_not_an_object():
    c, seen = harness(lambda r: ok("nope") if r.url.path.endswith("/gate/locate") else ok(AUTH))
    c.gate.sign_in(email="a@b.co", password="pw")
    assert urls(seen)[1] == f"{BASE}/@portal/gate/signin"


@pytest.mark.parametrize("status,code", [(503, "K9O_GATE_REGION_UNAVAILABLE"), (429, "K9O_RATE_LIMITED")])
def test_sends_nothing_to_sign_in_when_the_door_is_down_or_rate_limited(status, code):
    c, seen = harness(lambda r: httpx.Response(status, json={"code": code, "message": "x", "context": {}, "recommendation": None}))
    with pytest.raises(KError) as exc:
        c.gate.sign_in(email="a@b.co", password="pw")
    assert exc.value.code == code and exc.value.status == status
    assert len(urls(seen)) == 1  # the door once: no retry, no credential request


def test_uses_the_base_when_the_door_answers_404():
    def answer(r):
        if r.url.path.endswith("/gate/locate"):
            return httpx.Response(404, json={"code": "K9O_NOT_FOUND", "message": "x", "context": {}, "recommendation": None})
        return ok(AUTH)

    c, seen = harness(answer)
    c.gate.sign_in(email="a@b.co", password="pw")
    assert urls(seen) == [f"{BASE}/@portal/gate/locate", f"{BASE}/@portal/gate/signin"]
    assert json.loads(seen[1].content) == {"email": "a@b.co", "password": "pw"}


def test_a_404_on_the_credential_request_after_a_404_door_is_still_raised():
    c, seen = harness(lambda r: httpx.Response(404, json={"code": "K9O_NOT_FOUND", "message": "x", "context": {}, "recommendation": None}))
    with pytest.raises(KError) as exc:
        c.gate.sign_in(email="a@b.co", password="pw")
    assert exc.value.status == 404
    assert urls(seen) == [f"{BASE}/@portal/gate/locate", f"{BASE}/@portal/gate/signin"]


def test_does_not_cache_the_located_origin():
    answers = iter([door("us01", "https://acme.api-us01.k9o.io"), door("fr01", "https://acme.api-fr01.k9o.io")])

    def answer(r):
        return next(answers) if r.url.path.endswith("/gate/locate") else ok(AUTH)

    c, seen = harness(answer)
    c.gate.sign_in(email="a@b.co", password="pw")
    c.gate.sign_in(email="a@b.co", password="pw")
    assert sum(u.endswith("/gate/locate") for u in urls(seen)) == 2
    assert [u for u in urls(seen) if u.endswith("/signin")] == [
        "https://acme.api-us01.k9o.io/@portal/gate/signin", "https://acme.api-fr01.k9o.io/@portal/gate/signin"]


def test_refresh_and_sign_out_go_to_the_origin_that_issued_the_session():
    def answer(r):
        if r.url.path.endswith("/gate/locate"):
            return door("us01", "https://acme.api-us01.k9o.io")
        return ok(AUTH if r.url.path.endswith("/signin") else {"revoked": True})

    c, seen = harness(answer)
    c.gate.sign_in(email="a@b.co", password="pw")
    c.gate.refresh_token(refresh_token="r-1")
    c.gate.sign_out(refresh_token="r-1")
    got = urls(seen)
    assert "https://acme.api-us01.k9o.io/@portal/gate/refresh" in got
    assert "https://acme.api-us01.k9o.io/@portal/gate/signout" in got
    c.gate.refresh_token(refresh_token="r-1")  # the hint was removed on sign-out
    assert urls(seen)[-1] == f"{BASE}/@portal/gate/refresh"


def test_refresh_rotates_the_refresh_token_and_the_hint_follows_it():
    def answer(r):
        if r.url.path.endswith("/gate/locate"):
            return door("us01", "https://acme.api-us01.k9o.io")
        if r.url.path.endswith("/signin"):
            return ok(AUTH)
        return ok({"accessToken": "a2", "refreshToken": "r-2", "expiresAt": "x"})

    c, seen = harness(answer)
    c.gate.sign_in(email="a@b.co", password="pw")
    out = c.gate.refresh_token(refresh_token="r-1")
    assert out.data["refreshToken"] == "r-2"
    c.gate.refresh_token(refresh_token="r-2")
    got = [u for u in urls(seen) if u.endswith("/refresh")]
    assert got == ["https://acme.api-us01.k9o.io/@portal/gate/refresh"] * 2
    c.gate.sign_out(refresh_token="r-1")  # the old secret has no hint any more
    assert urls(seen)[-1] == f"{BASE}/@portal/gate/signout"


@pytest.mark.parametrize("reason", ["replay", "revoked", "inactive", "max_length"])
def test_a_refused_refresh_carries_the_session_expired_reason(reason):
    body = {"code": "K9O_SESSION_EXPIRED", "message": "The session has ended.", "context": {"reason": reason}}
    c, _ = harness(lambda r: httpx.Response(401, json=body))
    with pytest.raises(KError) as e:
        c.gate.refresh_token(refresh_token="r-1")
    assert e.value.code == "K9O_SESSION_EXPIRED"
    assert e.value.status == 401
    assert e.value.context["reason"] == reason


def test_the_hint_map_is_bounded():
    from k9o import locate

    for i in range(locate._HINT_LIMIT + 10):
        locate.remember_session_origin(f"bound-{i}", "https://acme.api-us01.k9o.io")
    assert locate.session_origin_hint("bound-0") is None
    assert locate.session_origin_hint(f"bound-{locate._HINT_LIMIT + 9}") == "https://acme.api-us01.k9o.io"
    assert len(locate._hints) <= locate._HINT_LIMIT


def test_sign_up_and_whoami_do_not_use_the_door():
    c, seen = harness(lambda r: ok(AUTH))
    c.gate.sign_up(email="a@b.co", password="pw-long-enough-1")
    c.gate.whoami()
    assert not any(u.endswith("/locate") for u in urls(seen))
