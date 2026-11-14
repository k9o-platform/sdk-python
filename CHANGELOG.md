# Changelog — k9o (Python SDK)

## 1.5.2

### Changed

- `K9O_TIER_FEATURE_GATED` is no longer documented as an error to expect: the platform no longer gates features by plan.
- The `tierGating` option is removed from sign-in provider creation; the platform no longer gates features by plan.
- README: Architecture at a glance, illustrations, and the package table lists only the packages for that SDK.

## 1.5.0

### Added

- `approvals.comments.add(pending_id, body, visibility)` and `approvals.comments.list(pending_id, limit=, cursor=)` (`POST` and `GET /@<project>/pending-approvals/<id>/comments`): comments on a pending change. `body` is plain text of 1 to 4000 characters; `visibility` is `everyone` or `approvers_only` and is fixed when posted. There is no edit or delete, and a comment never decides the change. Posting needs a session: a personal access token alone is refused (403 `K9O_RBAC_DENY`). `list` answers the comments the caller may see, oldest first, 1 to 200 at a time; `meta.cursor.next` is the cursor of the next page and `None` on the last. Errors: `K9O_APPROVAL_COMMENT_NOT_ELIGIBLE` (403), `K9O_APPROVAL_COMMENT_CLOSED` (409), `K9O_INVALID_FIELD_VALUE` (400), `K9O_NOT_FOUND` (404). `ApprovalComment` types the data.
- A page that carries `nextCursor` beside `data` and no `meta.cursor` now reads its cursor from `meta.cursor.next`.
- `realtime.replay(topic, from_sequence=, until_sequence=, limit=)` (`POST /@<project>/realtime/replay`): catch up on past events for a topic. `topic` is required; it answers a `RealtimeReplayPage` (`topic`, `events`, `next_cursor`, `has_more`, `resync`). Topics the gateway keeps no log for answer no events and `resync` set to `"reread-source"`. `audit.replay` is unchanged.
- `schema.collections.get(name)` (`GET /@<project>/schema/collections/<name>`): one collection with its columns, indexes and relationships.
- `KError.request_id` is filled from the gateway's `x-k9o-request-id` response header on every non-2xx answer (requests, raw downloads, stream opens, realtime subscribe and the Rampart key feed) when the error body carries none; a `requestId` in the body wins, and without either it stays `None`.
- `gate.sign_in` asks the project which region holds the email before it sends the password (`POST /@<project>/gate/locate` with only the email, no `Authorization` header). The password goes to the located origin only if it is `https` and a region host of the same org, gateway family and apex as the base URL, with no userinfo, port, path, query or fragment; anything else, or no origin in the answer, uses the base URL. Nothing is cached except a bounded hint (64) per refresh token so `refresh_token` and `sign_out` reach the region that issued the session. A non-2xx answer from the region step (429 `K9O_RATE_LIMITED`, 503 `K9O_GATE_REGION_UNAVAILABLE`) is raised as `KError` before the password is sent, with no retry. If the project's locate step answers 404, the client uses the base address.
- The Kibo client core matches the configured key against the call's `expected_key_refs` before it fetches a federated token or exchanges a credential; a key outside the call's pin gets `K9O_CRYPTO_UNEXPECTED_KEY` on every item, with no token fetch and no seal or open.
- `crypto.mode()` and `crypto.mode(crypt)` return the gateway's `route` (`"client"` or `"server"`) as `CryptoMode.route`, `None` on older gateways or for an unknown value; follow it, not `custody_mode`.
- The custody check before each `encrypt` names the crypt (`GET /crypto/mode?crypt=<crypt>`), so a personal access token or workload policy scoped to named crypts can encrypt; `crypto.mode()` still sends no query.
- `crypto.mode(crypt=None)` takes an optional crypt and sends `?crypt=<crypt>`; a bad crypt is refused with `K9O_INVALID_FIELD_VALUE` before sending. With none it is unchanged.
- `crypto.encrypt` follows the `route` field of the project's custody mode answer (`GET /crypto/mode`, read fresh before every encrypt). On a BYOK or HYOK project only an explicit `route: "server"` sends plaintext to the platform (the answer must also carry a non-empty `epoch`); a missing `route` or `"client"` uses the registered client core. A `route` that is not `"server"` or `"client"` raises `K9O_CRYPTO_CUSTODY_UNKNOWN` (context `custodyMode`, and `route` when it is a string) and nothing is sent, in every custody mode. `decrypt` and `rewrap` are unchanged.
- `crypto.crypts.list()`, `.create(name)` and `.retire(name)` (`GET`/`POST /crypto/crypts`, `POST /crypto/crypts/<name>/retire`): the project's registered crypt names, returned as `Crypt` (`name`, `status` `"active"` or `"retired"`, `created_at`, `retired_at`). A bad name (`[a-z0-9-]{1,64}`, not `actions`) is refused before sending (`K9O_INVALID_FIELD_VALUE`, field `name`); `create` sends only `{name}` and `retire` sends no body. A malformed gateway answer raises `K9O_INTERNAL_ERROR` (`crypto.crypts.<op>: the gateway answered ...`). `create` and `retire` need `crypto:manage_crypts` (project admins by default) and a personal access token is refused on `retire`. A retired or unregistered name only makes `encrypt` refuse it; `decrypt` and `rewrap` never read the registry. 409 `K9O_CRYPT_ALREADY_EXISTS`, 409 `K9O_CRYPT_LIMIT_EXCEEDED`, 409 `K9O_CRYPT_RETIRED` and 404 `K9O_CRYPT_NOT_FOUND` arrive as `KError`, never retried.
- `FederatedToken` has optional `provider` and `project_id`, set only when the gateway sends them as non-empty strings.
- `k9o.kibo.create_kibo_client_core(...)`: the client core for `crypto` on a BYOK or HYOK project, registered with `crypto.use_client_core(core)`. It fetches a federated token (`crypto.federated_token`), exchanges it on your own cloud (`acquire_kms_credential`) and seals or opens on this machine (`app_crypto_seal`, `app_crypto_open`) against your own KMS. The key, provider, region, role and endpoint come from your configuration only. `expected_key_refs` is required and must contain `key_ref`; a call with an empty pin, a core that does not list `expected_key_refs` in `core_capabilities()` (`K9O_CRYPTO_KEY_PIN_UNENFORCED`) or one older than `zk-bundle-v2.15.0` (`KIBO_WASM_ERROR`) is refused before any token is fetched. A token naming another provider or project than configured is refused (`K9O_INVALID_FIELD_VALUE`). The exchange has a deadline (`exchange_deadline_secs`, default 30; `KIBO_EXCHANGE_TIMEOUT`); the credential is cached per crypt, operation and subject until 60 s before expiry, and a token is fetched on every call. Item failures are per item with the core's code (`UPSTREAM_KMS_ERROR` is your own KMS); call-level problems raise. Rewrap is open then seal under the configured key with a `rewrap` token.
- `crypto.federated_token(crypt, op)` (`POST /crypto/federated-token`): a short-lived identity token (RS256 JWT, at most 15 minutes) to present to your own cloud's token exchange for BYOK and HYOK keys. Returns a `FederatedToken` with `token`, `expires_at`, `issuer`, `subject`, `audience` and `kid`; the token is left out of `repr`. A crypt that does not match `[a-z0-9-]{1,64}` or is `actions` or an `op` other than `encrypt`, `decrypt` or `rewrap` is refused before sending (`K9O_INVALID_FIELD_VALUE`). 409 `K9O_CUSTODY_SERVER_ROUTE`, 429 `K9O_RATE_LIMITED` and 503 `K9O_KEY_SERVICE_UNAVAILABLE` are raised as they come and never retried.
- `crypto` namespace (server-side encryption): `crypto.encrypt(crypt, items)`, `decrypt` and `rewrap` call `POST /crypto/encrypt|decrypt|rewrap` with the project key (you never name a key). The SDK base64-encodes plaintext, takes bytes or UTF-8 strings, never parses the opaque ciphertext, and checks the crypt (`[a-z0-9-]{1,64}`, not `actions`), 1 to 100 items, 64 KiB per item, 1 MiB per call and a 256-byte context before sending (`K9O_INVALID_FIELD_VALUE`, `K9O_PAYLOAD_TOO_LARGE`). Each item has its own result or code; the item shape follows the gateway. Machine calls for service tokens and personal access tokens; extensions, end users and mobile cannot call it; access rules on the crypt decide, default floor `PROJECT_EDITOR`. The calls route by custody: `encrypt` checks `crypto.mode()` (`GET /crypto/mode`, `{custodyMode, epoch}`) on every call with no cache, sends the `custodyEpoch` with the request, reroutes once if custody moved (409 `K9O_ZK_STORE_FLOW_REQUIRED`), and never sends plaintext to the platform when the key is in your own KMS or HSM (BYOK or HYOK); `decrypt` and `rewrap` try the platform first and reroute client-side when the ciphertext belongs to your own key. Register the Kibo client core with `crypto.use_client_core(core)` (the `ClientCore` protocol) and the same calls work in every custody mode; without one a BYOK or HYOK project gets 501 `K9O_ZK_CLIENT_CORE_UNAVAILABLE`, and an unknown mode or failed mode check fails closed with nothing sent. The ciphertext is covered by the project key's lifecycle only (zeroize or project purge), not by per-subject erasure; client-side Kibo, the extension route and this route do not open each other's ciphertext; decrypt needs the same crypt and context. 400 `K9O_ENVELOPE_INTEGRITY_FAILURE` is one code for every cannot-open, 409 `K9O_SEALED_UNDER_RETIRED_KEY` (`rewrap` is the fix), 404 `K9O_CRYPT_NOT_FOUND` and 409 `K9O_CRYPT_RETIRED` (encrypt only; decrypt and rewrap are refused for neither), 413, 429 and 503 `K9O_KEY_SERVICE_UNAVAILABLE` are never retried. The route is not live on the gateway yet.
- `expected_key_refs` option on `create_client` (a non-empty list of key identifiers: ARNs, resource names, key URLs) for projects that use BYOK or HYOK. The crypto router passes it to the registered client core as the `expected_key_refs` keyword on every `encrypt`, `decrypt` and `rewrap`, and only when it is set, so a core written without the parameter keeps working while the option is unset. The core refuses a key that is not in the list before it uses any token or makes any network call; the key version is ignored. The list is checked when the client is built (`K9O_INVALID_FIELD_VALUE`: empty, blank, whitespace or `*`). A core declares that it enforces the pin with `enforces_expected_key_refs = True`; with the option set and no declaration the call is refused with `K9O_CRYPTO_KEY_PIN_UNENFORCED` before the core is called. `key_ref_matches(key_ref, expected, provider)` is the version-ignoring comparison for core authors; `provider` ("aws", "gcp", "azure" or "vault") is required and never guessed from the reference, and the comparison follows that provider's rule (AWS whole reference; GCP without the version; Azure the first five parts in lower case; Vault `<path>/<key>` without `:<version>`). The `ClientCore` protocol documents the new parameter and the declaration.
- `realtime.subscribe` resumes by itself: it reconnects with `Last-Event-ID` after the server closes a stream (every 300 s) or the connection drops, drops events it already delivered, and treats 45 s without any bytes as a dead connection. Events carry `eventId`. A 4xx answer raises `KError` once; 5xx and network errors are retried (delay up to 30 s).
- `on_resync` option on `realtime.subscribe`: called (sync or async) with `{"reason", "action"}` when the feed position is lost; re-read current state there. The resync frame is not yielded, and the next connection starts without `Last-Event-ID`.
- `metadata` namespace: `filter`, `set`, `search`, `vectors.nearest`, `subscribe`, and `index.get` / `index.declare` for the columns you declare on a collection.
  Column names in `where`, `search` and `nearest` get the `metadata.` prefix when you leave it out; `set` writes declared columns only. `filter`, `search` and `nearest` return a `MetadataPage` (`data` rows, `cursor`).
  `subscribe` uses the same reconnecting reader as `realtime.subscribe` (events carry the record id only) and takes `from_sequence`. The SDK checks the surface grammar, limits, cursor rule and the 64-column limit before sending.
  Gateway codes such as `K9O_METADATA_FIELD_NOT_INDEXED`, `K9O_UNKNOWN_SURFACE` and `K9O_QUERY_RESULT_TOO_LARGE` arrive as `KError`.
- `rampart` namespace: `classify(input, label=)` returns the verdict, the signed receipt and `project_id`; `jwks()` reads the public key feed (`keys` and the deployment's `issuer`); `verify_receipt(result_or_receipt, input)` checks a receipt locally (key by `kid`, `EdDSA`, `typ`, signature, issuer from the key feed or the `issuer=` option, project from the classify result or `project_id=`, time window, and the SHA-256 of the canonical `input` in the platform's JCS code-point profile: keys sorted by UTF-8 byte order, integers only up to 2^53 - 1) and raises `ReceiptRefusal` with a closed `reason` on any failure (`project_unknown` when a bare receipt has no project id to compare). Rampart is advisory: it never blocks, and an unavailable classifier raises `K9O_RAMPART_CLASSIFIER_UNAVAILABLE`. A long input is scored in overlapping windows and the highest injection score decides; past 16 windows (about 6,000 tokens) the answer is 413 `K9O_RAMPART_INPUT_TOO_LONG` with no verdict, and 503 `K9O_RAMPART_CLASSIFIER_UNAVAILABLE` still means the classifier could not answer. The thresholds (calibration `rampart-launch-v1`) are a starting calibration and may move; a change is a new calibrationVersion. Ed25519 and the canonicalizer are implemented in the package, so there is no new dependency.
- `rampart`: `verify_receipt` reads the receipt's `windows` claim (the number of windows scored, 1 when the input fit one) into `VerifiedReceipt.windows` (`None` when the receipt has none) and ignores claims it does not know. `classify` raises `RampartInputTooLong` (a `KError`, 413 `K9O_RAMPART_INPUT_TOO_LONG`) past 16 windows; there is never a verdict for it.
- `analytics` namespace for datasets: `list`, `get`, `create`, `delete`, `query` (read-only SQL), `read` (paged), `write`, `materialize`, `upload` (CSV or JSON file, with `dry_run` and a `manifest` that creates the dataset), `download_original`, plus `analytics.rollups` and `analytics.schedules` (`create`, `list`, `delete`, `reattest`). `rollups.list`, `rollups.read` and `rollups.subscribe` read and follow rollups; `rollups.subscribe` uses the same reconnecting reader as `realtime.subscribe`.
  A rollup or schedule pauses when it is not re-attested within its `reattest_interval_days` (7, 30, 90 or 365). `reattest` is for people: a service, workload or extension token gets 403 `K9O_RBAC_DENY` (`reason` `human_only`).
- `keys` namespace: `list`, `get` and `rotate` for the project's encryption keys (metadata only: `id`, `version`, `status`, `custodyMode`, `createdAt`, `rotatedAt`, `byokRef`, `hyokRef`). Reading needs the project admin or auditor role; rotating needs the project admin role and a fresh step-up proof, and is limited to 3 rotations in any rolling 24 hours (429 `K9O_RATE_LIMITED`, `retry_after_sec`).
- `audit.tail(types=)` follows the project's audit events live (`auditEventId`, `type`, `actorType`, `occurredAt`, `outcome`) with the reconnecting reader of `realtime.subscribe`; on a `replay-ledger` resync it catches up through `audit.replay` by itself. `audit.replay` reads past events after an `auditEventId` or from a sequence. The earlier one-shot read is `audit.recent(since_minutes=, limit=)`; `tail(since_minutes=...)` still answers it.
- `KError.retry_after_sec` also reads a numeric `Retry-After` header when the error body gives none.
- `k9o.moat`: a local guard with no network access. `Moat(prompt=, input_schema=, output_schema=, filters=)` pins the system prompt, refuses role and system markers in untrusted input, enforces input and output schemas (a JSON Schema subset; unknown keywords are rejected at construction) and runs an optional PII filter. Refusals are `MoatRefusal` with `reason` `role_marker`, `input_schema`, `output_schema` or `pii`.
- `k9o.moat`: the role-marker scan matches on text normalised by the Moat contract rule (NFKC, combining marks (Mn, Me) ignored, default-ignorable and format code points removed using a fixed table, a small set of Cyrillic and Greek look-alikes of the marker letters mapped to the Latin letter, case-folded, horizontal whitespace collapsed, line breaks kept) and passes the shared marker vectors.
- `custody.status()`: read-only custody status of the project (`mode`, `attached`, `provider`, `lastJob`, `hsm`). Needs the project admin or auditor role.
- `KError.retry_after_sec`: the number of seconds the gateway asks you to wait, or `None`. It is a field only; retry behaviour is unchanged.
- `timeout` option on `create_client`: a number of seconds (read), a `(connect, read)` pair or an `httpx.Timeout`.
- `timeout` argument on `ai.generate`, `ai.embed`, `compute.llms.invoke`, `compute.llms.invoke_stream`, `realtime.subscribe` and `streams.subscribe`.
- `client.with_options(timeout=...)`: a client with another timeout, for any method.
- `require_approval` on single-record writes: `from_(c).insert(record, require_approval=True)`, and `.require_approval()` on `update` and `delete`, send `?requireApproval=true`.
  The answer is the queued change (HTTP 202) or, when an `auto_approve` policy applies the write, the row: `is_queued_write(result)` tells them apart. `QueuedChange` types the queued data.
- `approvals.list` takes `cursor`: pass `meta.cursor.next` of a page to read the next one (`None` on the last page). An invalid cursor raises `K9O_INVALID_CURSOR`.
- `approvals.bulk` results carry `failed`, a list of `{id, code, reason}` for rows that could not be decided (an empty list when none). The call answers HTTP 200 even when rows fail, so compare `affectedIds` with what you intended. `BulkApprovalsResult` and `BulkFailure` type the data.
- `functions.triggers`: `list`, `create`, `update` and `delete` for event triggers on a collection.
- `schedules` namespace for cron schedules: `list`, `get`, `create`, `update`, `delete`, `runs` and `run_now`.
- `end_users` namespace: `list` and `get`, plus `end_users.role_grants.list`, `grant` and `revoke`.
- `vectors` namespace: `nearest(collection, query_vector, top_k=, min_score=, metric=, filters=)` and `upsert(collection, rows)` for collections of your own embeddings.
  The vector is sent in the request body (up to 16000 numbers, 1 to 1000 rows per upsert). A collection name must start with `_`.
  The SDK checks the name, the vector length and numbers, `top_k`, `metric` and the row count before sending.
- README: what an event-trigger function receives (the JSON payload, redaction, `triggeredAt`, and deduping on `eventId`).

### Fixed

- `realtime.subscribe`, `metadata.subscribe` and `rollups.subscribe` no longer reconnect in a loop without delivering anything: on a fresh connection (no `Last-Event-ID` and no `from_sequence`) the gateway sends one `resync` frame with the current head and then streams events on the same connection. That opening `resync` is now informational (`on_resync` is not called, the connection is kept); a `resync` after an event was received, or on a connection that sent `Last-Event-ID` or `from_sequence`, still calls `on_resync` and restarts without `Last-Event-ID`.
- `audit.tail` read the wrong frame: the gateway sends the five fields inside `payload`, with the publisher's own SSE id (not the `auditEventId`) and the audit type as the event name. The tail now yields `auditEventId`, `type`, `actorType`, `occurredAt`, `outcome` and `eventId`, dedupes on `eventId`, resumes with `Last-Event-ID` set to the SSE `id`, and on a `replay-ledger` resync replays after the last `auditEventId`, pages while `hasMore`, and skips events already delivered. After a `resync` it now connects fresh first, holds the frames that arrive, replays from the last delivered `auditEventId`, then merges replay, held and live events (deduped), so nothing is lost; a repeated `resync` backs off. A new connection starts at the live head and may re-send the last few seconds of events, each yielded once, and a `resync` that opens a fresh connection is informational (the stream is kept). `audit.replay` sends `limit: 1000` when you give none and refuses a limit outside 1 to 10000 with `K9O_INVALID_FIELD_VALUE`.
- `audit.replay` now returns an `AuditReplayPage` (`events` flattened from `payload`, with `sequence` as a string; `next_cursor`; `has_more`) and accepts `after_audit_event_id` together with `from_sequence` (the id wins).
- `rollups.subscribe` yields the frame's `payload` cell (`cellKey`, `bucket`, `dims`, `final`, `measures`) with `eventId` and `occurredAt`; a cohort-suppressed cell has `status`, `reason` and `suppressed` True. A resync with action `reread-source` reaches `on_resync`.
- `keys.rotate(key_id, step_up=hook)` answers a 401 `K9O_STEP_UP_REQUIRED` challenge (`scope_set_root`, `nonce`, `methods`, `max_age_sec`, `rp_id`) with your hook and retries exactly once with the `x-k9o-step-up-proof` header; without a hook it raises `StepUpRequired` carrying the challenge at `error.step_up`. `keys.list` is unpaged: `data` is `{items}` and a `nextCursor` is dropped.
- `rampart.classify` refuses an `input` that is not an object (`K9O_INVALID_FIELD_VALUE`) before sending.
- `vector_search` on the select builder sends the embedding in the request body (`POST data/<collection>/search`), so real embeddings no longer fail with HTTP 414. It cannot be combined with `text_search`.
- `recommendation` on `KError` is always text or `None`. The gateway may send it as an object (`{"action": ...}` or `{"retryAfterSec": ..., "note": ...}`): the `action` text, else the `note` text, is used. Decoding an error no longer depends on the type of this field.
- Requests no longer fail after the 5-second httpx default. The defaults are now 10 s to connect and 120 s per read,
  so `ai.generate`, `invoke_stream` and idle stream tails work against models that take about 30 s.
  Stream reads use the read value as an idle timeout, not a total one.

## 1.4.0 — 2026-10-26

### Added

- `query(sql, params)`: one read-only SQL statement; a list binds `$1, $2, ...`, a dict binds `:name` placeholders.
- `search(collection, column=..., q=...)`: full-text search over one column, plus `text_search` and `vector_search` on `select()`.
- `ai.generate`: text generation from a prompt (verb sugar over `compute.llms.invoke`; `model` defaults to the org's default LLM).
- `streams` namespace for user data streams: `create`, `list`, `get`, `delete`, `publish`, `replay` and `subscribe`.
  `subscribe` tails live events over SSE and reconnects from the last sequence when the server closes the connection.
- `approvals` namespace for the pending-change queue: `list`, `get`, `approve`, `reject` and `bulk`.
- `approval_policies` namespace: `list`, `create` and `delete`.
- `schema.collections.list`: the project's collections with row counts and last migration, paged by cursor.

### Changed

- The README description now covers streams and realtime, and the platform overview moved to an "About Kilimanjaro" appendix.
- Function deploy examples use the sandbox tier.

## 1.3.0 — 2026-10-26

Every method now matches the gateway.

### Changed

- `projects.create` takes `display_name` (and optional `slug`, `region`, `custody_mode`, ...);
  `projects.get` and `projects.update` use the projects routes.
- `functions.deploy` takes `name`, `runtime`, `tier` (`gateway` or `sandbox`), `artifact_key`, `version` and `description`.
  Inline code is gone.
- `functions.invoke` sends the payload as `{payload}`.
- `policies.create`, `update` and `validate` take a `policy` document (plus optional `rules`, `editor_mode`);
  `policies.list` takes only `status`.
- `audit.query` filters are `event_type`, `actor_id`, `after`, `before`, `limit` and `cursor`.
- Org roles, identity providers, default model and SMTP changed: `smtp.set` uses `password_ref` and `from_address`;
  `smtp.test` takes `to` and `subject`.
- `me.api_tokens.create` takes `label` and `scope_manifest` (and `ttl_seconds`); `me.sessions.revoke` and `revoke_all` use new routes.
- `compute.llms.invoke` settings are sent flat, and the result has `content` and `usage`.
- `studio.workflows.trigger` sends the context as `triggerContext`.
- `gate.sign_out` requires the refresh token.
- `kibo.vault.secrets.get` looks a secret up by exact name; `region` is optional on every vault method.
- The API host comes from the base URL: any `<org>.api.<apex>` host works and no apex is hardcoded.
  `https://api.k9o.io` is only the default.

### Removed

- `gate.verify_session`, `gate.hosted` and `realtime.replay`, and the poll transport of `realtime.subscribe`.
- `me.notifications`, `me.audit_log` and `me.data_export`.
- `org.groups.create`, `org.groups.delete`, `org.members.get` and `org.smtp.get`.

### Added

- `functions.upload_artifact(data, kind)` uploads a build and returns the `artifactKey` for `deploy`.
- `parse_api_host`, `ApiHost` and `DEFAULT_BASE_URL` are exported.
- A contract test checks every method against the gateway's API description.

## 1.2.0 — 2026-10-25

- The README now introduces the Kilimanjaro building blocks (Keep, Pulse, Gate, Kibo). No change to the public API.

## 1.1.1 — 2026-10-25

- Documentation links corrected: the documentation hub is https://docs.kilimanjaro.dev, with the website, platform API and console listed in a Links section. Package metadata points at the same places.

## 1.1.0 — 2026-10-25

- Documentation rewritten with runnable examples and expected output; internal cross-references removed from the published sources. No change to the public API.
- `k9o.__version__` now reports the installed package version (it previously reported `0.3.0`).
- Fixed: boolean filter values are sent as `true` / `false`. `eq("done", False)` previously sent `False`.
- Error text for `update()` / `delete()` without an `id` filter no longer mentions a version or a roadmap.

## 1.0.0 — 2026-10-23

First public release. See the README for the full API overview.
