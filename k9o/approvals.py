"""
Approvals namespaces: `client.approvals.*` (the pending-change queue) and `client.approval_policies.*` (the rules that
decide which writes are queued).

Wire mapping (project plane):
  - approvals.list(status=, limit=, cursor=)           GET    /@<slug>/pending-approvals
  - approvals.get(id)                                  GET    /@<slug>/pending-approvals/<id>
  - approvals.approve(id, reason=)                     POST   /@<slug>/pending-approvals/<id>/approve
  - approvals.reject(id, reason)                       POST   /@<slug>/pending-approvals/<id>/reject
  - approvals.bulk(action, criteria, reason=)          POST   /@<slug>/pending-approvals/bulk
  - approvals.comments.add(pending_id, body, visibility)
                                                       POST   /@<slug>/pending-approvals/<pending_id>/comments
  - approvals.comments.list(pending_id, limit=, cursor=)
                                                       GET    /@<slug>/pending-approvals/<pending_id>/comments
  - approval_policies.list()                           GET    /@<slug>/approval-policies
  - approval_policies.create(collection, operation, action, predicate)
                                                       POST   /@<slug>/approval-policies
  - approval_policies.delete(id)                       DELETE /@<slug>/approval-policies/<id>

`approvals.list` returns the newest changes first, `limit` at a time (1 to 200, default 50). Pass `meta.cursor.next` of
one page as `cursor` of the next; it is `None` on the last page. A cursor belongs to the `status` it was issued for:
reuse with another `status` raises KError `K9O_INVALID_CURSOR` (HTTP 400), as does a malformed cursor. Project admins and
auditors see the whole queue; other members see only the changes they proposed.

`approve` can answer 200 with `status` still `pending_approval`: your approval is recorded and more approvers are
needed. `bulk` answers HTTP 200 even when rows fail: each failed row is listed in `failed` (`id`, `code`, `reason`)
and left out of `affectedIds`, so compare `affectedIds` with what you intended.
`approvals.comments.add` posts a plain-text comment (1 to 4000 characters) on a pending change; `visibility` is required,
`everyone` or `approvers_only`, and fixed once posted. A comment is never a decision. There is no edit or delete. Posting
needs a session: a personal access token alone is refused with 403 `K9O_RBAC_DENY` (`context.reason` `session_required`).
Other answers: 403 `K9O_APPROVAL_COMMENT_NOT_ELIGIBLE`, 409 `K9O_APPROVAL_COMMENT_CLOSED` (the change is decided, expired
or cancelled), 400 `K9O_INVALID_FIELD_VALUE`, 404 `K9O_NOT_FOUND`. `comments.list` returns the comments the caller may see,
oldest first, `limit` at a time (1 to 200, default 50); `meta.cursor.next` of one page is the `cursor` of the next and is
`None` on the last page.
Arguments are checked before any request; a bad value raises KError `K9O_INVALID_FIELD_VALUE`.
"""

from __future__ import annotations

import re
from typing import Any, TypedDict
from urllib.parse import quote

from .envelope import Envelope
from .errors import KError
from .http import HttpClientConfig, request
from .url_grammar import project_path

_COLLECTION_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]{0,62}$")
_OPERATIONS = ("insert", "update", "delete")
_POLICY_ACTIONS = ("auto_approve", "queue")


def _invalid(message: str, **context: Any) -> KError:
    return KError(code="K9O_INVALID_FIELD_VALUE", message=message, context=context)


def _require_project_slug(config: HttpClientConfig, what: str) -> str:
    if not config.project_slug:
        raise ValueError(f"{what} operations require project_slug bound on the client")
    return config.project_slug


def _require_id(value: Any, what: str) -> str:
    if not isinstance(value, str) or not value:
        raise _invalid(f"{what} id must be a non-empty string")
    return quote(value, safe="")


def _require_reason(reason: Any) -> str:
    if not isinstance(reason, str) or not reason.strip():
        raise _invalid("reason is required and must be a non-empty string")
    return reason


def _require_predicate(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _invalid(f"{field} must be a predicate object")
    return value


def check_limit(limit: Any) -> None:
    """Shared by every list that takes `limit`: a whole number from 1 to 200."""
    if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or limit < 1 or limit > 200):
        raise _invalid("limit must be a whole number from 1 to 200", limit=limit)


COMMENT_VISIBILITIES = ("everyone", "approvers_only")
COMMENT_MAX_CHARS = 4000


class ApprovalComment(TypedDict):
    """One comment on a pending change (`envelope.data` of `comments.add`; an item of `envelope.data` of `comments.list`)."""

    id: str  # `acm_<...>`
    caseId: str  # the pending change
    authorId: str  # opaque user id
    visibility: str  # "everyone" | "approvers_only"; fixed when posted
    body: str  # plain text: render it as text, never as HTML
    createdAt: str  # ISO 8601 date-time


class BulkFailure(TypedDict):
    """One row `approvals.bulk` could not decide."""

    id: str  # the pending change
    code: str  # a K9O_* code such as K9O_STATE_CONFLICT; an open set, so treat it as a string
    reason: str  # the error message for this row


class BulkApprovalsResult(TypedDict):
    """What `approvals.bulk` answers with (`envelope.data`). HTTP is 200 even when rows fail: every matched row is
    in `affectedIds` or in `failed`."""

    attestationId: str
    action: str  # "approve" | "reject"
    affectedCount: int  # len(affectedIds)
    affectedIds: list[str]
    failed: list[BulkFailure]  # empty when every matched row was decided
    reason: str | None  # the request reason, None when absent


class QueuedChange(TypedDict):
    """What a write answers with (`envelope.data`) when it was queued instead of applied (HTTP 202): the change as
    it was proposed. Decision fields (`approverId`, `decidedAt`, `decisionReason`, `createdAt`, `expiresAt`) are not
    part of this answer; read the full change with `approvals.get(id)`."""

    id: str  # `apr_<base62>`; pass it to `approvals.get`, `approve` or `reject`
    status: str  # always "pending_approval"
    collection: str
    operation: str  # "insert" | "update" | "delete"
    makerId: str
    proposedPayload: dict[str, Any]  # insert: the record; update: the patch; delete: {}
    targetRecordId: str | None  # None for an insert


def is_queued_write(result: Envelope) -> bool:
    """True when a write sent with `require_approval` was queued (HTTP 202) instead of applied.

    An `auto_approve` policy applies the write at once even with the flag, so check before reading `result.data`
    as a row. When True, `result.data` is a `QueuedChange` and `result.meta.raw["approval"]["status"]` repeats its status.
    """
    approval = result.meta.raw.get("approval")
    data = result.data
    return (
        isinstance(approval, dict)
        and approval.get("status") == "pending_approval"
        and isinstance(data, dict)
        and data.get("status") == "pending_approval"
    )


class ApprovalCommentsNamespace:
    """`client.approvals.comments`: comments on a pending change. A comment never decides anything."""

    def __init__(self, config: HttpClientConfig) -> None:
        self._config = config

    def _path(self, pending_id: Any) -> str:
        seg = _require_id(pending_id, "pending approval")
        return project_path(_require_project_slug(self._config, "approvals"), f"pending-approvals/{seg}/comments")

    def add(self, pending_id: str, body: str, visibility: str) -> Envelope:
        """Post a comment on a pending change; `result.data` is an `ApprovalComment` (HTTP 201).

        `body` is plain text of 1 to 4000 characters and must not be blank. `visibility` is required: `everyone`
        reaches everyone who can read the change, `approvers_only` only people eligible to decide it; it cannot be
        changed afterwards, and there is no edit or delete. Needs a session: a personal access token alone is refused
        (403 `K9O_RBAC_DENY`). Raises KError `K9O_APPROVAL_COMMENT_NOT_ELIGIBLE` (403) when the caller may not post this
        comment, `K9O_APPROVAL_COMMENT_CLOSED` (409) once the change is decided, expired or cancelled, and
        `K9O_NOT_FOUND` (404) for an unknown change.
        """
        path = self._path(pending_id)
        if not isinstance(body, str) or not body.strip() or len(body) > COMMENT_MAX_CHARS or "\x00" in body:
            raise _invalid(f"body must be plain text of 1 to {COMMENT_MAX_CHARS} characters, not blank")
        if visibility not in COMMENT_VISIBILITIES:
            raise _invalid('visibility must be "everyone" or "approvers_only"', visibility=visibility)
        return request(self._config, method="POST", path=path, body={"body": body, "visibility": visibility})

    def list(self, pending_id: str, *, limit: int | None = None, cursor: str | None = None) -> Envelope:
        """The comments on a pending change that the caller may see, oldest first, `limit` at a time (1 to 200, default 50).

        `result.data` is a list of `ApprovalComment`. Pass `result.meta.cursor.next` as `cursor` for the next page;
        it is `None` on the last page. Comments stay readable after the change is decided.
        """
        path = self._path(pending_id)
        check_limit(limit)
        if cursor is not None and not isinstance(cursor, str):
            raise _invalid("cursor must be a string", cursor=cursor)
        query = {k: v for k, v in {"limit": limit, "cursor": cursor}.items() if v is not None}
        return request(self._config, method="GET", path=path, query=query or None)


class ApprovalsNamespace:
    def __init__(self, config: HttpClientConfig) -> None:
        self._config = config
        self.comments = ApprovalCommentsNamespace(config)

    def _path(self, tail: str = "") -> str:
        return project_path(_require_project_slug(self._config, "approvals"), "pending-approvals" + tail)

    def list(self, *, status: str | None = None, limit: int | None = None, cursor: str | None = None) -> Envelope:
        """List pending changes, newest first, `limit` at a time (1 to 200, default 50).

        `status` is one of `pending_approval`, `approved`, `rejected`, `expired`, `redeemed`. Pass
        `result.meta.cursor.next` as `cursor` for the next page; it is `None` on the last page. A cursor only works
        with the same `status` it was issued for, otherwise KError `K9O_INVALID_CURSOR`.
        """
        check_limit(limit)
        if cursor is not None and not isinstance(cursor, str):
            raise _invalid("cursor must be a string", cursor=cursor)
        query = {k: v for k, v in {"status": status, "limit": limit, "cursor": cursor}.items() if v is not None}
        return request(self._config, method="GET", path=self._path(), query=query or None)

    def get(self, id: str) -> Envelope:
        """One pending change. Members who are not queue admins can read only their own."""
        return request(self._config, method="GET", path=self._path("/" + _require_id(id, "approval")))

    def approve(self, id: str, *, reason: str | None = None) -> Envelope:
        """Approve a pending change. Read `status` on the answer: `pending_approval` means more approvers are needed."""
        seg = _require_id(id, "approval")
        return request(self._config, method="POST", path=self._path(f"/{seg}/approve"),
                       body={} if reason is None else {"reason": reason})

    def reject(self, id: str, reason: str) -> Envelope:
        """Reject a pending change. `reason` is required and must not be empty."""
        seg = _require_id(id, "approval")
        return request(self._config, method="POST", path=self._path(f"/{seg}/reject"),
                       body={"reason": _require_reason(reason)})

    def bulk(self, action: str, criteria: dict[str, Any], reason: str | None = None) -> Envelope:
        """Approve or reject every pending change matching `criteria` (a predicate object).

        `result.data` is a `BulkApprovalsResult`. The call answers HTTP 200 even when rows fail: those rows are in
        `failed` (`id`, `code`, `reason`; an empty list when none) and not in `affectedIds`, so compare
        `affectedIds` with what you intended. `reason` is required when `action` is `reject`.
        """
        if action not in ("approve", "reject"):
            raise _invalid('action must be "approve" or "reject"', action=action)
        body: dict[str, Any] = {"action": action, "criteria": _require_predicate(criteria, "criteria")}
        if action == "reject":
            body["reason"] = _require_reason(reason)
        elif reason is not None:
            body["reason"] = reason
        res = request(self._config, method="POST", path=self._path("/bulk"), body=body)
        if isinstance(res.data, dict) and not isinstance(res.data.get("failed"), list):
            res.data["failed"] = []
        return res


class ApprovalPoliciesNamespace:
    def __init__(self, config: HttpClientConfig) -> None:
        self._config = config

    def _path(self, tail: str = "") -> str:
        return project_path(_require_project_slug(self._config, "approval_policies"), "approval-policies" + tail)

    def list(self) -> Envelope:
        """All policies, oldest first. The first matching policy for a write decides."""
        return request(self._config, method="GET", path=self._path())

    def create(self, collection: str, operation: str, action: str, predicate: dict[str, Any]) -> Envelope:
        """Create a policy. Identical policies are not de-duplicated."""
        if not isinstance(collection, str) or not _COLLECTION_RE.match(collection):
            raise _invalid("collection must match ^[a-zA-Z_][a-zA-Z0-9_]{0,62}$", collection=collection)
        if operation not in _OPERATIONS:
            raise _invalid('operation must be "insert", "update" or "delete"', operation=operation)
        if action not in _POLICY_ACTIONS:
            raise _invalid('action must be "auto_approve" or "queue"', action=action)
        body = {"collection": collection, "operation": operation, "action": action,
                "predicate": _require_predicate(predicate, "predicate")}
        return request(self._config, method="POST", path=self._path(), body=body)

    def delete(self, id: str) -> Envelope:
        """Delete a policy; the answer is `{"id": ...}`."""
        return request(self._config, method="DELETE", path=self._path("/" + _require_id(id, "policy")))
