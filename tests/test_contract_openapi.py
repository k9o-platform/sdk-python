"""
Contract test: every public SDK call is sent through a recording transport and checked two ways.

  1. Exactly: HTTP method, path, query and JSON body are asserted per call (the table below is the wire inventory).
  2. Against the gateway's emitted OpenAPI (services/gateway/openapi.json): the (method, path) pair must be a
     declared route (`{param}` matches one path segment), every query parameter must be one the route declares (or be
     listed in EXTRA_QUERY with the handler that reads it), every declared required body field must be present, and
     no body key may fall outside the declared properties. Routes that declare no request body are checked by the
     exact assertions only (the gateway handler is the contract there).

A third test walks the client's public namespaces and fails when a public method is missing from the table, so a
method cannot be added or kept without a route.
"""

from __future__ import annotations

import inspect
import json
import re
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

import httpx
import pytest

from k9o import create_client

SPEC_PATH = Path(__file__).resolve().parents[3] / "services" / "gateway" / "openapi.json"
RAW = object()  # body is not JSON (binary / multipart): only the route is checked

BASE = "https://acme.api.k9o.io"

# Query parameters a gateway handler reads although its OpenAPI entry omits them.
EXTRA_QUERY: dict[tuple[str, str], set[str]] = {
    # services/gateway/src/routes/orgs-crud.ts GET /audit-log reads `type` and `limit`
    ("GET", "/audit-log"): {"type", "limit"},
    # services/gateway/src/routes/project-workflows.ts reads limit and cursor on the runs list
    ("GET", "/@{projectSlug}/workflows/{wflId}/runs"): {"limit", "cursor"},
    # services/gateway/src/routes/vault-secrets.ts reads op (and a legacy region) on the named-secret routes
    ("POST", "/@{projectSlug}/vault/secrets/{name}"): {"op", "region"},
    ("GET", "/@{projectSlug}/vault/secrets/{name}"): {"region"},
    ("PUT", "/@{projectSlug}/vault/secrets/{name}"): {"region"},
    ("DELETE", "/@{projectSlug}/vault/secrets/{name}"): {"region"},
    # services/gateway/src/routes/realtime: the SSE route reads topic and filter
    ("GET", "/@{projectSlug}/realtime/subscribe"): {"topic", "filter"},
    # services/gateway/src/routes/pulse-stream-consume.ts: the SSE route reads fromSequence
    ("GET", "/@{projectSlug}/data-streams/user/{streamSlug}/subscribe"): {"fromSequence"},
    # services/gateway/src/routes/pending-approvals.ts reads status, limit and cursor on the queue list
    ("GET", "/@{projectSlug}/pending-approvals"): {"status", "limit", "cursor"},
    # services/gateway/src/routes/pending-approvals.ts reads limit and cursor on the comment list
    ("GET", "/@{projectSlug}/pending-approvals/{pendingId}/comments"): {"limit", "cursor"},
    # services/gateway/src/routes/end-user-role-grants.ts reads endUserId on the list
    ("GET", "/@{projectSlug}/end-user-role-grants"): {"endUserId"},
    # services/gateway/src/routes/collection-records.ts: single-record writes read requireApproval
    ("POST", "/@{projectSlug}/data/{collection}"): {"requireApproval"},
    ("PATCH", "/@{projectSlug}/data/{collection}/{id}"): {"requireApproval"},
    ("DELETE", "/@{projectSlug}/data/{collection}/{id}"): {"requireApproval"},
    # dataset routes: the emitted OpenAPI describes these query parameters in prose but does not declare them
    ("POST", "/@{projectSlug}/analytics/{datasetSlug}/write"): {"onError"},
    ("POST", "/@{projectSlug}/analytics/{datasetSlug}/upload"): {"format", "dryRun", "onError", "delimiter", "encoding", "hasHeader"},
    ("GET", "/@{projectSlug}/rollups/{rollupSlug}/read"): {"from", "to", "dims"},
    # services/gateway/src/routes/collection-records.ts: data-plane filters are column=op.value query keys
}
# Routes the SDK calls that are not in this tree's OpenAPI yet: exempt from the route check only. Remove the entry
# when the route lands (test_pending_routes_are_not_yet_in_openapi fails once it is declared).
PENDING_ROUTES: dict[str, str] = {
}
# Body fields a route reads although this tree's OpenAPI omits them (the audit tail's replay cursor).
EXTRA_BODY: dict[tuple[str, str], set[str]] = {("POST", "/@{projectSlug}/realtime/replay"): {"afterAuditEventId"}}
# Data-plane routes take column filters as free query keys.
FREE_QUERY_ROUTES = {("GET", "/@{projectSlug}/data/{collection}")}


def _client(**kw):
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        if req.method == "HEAD":
            return httpx.Response(200)
        if req.url.path == "/rampart/jwks":  # the key feed has no data envelope
            return httpx.Response(200, json={"keys": [], "meta": {}})
        data = {"custodyMode": "PLATFORM_MANAGED", "epoch": "ep-1", "token": "t.t.t", "expiresAt": "2026-11-02T22:15:00.000Z", "issuer": "https://api.k9o.io/kms-federation/p1/", "subject": "p1:crypt:pii:op:encrypt", "audience": "sts.amazonaws.com", "kid": "k1", "items": [{"ciphertext": "Y3Q="}], "value": "eA==", "secrets": [{"name": "db/pw"}], "nextCursor": None,
                "grants": [], "grant": {},
                "classification": "clear", "riskScore": 0, "confidence": 9000, "categories": [],
                "calibrationVersion": "cal-1", "classificationReceipt": "a.b.c",
                "crypts": [], "name": "pii", "status": "active", "createdAt": "2026-11-02T10:00:00.000Z", "retiredAt": None}
        return httpx.Response(200, json={"data": data, "meta": {}})

    c = create_client(base_url=BASE, project_slug="portal", api_key="k", **kw)
    c.config.transport = httpx.MockTransport(handler)
    return c, seen


def _sent(call) -> httpx.Request:
    c, seen = _client()
    call(c)
    reqs = [r for r in seen if r.method != "HEAD"]
    if reqs and reqs[0].url.path == P + "/gate/locate":  # sign_in asks the locate door first (test_locate_door_wire)
        reqs = reqs[1:]
    if reqs and reqs[-1].url.path.endswith("/crypto/encrypt"):  # encrypt checks the custody mode first, then sends once
        assert [r.url.path for r in reqs[:-1]] == [P + "/crypto/mode"], f"unexpected requests {[r.url.path for r in reqs]}"
        return reqs[-1]
    assert len(reqs) == 1, f"expected one request, got {len(reqs)}"
    return reqs[0]


MANIFEST = {"tier": "readonly", "projects": ["portal"], "resourceClasses": None, "collections": None, "cidrs": None}
MIGRATION = {"version": 1, "slug": "init", "up": [], "down": []}
P = "/@portal"

# (id, call, method, path, query, body)
TABLE = [
    # projects
    ("projects.list", lambda c: c.projects.list(), "GET", "/projects", {}, None),
    ("projects.create", lambda c: c.projects.create(display_name="Demo", slug="demo", custody_mode="PLATFORM_MANAGED"),
     "POST", "/projects", {}, {"displayName": "Demo", "slug": "demo", "custodyMode": "PLATFORM_MANAGED"}),
    ("projects.get", lambda c: c.projects.get("demo"), "GET", "/projects/demo", {}, None),
    ("projects.update", lambda c: c.projects.update("demo", display_name="Renamed"),
     "PATCH", "/projects/demo", {}, {"displayName": "Renamed"}),
    # functions
    ("functions.upload_artifact", lambda c: c.functions.upload_artifact(b"\x00asm", "wasm"),
     "POST", P + "/functions/artifacts", {}, RAW),
    ("functions.deploy", lambda c: c.functions.deploy(name="hello", runtime="bun", tier="sandbox",
                                                       artifact_key="functions/p/abc.tar", version="1"),
     "POST", P + "/functions", {},
     {"name": "hello", "runtime": "bun", "tier": "sandbox", "artifactKey": "functions/p/abc.tar", "version": "1"}),
    ("functions.list", lambda c: c.functions.list(limit=10, status="active"), "GET", P + "/functions",
     {"limit": "10", "status": "active"}, None),
    ("functions.get", lambda c: c.functions.get("hello"), "GET", P + "/functions/hello", {}, None),
    ("functions.delete", lambda c: c.functions.delete("hello"), "DELETE", P + "/functions/hello", {}, None),
    ("functions.invoke", lambda c: c.functions.invoke("hello", {"a": 1}), "POST", P + "/functions/hello/invoke",
     {}, {"payload": {"a": 1}}),
    # policies
    ("policies.current", lambda c: c.policies.current(), "GET", P + "/policy", {}, None),
    ("policies.list", lambda c: c.policies.list(status="published"), "GET", P + "/policies",
     {"status": "published"}, None),
    ("policies.create", lambda c: c.policies.create(policy={"collections": {}}, editor_mode="json"),
     "POST", P + "/policies", {}, {"policy": {"collections": {}}, "editorMode": "json"}),
    ("policies.validate", lambda c: c.policies.validate(policy={"collections": {}}),
     "POST", P + "/policies/validate", {}, {"policy": {"collections": {}}}),
    ("policies.get", lambda c: c.policies.get("pol_1"), "GET", P + "/policies/pol_1", {}, None),
    ("policies.update", lambda c: c.policies.update("pol_1", policy={"collections": {}}),
     "PATCH", P + "/policies/pol_1", {}, {"policy": {"collections": {}}}),
    ("policies.delete", lambda c: c.policies.delete("pol_1"), "DELETE", P + "/policies/pol_1", {}, None),
    ("policies.publish", lambda c: c.policies.publish("pol_1"), "POST", P + "/policies/pol_1/publish", {}, None),
    # audit
    ("audit.query", lambda c: c.audit.query(limit=10), "GET", P + "/audit-log", {"limit": "10"}, None),
    ("audit.query.filters", lambda c: c.audit.query(event_type="auth.sign_in", actor_id="u_1",
                                                    after="2026-01-01", before="2026-02-01"),
     "GET", P + "/audit-log",
     {"eventType": "auth.sign_in", "actorId": "u_1", "after": "2026-01-01", "before": "2026-02-01"}, None),
    ("audit.tail", lambda c: c.audit.tail(since_minutes=5), "GET", P + "/audit-log", {"after": "*", "limit": "100"}, None),
    # orgs / org
    ("orgs.list", lambda c: c.orgs.list(), "GET", "/orgs", {}, None),
    ("orgs.get", lambda c: c.orgs.get("acme"), "GET", "/orgs/acme", {}, None),
    ("orgs.restore", lambda c: c.orgs.restore("org_1"), "POST", "/orgs/org_1/restore", {}, None),
    ("orgs.current", lambda c: c.orgs.current(), "GET", "/org", {}, None),
    ("org.get", lambda c: c.org.get(), "GET", "/org", {}, None),
    ("org.members.list", lambda c: c.org.members.list(status="active", acceptance_status="accepted"), "GET",
     "/members", {"status": "active", "acceptanceStatus": "accepted"}, None),
    ("org.members.invite", lambda c: c.org.members.invite(email="a@b.co", role="ORG_MEMBER"), "POST", "/members", {},
     {"email": "a@b.co", "role": "ORG_MEMBER"}),
    ("org.members.remove", lambda c: c.org.members.remove("u_1"), "DELETE", "/members/u_1", {}, None),
    ("org.groups.list", lambda c: c.org.groups.list(user_id="u_1"), "GET", "/groups", {"userId": "u_1"}, None),
    ("org.groups.get", lambda c: c.org.groups.get("grp_1"), "GET", "/groups/grp_1", {}, None),
    ("org.audit_log.query", lambda c: c.org.audit_log.query(type="data.write.org.role_granted", limit=10), "GET",
     "/audit-log", {"type": "data.write.org.role_granted", "limit": "10"}, None),
    ("org.audit_log.tail", lambda c: c.org.audit_log.tail(since_minutes=5), "GET", "/audit-log", {"limit": "100"}, None),
    ("org.gate.roles.list", lambda c: c.org.gate.roles.list(), "GET", "/role-grants", {}, None),
    ("org.gate.roles.grant", lambda c: c.org.gate.roles.grant(user_id="u_1", role="ORG_ADMIN"), "POST",
     "/role-grants", {}, {"userId": "u_1", "role": "ORG_ADMIN"}),
    ("org.gate.roles.get", lambda c: c.org.gate.roles.get("g_1"), "GET", "/role-grants/g_1", {}, None),
    ("org.gate.roles.revoke", lambda c: c.org.gate.roles.revoke("g_1"), "DELETE", "/role-grants/g_1", {}, None),
    ("org.gate.idp_providers.list", lambda c: c.org.gate.idp_providers.list(), "GET", "/identity-providers", {}, None),
    ("org.gate.idp_providers.create",
     lambda c: c.org.gate.idp_providers.create(idp_provider="saml", display_name="Okta", client_id="cid",
                                               client_secret_ref="ref_1"),
     "POST", "/identity-providers", {},
     {"idpProvider": "saml", "displayName": "Okta", "clientId": "cid", "clientSecretRef": "ref_1"}),
    ("org.gate.idp_providers.get", lambda c: c.org.gate.idp_providers.get("idp_1"), "GET",
     "/identity-providers/idp_1", {}, None),
    ("org.gate.idp_providers.configure", lambda c: c.org.gate.idp_providers.configure("idp_1", enabled=True),
     "PATCH", "/identity-providers/idp_1", {}, {"enabled": True}),
    ("org.gate.idp_providers.delete", lambda c: c.org.gate.idp_providers.delete("idp_1"), "DELETE",
     "/identity-providers/idp_1", {}, None),
    ("org.smtp.set", lambda c: c.org.smtp.set(host="smtp.x.io", port=587, username="u", password_ref="ref_1",
                                              from_address="a@b.co", from_name="A"),
     "PATCH", "/org", {},
     {"smtpConfig": {"host": "smtp.x.io", "port": 587, "username": "u", "passwordRef": "ref_1",
                     "fromAddress": "a@b.co", "fromName": "A"}}),
    ("org.smtp.test", lambda c: c.org.smtp.test(to="a@b.co"), "POST", "/org/smtp/test-email", {}, {"to": "a@b.co"}),
    ("org.smtp.unset", lambda c: c.org.smtp.unset(), "PATCH", "/org", {}, {"smtpConfig": None}),
    ("org.default_llm.get", lambda c: c.org.default_llm.get(), "GET", "/default-llm", {}, None),
    ("org.default_llm.set", lambda c: c.org.default_llm.set(model_id="llama-3-8b"), "PATCH", "/default-llm", {},
     {"modelId": "llama-3-8b"}),
    # me
    ("me.profile.get", lambda c: c.me.profile.get(), "GET", "/me", {}, None),
    ("me.profile.update", lambda c: c.me.profile.update(display_name="A"), "PATCH", "/me", {}, {"displayName": "A"}),
    ("me.sessions.list", lambda c: c.me.sessions.list(), "GET", "/me/sessions", {}, None),
    ("me.sessions.revoke", lambda c: c.me.sessions.revoke("s_1"), "POST", "/auth/sessions/s_1/revoke", {}, None),
    ("me.sessions.revoke_all", lambda c: c.me.sessions.revoke_all(), "POST", "/me/sessions/revoke-all", {}, None),
    ("me.api_tokens.list", lambda c: c.me.api_tokens.list(), "GET", "/me/api-tokens", {}, None),
    ("me.api_tokens.create", lambda c: c.me.api_tokens.create(label="ci", scope_manifest=MANIFEST, ttl_seconds=3600),
     "POST", "/me/api-tokens", {}, {"label": "ci", "scopeManifest": MANIFEST, "ttlSeconds": 3600}),
    ("me.api_tokens.delete", lambda c: c.me.api_tokens.delete("pat_1"), "DELETE", "/me/api-tokens/pat_1", {}, None),
    # gate
    ("gate.sign_up", lambda c: c.gate.sign_up(email="a@b.co", password="pw", display_name="A"), "POST",
     P + "/gate/signup", {}, {"email": "a@b.co", "password": "pw", "displayName": "A"}),
    ("gate.sign_in", lambda c: c.gate.sign_in(email="a@b.co", password="pw"), "POST", P + "/gate/signin", {},
     {"email": "a@b.co", "password": "pw"}),
    ("gate.sign_out", lambda c: c.gate.sign_out(refresh_token="rt"), "POST", P + "/gate/signout", {},
     {"refreshToken": "rt"}),
    ("gate.whoami", lambda c: c.gate.whoami(), "GET", P + "/gate/whoami", {}, None),
    ("gate.revoke_all_sessions", lambda c: c.gate.revoke_all_sessions(), "POST", P + "/auth/sessions/revoke-all", {}, None),
    ("gate.refresh_token", lambda c: c.gate.refresh_token(refresh_token="rt"), "POST", P + "/gate/refresh", {},
     {"refreshToken": "rt"}),
    # migrations
    ("migrations.list", lambda c: c.migrations.list(), "GET", P + "/migrations", {}, None),
    ("migrations.get", lambda c: c.migrations.get(1), "GET", P + "/migrations/1", {}, None),
    ("migrations.apply", lambda c: c.migrations.apply([MIGRATION]), "POST", P + "/migrations/apply", {},
     {"migrations": [MIGRATION]}),
    ("migrations.queue", lambda c: c.migrations.queue([MIGRATION]), "POST", P + "/migrations/queue", {},
     {"migrations": [MIGRATION]}),
    ("migrations.list_pending", lambda c: c.migrations.list_pending(), "GET", P + "/migrations/pending", {}, None),
    ("migrations.apply_pending", lambda c: c.migrations.apply_pending("pm_1"), "POST",
     P + "/migrations/pending/pm_1/apply", {}, None),
    ("migrations.reject_pending", lambda c: c.migrations.reject_pending("pm_1", "no"), "POST",
     P + "/migrations/pending/pm_1/reject", {}, {"reason": "no"}),
    ("migrations.delete_pending", lambda c: c.migrations.delete_pending("pm_1"), "DELETE",
     P + "/migrations/pending/pm_1", {}, None),
    # storage
    ("storage.list_buckets", lambda c: c.storage.list_buckets(), "GET", P + "/storage/buckets", {}, None),
    ("storage.get_bucket", lambda c: c.storage.get_bucket("docs"), "GET", P + "/storage/buckets/docs", {}, None),
    ("storage.create_bucket", lambda c: c.storage.create_bucket("docs", public=False), "POST",
     P + "/storage/buckets", {}, {"name": "docs", "public": False}),
    ("storage.update_bucket", lambda c: c.storage.update_bucket("docs", public=True), "PATCH",
     P + "/storage/buckets/docs", {}, {"public": True}),
    ("storage.delete_bucket", lambda c: c.storage.delete_bucket("docs", empty=True), "DELETE",
     P + "/storage/buckets/docs", {"empty": "true"}, None),
    ("storage.from_.upload", lambda c: c.storage.from_("docs").upload("a.txt", "hello"), "POST",
     P + "/storage/buckets/docs/objects", {}, RAW),
    ("storage.from_.create_signed_url", lambda c: c.storage.from_("docs").create_signed_url("a.txt", expires_in=600),
     "POST", P + "/storage/buckets/docs/objects/sign", {}, {"path": "a.txt", "expiresIn": 600}),
    ("storage.from_.list", lambda c: c.storage.from_("docs").list(prefix="a"), "GET",
     P + "/storage/buckets/docs/objects", {"prefix": "a"}, None),
    ("storage.from_.remove", lambda c: c.storage.from_("docs").remove("a.txt"), "DELETE",
     P + "/storage/buckets/docs/objects", {"path": "a.txt"}, None),
    # sql query and search
    ("query", lambda c: c.query("SELECT * FROM users WHERE data->>'role' = :role", {"role": "admin"}), "POST",
     P + "/query", {}, {"sql": "SELECT * FROM users WHERE data->>'role' = $1", "params": ["admin"]}),
    ("search", lambda c: c.search("articles", column="body", q="climate", limit=5), "GET", P + "/data/articles",
     {"textSearch": "body:climate", "limit": "5"}, None),
    # ai and compute
    ("ai.embed", lambda c: c.ai.embed(["a", "b"]), "POST", P + "/ai/embeddings", {}, {"input": ["a", "b"]}),
    ("ai.generate", lambda c: c.ai.generate("Say hi", max_tokens=20), "POST", P + "/ai/generate", {},
     {"prompt": "Say hi", "maxTokens": 20}),
    ("compute.llms.list", lambda c: c.compute.llms.list(), "GET", P + "/compute/llms", {}, None),
    ("compute.llms.read", lambda c: c.compute.llms.read("llama-3-8b"), "GET", P + "/compute/llms/llama-3-8b", {}, None),
    ("compute.llms.invoke", lambda c: c.compute.llms.invoke("llama-3-8b", prompt="hi",
                                                           params={"maxTokens": 10, "temperature": 0.2}),
     "POST", P + "/compute/llms/llama-3-8b/invoke", {}, {"prompt": "hi", "maxTokens": 10, "temperature": 0.2}),
    # studio
    ("studio.workflows.list", lambda c: c.studio.workflows.list(status="active"), "GET", P + "/workflows",
     {"status": "active"}, None),
    ("studio.workflows.create",
     lambda c: c.studio.workflows.create({"name": "w", "triggerType": "manual", "triggerConfig": {},
                                          "actions": [{"index": 0, "action_type": "branch", "config": {}}]}),
     "POST", P + "/workflows", {},
     {"name": "w", "triggerType": "manual", "triggerConfig": {},
      "actions": [{"index": 0, "action_type": "branch", "config": {}}]}),
    ("studio.workflows.read", lambda c: c.studio.workflows.read("wf_1"), "GET", P + "/workflows/wf_1", {}, None),
    ("studio.workflows.update", lambda c: c.studio.workflows.update("wf_1", {"name": "w2"}), "PATCH",
     P + "/workflows/wf_1", {}, {"name": "w2"}),
    ("studio.workflows.delete", lambda c: c.studio.workflows.delete("wf_1"), "DELETE", P + "/workflows/wf_1", {}, None),
    ("studio.workflows.trigger", lambda c: c.studio.workflows.trigger("wf_1", {"k": "v"}), "POST",
     P + "/workflows/wf_1/runs", {}, {"triggerContext": {"k": "v"}}),
    ("studio.workflows.runs.list", lambda c: c.studio.workflows.runs.list("wf_1", limit=5), "GET",
     P + "/workflows/wf_1/runs", {"limit": "5"}, None),
    ("studio.workflows.runs.read", lambda c: c.studio.workflows.runs.read("wf_1", "run_1"), "GET",
     P + "/workflows/wf_1/runs/run_1", {}, None),
    ("studio.workflows.runs.cancel", lambda c: c.studio.workflows.runs.cancel("wf_1", "run_1"), "POST",
     P + "/workflows/wf_1/runs/run_1/cancel", {}, None),
    ("studio.dashboards.list", lambda c: c.studio.dashboards.list(pinned_only=True), "GET", P + "/dashboards",
     {"pinnedOnly": "true"}, None),
    ("studio.dashboards.create", lambda c: c.studio.dashboards.create({"name": "d"}), "POST", P + "/dashboards", {},
     {"name": "d"}),
    ("studio.dashboards.read", lambda c: c.studio.dashboards.read("d_1"), "GET", P + "/dashboards/d_1", {}, None),
    ("studio.dashboards.update", lambda c: c.studio.dashboards.update("d_1", {"name": "d2"}), "PATCH",
     P + "/dashboards/d_1", {}, {"name": "d2"}),
    ("studio.dashboards.delete", lambda c: c.studio.dashboards.delete("d_1"), "DELETE", P + "/dashboards/d_1", {}, None),
    ("studio.dashboards.pin", lambda c: c.studio.dashboards.pin("d_1"), "POST", P + "/dashboards/d_1/pin", {}, None),
    ("studio.dashboards.unpin", lambda c: c.studio.dashboards.unpin("d_1"), "POST", P + "/dashboards/d_1/unpin", {}, None),
    # kibo vault
    ("kibo.vault.secrets.list", lambda c: c.kibo.vault.secrets.list(prefix="db/"), "GET", P + "/vault/secrets",
     {"prefix": "db/"}, None),
    ("kibo.vault.secrets.get", lambda c: c.kibo.vault.secrets.get("db/pw"), "GET", P + "/vault/secrets",
     {"prefix": "db/pw", "limit": "100"}, None),
    ("kibo.vault.secrets.create", lambda c: c.kibo.vault.secrets.create("db/pw", value="x"), "POST",
     P + "/vault/secrets/db%2Fpw", {}, {"value": "eA=="}),
    ("kibo.vault.secrets.reveal_challenge", lambda c: c.kibo.vault.secrets.reveal_challenge("db/pw"), "POST",
     P + "/vault/secrets/db%2Fpw", {"op": "reveal-challenge"}, None),
    ("kibo.vault.secrets.reveal", lambda c: c.kibo.vault.secrets.reveal("db/pw", challenge="ch"), "GET",
     P + "/vault/secrets/db%2Fpw", {}, None),
    ("kibo.vault.secrets.update", lambda c: c.kibo.vault.secrets.update("db/pw", value="y"), "PUT",
     P + "/vault/secrets/db%2Fpw", {}, {"value": "eQ=="}),
    ("kibo.vault.secrets.rotate", lambda c: c.kibo.vault.secrets.rotate("db/pw"), "POST",
     P + "/vault/secrets/db%2Fpw", {"op": "rotate"}, None),
    ("kibo.vault.secrets.move", lambda c: c.kibo.vault.secrets.move("db/pw", new_path="db/pw2"), "POST",
     P + "/vault/secrets/db%2Fpw", {"op": "move"}, {"newPath": "db/pw2"}),
    ("kibo.vault.secrets.delete", lambda c: c.kibo.vault.secrets.delete("db/pw"), "DELETE",
     P + "/vault/secrets/db%2Fpw", {}, None),
    # streams
    ("streams.create", lambda c: c.streams.create("Orders feed", 1073741824, retention_seconds=3600), "POST",
     P + "/data-streams/user", {}, {"displayName": "Orders feed", "maxBytes": 1073741824, "retentionSeconds": 3600}),
    ("streams.list", lambda c: c.streams.list(), "GET", P + "/data-streams/user", {}, None),
    ("streams.get", lambda c: c.streams.get("orders-feed"), "GET", P + "/data-streams/user/orders-feed", {}, None),
    ("streams.delete", lambda c: c.streams.delete("orders-feed"), "DELETE", P + "/data-streams/user/orders-feed",
     {}, None),
    ("streams.publish", lambda c: c.streams.publish("orders-feed", {"id": 1}, subject="eu.new"), "POST",
     P + "/data-streams/user/orders-feed/publish", {}, {"data": {"id": 1}, "subject": "eu.new"}),
    ("streams.replay", lambda c: c.streams.replay("orders-feed", from_sequence=5, until_sequence=9, limit=3), "POST",
     P + "/data-streams/user/orders-feed/replay", {}, {"fromSequence": 5, "untilSequence": 9, "limit": 3}),
    # approvals, approval policies, schema (the OpenAPI entries declare no bodies; these are the ones the handlers read)
    ("approvals.list", lambda c: c.approvals.list(status="pending_approval", limit=20, cursor="abc"), "GET",
     P + "/pending-approvals", {"status": "pending_approval", "limit": "20", "cursor": "abc"}, None),
    ("approvals.get", lambda c: c.approvals.get("apr_1"), "GET", P + "/pending-approvals/apr_1", {}, None),
    ("approvals.approve", lambda c: c.approvals.approve("apr_1", reason="ok"), "POST",
     P + "/pending-approvals/apr_1/approve", {}, {"reason": "ok"}),
    ("approvals.reject", lambda c: c.approvals.reject("apr_1", "duplicate"), "POST",
     P + "/pending-approvals/apr_1/reject", {}, {"reason": "duplicate"}),
    ("approvals.comments.add", lambda c: c.approvals.comments.add("apr_1", "Check the line items.", "approvers_only"),
     "POST", P + "/pending-approvals/apr_1/comments", {},
     {"body": "Check the line items.", "visibility": "approvers_only"}),
    ("approvals.comments.list", lambda c: c.approvals.comments.list("apr_1", limit=20, cursor="abc"), "GET",
     P + "/pending-approvals/apr_1/comments", {"limit": "20", "cursor": "abc"}, None),
    ("approvals.bulk", lambda c: c.approvals.bulk("reject", {"eq": ["change.table", "orders"]}, reason="stale"),
     "POST", P + "/pending-approvals/bulk", {},
     {"action": "reject", "criteria": {"eq": ["change.table", "orders"]}, "reason": "stale"}),
    ("approval_policies.list", lambda c: c.approval_policies.list(), "GET", P + "/approval-policies", {}, None),
    ("approval_policies.create", lambda c: c.approval_policies.create(
        "orders", "update", "queue", {"gt": ["change.row.amount", 1000]}), "POST", P + "/approval-policies", {},
     {"collection": "orders", "operation": "update", "action": "queue",
      "predicate": {"gt": ["change.row.amount", 1000]}}),
    ("approval_policies.delete", lambda c: c.approval_policies.delete("apo_1"), "DELETE",
     P + "/approval-policies/apo_1", {}, None),
    ("schema.collections.list", lambda c: c.schema.collections.list(limit=10, cursor="abc"), "GET",
     P + "/schema/collections", {"limit": "10", "cursor": "abc"}, None),
    ("schema.collections.get", lambda c: c.schema.collections.get("orders"), "GET", P + "/schema/collections/orders", {}, None),
    # triggers, schedules, end users
    ("functions.triggers.list", lambda c: c.functions.triggers.list("nightly"), "GET",
     P + "/functions/nightly/triggers", {}, None),
    ("functions.triggers.create", lambda c: c.functions.triggers.create(
        "nightly", collection="orders", events=["insert", "update"], filter={"eq": ["resource.status", "paid"]},
        enabled=True, log_ok_invocations=False), "POST", P + "/functions/nightly/triggers", {},
     {"collection": "orders", "events": ["insert", "update"], "filter": {"eq": ["resource.status", "paid"]},
      "enabled": True, "logOkInvocations": False}),
    ("functions.triggers.update", lambda c: c.functions.triggers.update("nightly", "trg_1", filter=None, enabled=False),
     "PATCH", P + "/functions/nightly/triggers/trg_1", {}, {"filter": None, "enabled": False}),
    ("functions.triggers.delete", lambda c: c.functions.triggers.delete("nightly", "trg_1"), "DELETE",
     P + "/functions/nightly/triggers/trg_1", {}, None),
    ("schedules.list", lambda c: c.schedules.list(status="active", function_name="nightly", limit=5, cursor="abc"),
     "GET", P + "/forge/schedules", {"status": "active", "function_name": "nightly", "limit": "5", "cursor": "abc"},
     None),
    ("schedules.get", lambda c: c.schedules.get("trg_1"), "GET", P + "/forge/schedules/trg_1", {}, None),
    ("schedules.create", lambda c: c.schedules.create(
        function_name="nightly", schedule_expression="0 0 * * *", schedule_timezone="UTC", catchup_policy="skip",
        collection="orders", enabled=True, log_ok_invocations=False), "POST", P + "/forge/schedules", {},
     {"functionName": "nightly", "scheduleExpression": "0 0 * * *", "scheduleTimezone": "UTC",
      "catchupPolicy": "skip", "collection": "orders", "enabled": True, "logOkInvocations": False}),
    ("schedules.update", lambda c: c.schedules.update("trg_1", status="paused"), "PATCH",
     P + "/forge/schedules/trg_1", {}, {"status": "paused"}),
    ("schedules.delete", lambda c: c.schedules.delete("trg_1"), "DELETE", P + "/forge/schedules/trg_1", {}, None),
    ("schedules.runs", lambda c: c.schedules.runs("trg_1", limit=10, cursor="abc"), "GET",
     P + "/forge/schedules/trg_1/runs", {"limit": "10", "cursor": "abc"}, None),
    ("schedules.run_now", lambda c: c.schedules.run_now("trg_1"), "POST", P + "/forge/schedules/trg_1/run-now", {},
     None),
    ("end_users.list", lambda c: c.end_users.list(status="all", q="a@", limit=5, cursor="abc"), "GET",
     P + "/end-users", {"status": "all", "q": "a@", "limit": "5", "cursor": "abc"}, None),
    ("end_users.get", lambda c: c.end_users.get("eu_1"), "GET", P + "/end-users/eu_1", {}, None),
    ("end_users.role_grants.list", lambda c: c.end_users.role_grants.list(end_user_id="eu_1"), "GET",
     P + "/end-user-role-grants", {"endUserId": "eu_1"}, None),
    ("end_users.role_grants.grant", lambda c: c.end_users.role_grants.grant("eu_1", "billing"), "POST",
     P + "/end-user-role-grants", {}, {"endUserId": "eu_1", "role": "billing"}),
    ("end_users.role_grants.revoke", lambda c: c.end_users.role_grants.revoke("eurg_1"), "DELETE",
     P + "/end-user-role-grants/eurg_1", {}, None),
    ("vectors.nearest", lambda c: c.vectors.nearest(
        "_notes", [1, 0, 0], top_k=2, min_score=0.5, metric="dot", filters={"metadata.lang": "en"}), "POST",
     P + "/vectors/_notes/search", {},
     {"queryVector": [1, 0, 0], "topK": 2, "minScore": 0.5, "metric": "dot", "filters": {"metadata.lang": "en"}}),
    ("vectors.upsert", lambda c: c.vectors.upsert("_notes", [
        {"id": "n1", "vector": [1, 0, 0], "_source": {"handle": "intro", "chunkIndex": 0},
         "subjectIds": ["eu_abc"]}]), "POST", P + "/vectors/_notes/upsert", {},
     {"rows": [{"id": "n1", "vector": [1, 0, 0], "_source": {"handle": "intro", "chunkIndex": 0},
                "subjectIds": ["eu_abc"]}]}),
    ("custody.status", lambda c: c.custody.status(), "GET", P + "/custody/status", {}, None),
    ("rampart.classify", lambda c: c.rampart.classify({"text": "hello"}, label="chat"), "POST",
     P + "/rampart/classify", {}, {"input": {"text": "hello"}, "label": "chat"}),
    ("rampart.jwks", lambda c: c.rampart.jwks(), "GET", "/rampart/jwks", {}, None),
    ("metadata.filter", lambda c: c.metadata.filter(
        ["collection:posts"], where=[{"field": "priority", "op": "gte", "value": 2}], limit=50, cursor="cur_0"), "POST",
     P + "/metadata/query", {},
     {"surfaces": ["collection:posts"], "where": [{"field": "metadata.priority", "op": "gte", "value": 2}],
      "limit": 50, "cursor": "cur_0"}),
    ("metadata.set", lambda c: c.metadata.set("posts", "p1", {"title": "T", "metadata.priority": 3}), "PATCH",
     P + "/metadata/collection/posts/p1", {}, {"metadata": {"title": "T", "priority": 3}}),
    ("metadata.search", lambda c: c.metadata.search(surface="collection:posts", field="body", q="finance", limit=5),
     "POST", P + "/metadata/search", {},
     {"surface": "collection:posts", "field": "metadata.body", "q": "finance", "limit": 5}),
    ("metadata.vectors.nearest", lambda c: c.metadata.vectors.nearest(
        surface="collection:posts", column="embedding", embedding=[1, 0, 0], k=3, metric="dot"), "POST",
     P + "/metadata/vectors/nearest", {},
     {"surface": "collection:posts", "column": "metadata.embedding", "embedding": [1, 0, 0], "k": 3, "metric": "dot"}),
    ("metadata.index.get", lambda c: c.metadata.index.get("posts"), "GET", P + "/metadata/index/posts", {}, None),
    ("metadata.index.declare", lambda c: c.metadata.index.declare("posts", [{"column": "body"}, {"column": "tags", "indexed": True}]),
     "PUT", P + "/metadata/index/posts", {}, {"columns": [{"column": "body"}, {"column": "tags", "indexed": True}]}),
    ("from_.select_vector_search", lambda c: c.from_("docs").select().vector_search("embedding", [1, 0]).execute(),
     "POST", P + "/data/docs/search", {}, {"vectorSearch": {"column": "embedding", "vector": [1, 0]}}),
    ("from_.insert_require_approval", lambda c: c.from_("posts").insert({"title": "t"}, require_approval=True),
     "POST", P + "/data/posts", {"requireApproval": "true"}, {"title": "t"}),
    # datasets, rollups, keys, audit replay (routes below are PENDING_ROUTES until the gateway OpenAPI in this tree declares them)
    ("audit.recent", lambda c: c.audit.recent(since_minutes=5), "GET", P + "/audit-log", {"after": "*", "limit": "100"}, None),
    ("audit.replay", lambda c: c.audit.replay(after_audit_event_id="evt_1"), "POST", P + "/realtime/replay", {},
     {"topic": "audit.project.*", "afterAuditEventId": "evt_1", "limit": 1000}),
    ("realtime.replay", lambda c: c.realtime.replay("audit.project.*", from_sequence=3, until_sequence=9, limit=50), "POST", P + "/realtime/replay", {},
     {"topic": "audit.project.*", "fromSequence": 3, "untilSequence": 9, "limit": 50}),
    ("crypto.encrypt", lambda c: c.crypto.encrypt("pii", [{"plaintext": b"x", "context": "u1"}]), "POST", P + "/crypto/encrypt", {},
     {"crypt": "pii", "custodyEpoch": "ep-1", "items": [{"plaintext": "eA==", "context": "u1"}]}),
    ("crypto.mode", lambda c: c.crypto.mode(), "GET", P + "/crypto/mode", {}, None),
    ("crypto.crypts.list", lambda c: c.crypto.crypts.list(), "GET", P + "/crypto/crypts", {}, None),
    ("crypto.crypts.create", lambda c: c.crypto.crypts.create("pii"), "POST", P + "/crypto/crypts", {}, {"name": "pii"}),
    ("crypto.crypts.retire", lambda c: c.crypto.crypts.retire("pii"), "POST", P + "/crypto/crypts/pii/retire", {}, None),
    ("crypto.federated_token", lambda c: c.crypto.federated_token("pii", "encrypt"), "POST", P + "/crypto/federated-token", {},
     {"crypt": "pii", "op": "encrypt"}),
    ("crypto.decrypt", lambda c: c.crypto.decrypt("pii", ["Y3Q="]), "POST", P + "/crypto/decrypt", {},
     {"crypt": "pii", "items": [{"ciphertext": "Y3Q="}]}),
    ("crypto.rewrap", lambda c: c.crypto.rewrap("pii", ["Y3Q="]), "POST", P + "/crypto/rewrap", {},
     {"crypt": "pii", "items": [{"ciphertext": "Y3Q="}]}),
    ("analytics.list", lambda c: c.analytics.list(), "GET", P + "/analytics", {}, None),
    ("analytics.query", lambda c: c.analytics.query("SELECT 1 FROM dataset.sales", ["sales"]), "POST",
     P + "/analytics/query", {}, {"sql": "SELECT 1 FROM dataset.sales", "datasets": ["sales"]}),
    ("analytics.create", lambda c: c.analytics.create("sales", display_name="Sales", columns=[{"name": "city", "type": "string"}],
                                                      residency_class="shared", partition_by=["city"],
                                                      storage_target={"kind": "managed"}),
     "POST", P + "/analytics/sales", {},
     {"displayName": "Sales", "columns": [{"name": "city", "type": "string"}], "residencyClass": "shared",
      "partitionBy": ["city"], "storageTarget": {"kind": "managed"}}),
    ("analytics.get", lambda c: c.analytics.get("sales"), "GET", P + "/analytics/sales", {}, None),
    ("analytics.delete", lambda c: c.analytics.delete("sales"), "DELETE", P + "/analytics/sales", {}, None),
    ("analytics.materialize", lambda c: c.analytics.materialize("sales", from_collection="orders"), "POST",
     P + "/analytics/sales/materialize", {}, {"fromCollection": "orders"}),
    ("analytics.read", lambda c: c.analytics.read("sales", partition_filter={"city": "Berlin"}, cursor="c1", limit=10),
     "POST", P + "/analytics/sales/read", {}, {"partitionFilter": {"city": "Berlin"}, "cursor": "c1", "limit": 10}),
    ("analytics.write", lambda c: c.analytics.write("sales", [{"city": "Berlin"}], on_error="reject"), "POST",
     P + "/analytics/sales/write", {"onError": "reject"}, [{"city": "Berlin"}]),
    ("analytics.upload", lambda c: c.analytics.upload("sales", b"city\nBerlin\n", format="csv", dry_run=True,
                                                      on_error="skip", delimiter=",", encoding="utf-8", has_header=True),
     "POST", P + "/analytics/sales/upload",
     {"format": "csv", "dryRun": "true", "onError": "skip", "delimiter": ",", "encoding": "utf-8", "hasHeader": "true"}, RAW),
    ("analytics.download_original", lambda c: c.analytics.download_original("sales"), "GET",
     P + "/analytics/sales/upload/original", {}, None),
    ("analytics.rollups.create", lambda c: c.analytics.rollups.create(
        "sales", slug="hourly", source="orders", measures=[{"name": "n", "agg": "count"}], window_seconds=3600,
        dimensions=["city"], non_personal=True, reattest_interval_days=30),
     "POST", P + "/analytics/sales/rollups", {},
     {"slug": "hourly", "source": "orders", "measures": [{"name": "n", "agg": "count"}], "windowSeconds": 3600,
      "dimensions": ["city"], "nonPersonal": True, "reattestIntervalDays": 30}),
    ("analytics.rollups.list", lambda c: c.analytics.rollups.list("sales"), "GET", P + "/analytics/sales/rollups", {}, None),
    ("analytics.rollups.delete", lambda c: c.analytics.rollups.delete("sales", "rol_1"), "DELETE",
     P + "/analytics/sales/rollups/rol_1", {}, None),
    ("analytics.rollups.reattest", lambda c: c.analytics.rollups.reattest("sales", "rol_1", reattest_interval_days=7),
     "POST", P + "/analytics/sales/rollups/rol_1/reattest", {}, {"reattestIntervalDays": 7}),
    ("analytics.schedules.create", lambda c: c.analytics.schedules.create("sales", from_collection="orders",
                                                                          interval_seconds=3600, reattest_interval_days=90),
     "POST", P + "/analytics/sales/schedules", {},
     {"fromCollection": "orders", "intervalSeconds": 3600, "reattestIntervalDays": 90}),
    ("analytics.schedules.list", lambda c: c.analytics.schedules.list("sales"), "GET", P + "/analytics/sales/schedules", {}, None),
    ("analytics.schedules.delete", lambda c: c.analytics.schedules.delete("sales", "sch_1"), "DELETE",
     P + "/analytics/sales/schedules/sch_1", {}, None),
    ("analytics.schedules.reattest", lambda c: c.analytics.schedules.reattest("sales", "sch_1"), "POST",
     P + "/analytics/sales/schedules/sch_1/reattest", {}, {}),
    ("rollups.list", lambda c: c.rollups.list(), "GET", P + "/rollups", {}, None),
    ("rollups.read", lambda c: c.rollups.read("hourly", from_="2026-10-01T00:00:00Z", to="2026-10-02T00:00:00Z", dims=["city"]),
     "GET", P + "/rollups/hourly/read", {"from": "2026-10-01T00:00:00Z", "to": "2026-10-02T00:00:00Z", "dims": "city"}, None),
    ("keys.list", lambda c: c.keys.list(status="all"), "GET", P + "/vault/keys", {"status": "all"}, None),
    ("keys.get", lambda c: c.keys.get("vkey_1"), "GET", P + "/vault/keys/vkey_1", {}, None),
    ("keys.rotate", lambda c: c.keys.rotate("vkey_1"), "POST", P + "/vault/keys/vkey_1/rotate", {}, None),
    # data plane
    ("from_.select", lambda c: c.from_("posts").select().eq("published", True).limit(5).execute(), "GET",
     P + "/data/posts", {"published": "eq.true", "limit": "5"}, None),
    ("from_.insert", lambda c: c.from_("posts").insert({"title": "t"}), "POST", P + "/data/posts", {}, {"title": "t"}),
    ("from_.update", lambda c: c.from_("posts").update({"title": "u"}).eq("id", "p_1").execute(), "PATCH",
     P + "/data/posts/p_1", {}, {"title": "u"}),
    ("from_.delete", lambda c: c.from_("posts").delete().eq("id", "p_1").execute(), "DELETE", P + "/data/posts/p_1",
     {}, None),
]


# Calls whose reveal() decodes a body; the generic recording response is not a valid reveal payload.
def _spec():
    if not SPEC_PATH.exists():
        pytest.skip(f"gateway OpenAPI not found at {SPEC_PATH}")
    return json.loads(SPEC_PATH.read_text())


def _deref(spec, node):
    for _ in range(20):
        if isinstance(node, dict) and "$ref" in node:
            target = spec
            for part in node["$ref"].lstrip("#/").split("/"):
                target = target[part]
            node = target
        else:
            break
    return node if isinstance(node, dict) else {}


def _summarise(spec, schema):
    """{required, properties} of an object schema; properties None when the schema is open (a union or free-form)."""
    s = _deref(spec, schema)
    required = set(s.get("required") or [])
    props = set(s["properties"]) if s.get("properties") else None
    open_ = s.get("additionalProperties") is True
    for part in s.get("allOf") or []:
        sub = _summarise(spec, part)
        required |= set(sub["required"])
        if sub["properties"] is None:
            open_ = True
        else:
            props = (props or set()) | set(sub["properties"])
    if s.get("oneOf") or s.get("anyOf"):
        open_ = True
    return {"required": required, "properties": None if open_ or props is None else props}


def _route(spec, method: str, path: str):
    """(template, operation) for a sent path; `{param}` matches exactly one segment."""
    hits = []
    for template, item in spec["paths"].items():
        if method.lower() not in item:
            continue
        pattern = "^" + re.sub(r"\\\{[^}]+\\\}", "[^/]+", re.escape(template)) + "$"
        if re.match(pattern, path):
            hits.append((template, item[method.lower()]))
    if not hits:
        return None
    # Prefer the most literal template (fewest params).
    hits.sort(key=lambda h: h[0].count("{"))
    return hits[0]


def _pending_key(method: str, path: str):
    for key in PENDING_ROUTES:
        m, template = key.split(" ", 1)
        pattern = "^" + re.sub(r"\\\{[^}]+\\\}", "[^/]+", re.escape(template)) + "$"
        if m == method and re.match(pattern, path):
            return key
    return None


def test_pending_routes_are_not_yet_in_openapi():
    spec = _spec()
    for key, why in PENDING_ROUTES.items():
        method, template = key.split(" ", 1)
        declared = method.lower() in spec["paths"].get(template, {})
        assert not declared, f"{key} is now in the gateway OpenAPI ({why}): remove it from PENDING_ROUTES"


@pytest.mark.parametrize("entry", TABLE, ids=[e[0] for e in TABLE])
def test_exact_wire_and_openapi(entry):
    ident, call, method, path, query, body = entry
    req = _sent(call)
    assert req.method == method, ident
    assert req.url.raw_path.decode().split("?")[0] == path, ident

    sent_query = dict(parse_qsl(urlsplit(str(req.url)).query))
    if ident in ("audit.tail", "audit.recent"):
        assert "after" in sent_query and sent_query.pop("after")
        query = {k: v for k, v in query.items() if k != "after"}
    assert sent_query == query, ident

    if body is RAW:
        sent_body = RAW
    elif req.content:
        sent_body = json.loads(req.content)
        assert body == sent_body, ident
    else:
        sent_body = None
        assert body is None, ident

    spec = _spec()
    if _pending_key(method, path) is not None:
        return
    hit = _route(spec, method, path.replace("%2F", "%2F"))
    assert hit is not None, f"{ident}: {method} {path} is not a route in the gateway OpenAPI"
    template, op = hit

    declared = {_deref(spec, p).get("name") for p in op.get("parameters", []) if _deref(spec, p).get("in") == "query"}
    if (method, template) not in FREE_QUERY_ROUTES:
        allowed = declared | EXTRA_QUERY.get((method, template), set())
        assert set(sent_query) <= allowed, f"{ident}: query {set(sent_query) - allowed} not read by {method} {template}"

    if isinstance(sent_body, dict):
        content = _deref(spec, op.get("requestBody", {})).get("content", {})
        media = content.get("application/json") or (next(iter(content.values())) if content else None)
        if media is not None and media.get("schema"):
            summary = _summarise(spec, media["schema"])
            missing = summary["required"] - set(sent_body)
            assert not missing, f"{ident}: required body fields missing: {missing}"
            if summary["properties"] is not None:
                extra = set(sent_body) - summary["properties"] - EXTRA_BODY.get((method, template), set())
                assert not extra, f"{ident}: body fields the gateway does not declare: {extra}"


def test_locate_door_wire():
    """sign_in asks POST /@portal/gate/locate with only the email, no Authorization, before the credential call."""
    c, seen = _client()
    c.gate.sign_in(email="a@b.co", password="pw")
    reqs = [r for r in seen if r.method != "HEAD"]
    assert [(r.method, r.url.path) for r in reqs] == [("POST", P + "/gate/locate"), ("POST", P + "/gate/signin")]
    assert json.loads(reqs[0].content) == {"email": "a@b.co"}
    assert "authorization" not in reqs[0].headers
    assert reqs[1].headers["authorization"] == "Bearer k"


def test_subscribe_route_is_in_openapi():
    import asyncio

    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, content=b'data: {"eventType": "x", "eventId": "evt_x"}\n\n', headers={"content-type": "text/event-stream"})

    c = create_client(base_url=BASE, project_slug="portal")
    c.config.transport = httpx.MockTransport(handler)

    async def go():
        async for e in c.realtime.subscribe("collections.orders.*", filter={"a": 1}):
            return [e]  # the feed reconnects forever; one event is enough

    asyncio.run(go())
    req = [r for r in seen if r.method != "HEAD"][0]
    assert req.method == "GET" and req.url.path == "/@portal/realtime/subscribe"
    assert dict(parse_qsl(req.url.query.decode())) == {"topic": "collections.orders.*", "filter": '{"a": 1}'}
    assert _route(_spec(), "GET", req.url.path) is not None


def test_invoke_stream_route_and_flat_body():
    import asyncio

    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, content=b'data: {"choices": []}\n\ndata: [DONE]\n\n',
                              headers={"content-type": "text/event-stream"})

    c = create_client(base_url=BASE, project_slug="portal")
    c.config.transport = httpx.MockTransport(handler)

    async def go():
        return [x async for x in c.compute.llms.invoke_stream("llama-3-8b", prompt="hi", params={"maxTokens": 3})]

    assert asyncio.run(go()) == [{"choices": []}]
    req = [r for r in seen if r.method != "HEAD"][0]
    assert req.method == "POST" and req.url.path == "/@portal/compute/llms/llama-3-8b/invoke-stream"
    assert json.loads(req.content) == {"prompt": "hi", "maxTokens": 3}
    assert _route(_spec(), "POST", req.url.path) is not None


# Dropped from the SDK: no gateway route serves them.
REMOVED = [
    "me.notifications", "me.audit_log", "me.data_export",
    "gate.hosted", "gate.hosted_async", "gate.verify_session",
    "org.groups.create", "org.groups.delete", "org.members.get", "org.smtp.get",
    "policies.evaluate", "identity", "audit.emit",
]


@pytest.mark.parametrize("dotted", REMOVED)
def test_removed_methods_are_gone(dotted):
    c, _ = _client()
    obj = c
    for part in dotted.split("."):
        if not hasattr(obj, part):
            return
        obj = getattr(obj, part)
    pytest.fail(f"{dotted} is still on the client but the gateway has no route for it")


# Public callables that are not one HTTP call each.
NOT_A_CALL = {
    "from_.select", "from_.insert", "from_.update", "from_.delete",  # exercised through the builder entries
    "storage.from_", "from_", "with_options",  # builders / handles / a client with another timeout
    "crypto.use_client_core",  # registers the client-side core, no wire call
    "rampart.verify_receipt",  # local: checks a receipt against the key feed, tested in test_rampart.py
    "realtime.subscribe", "streams.subscribe", "rollups.subscribe", "metadata.subscribe", "compute.llms.invoke_stream",  # SSE generators, tested above
}


def _public_methods(obj, prefix, depth=0):
    out = []
    for name in dir(obj):
        if name.startswith("_") or depth > 5:
            continue
        member = getattr(obj, name)
        dotted = f"{prefix}.{name}" if prefix else name
        if inspect.ismethod(member):
            out.append(dotted)
        elif not callable(member) and not isinstance(member, (str, int, float, dict, list, tuple, type(None), bool)):
            if name in ("config",):
                continue
            out.extend(_public_methods(member, dotted, depth + 1))
    return out


def test_every_public_method_is_in_the_table():
    c, _ = _client()
    tabled = {e[0] for e in TABLE}
    tabled |= {re.sub(r"\.filters$", "", i) for i in tabled}
    missing = []
    for dotted in _public_methods(c, ""):
        if dotted.endswith("_async") or dotted in NOT_A_CALL:
            continue
        if dotted.startswith("storage.") and dotted.count(".") == 1 and dotted not in tabled:
            missing.append(dotted)
            continue
        if dotted not in tabled:
            missing.append(dotted)
    assert not missing, f"public methods with no wire entry in TABLE: {missing}"
