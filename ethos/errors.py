"""Error taxonomy, HTTP mapping, and retry classification."""
from __future__ import annotations

from enum import Enum
from typing import Any

__all__ = [
    "ErrorCode", "FailureCode", "FAILURE_CODE_TO_STATUS", "RetryClass",
    "classify_sqlstate", "CONFIRMED_ABORT_STATES", "UNCERTAIN_STATES",
    "INTEGRITY_STATES", "IDEMPOTENCY_UNIQUE_CONSTRAINT",
    "EthosError", "ValidationError", "NotFoundError", "PermissionDenied",
    "RuleViolation", "VersionConflict", "IdempotencyKeyReused", "ProtocolError",
]


class ErrorCode(str, Enum):
    VALIDATION_ERROR = "VALIDATION_ERROR"
    NOT_FOUND = "NOT_FOUND"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    RULE_VIOLATION = "RULE_VIOLATION"
    VERSION_CONFLICT = "VERSION_CONFLICT"
    ACTION_FAILED = "ACTION_FAILED"
    IDEMPOTENCY_KEY_REUSED = "IDEMPOTENCY_KEY_REUSED"
    UNAUTHORIZED = "UNAUTHORIZED"
    PAYLOAD_TOO_LARGE = "PAYLOAD_TOO_LARGE"
    INTERNAL_ERROR = "INTERNAL_ERROR"
    PROTOCOL_ERROR = "PROTOCOL_ERROR"


class FailureCode(str, Enum):
    """Codes that can be persisted on a failed event. Closed set."""

    ACTOR_NOT_FOUND = "ACTOR_NOT_FOUND"
    ENTITY_NOT_FOUND = "ENTITY_NOT_FOUND"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    RULE_VIOLATION = "RULE_VIOLATION"
    ACTION_FAILED = "ACTION_FAILED"


# Single source for both first execution and idempotent replay, so the two paths
# cannot disagree about the HTTP status of a recorded failure.
FAILURE_CODE_TO_STATUS: dict[FailureCode, int] = {
    FailureCode.ACTOR_NOT_FOUND: 404,
    FailureCode.ENTITY_NOT_FOUND: 404,
    FailureCode.PERMISSION_DENIED: 403,
    FailureCode.RULE_VIOLATION: 409,
    FailureCode.ACTION_FAILED: 409,
}


class EthosError(Exception):
    code: ErrorCode = ErrorCode.INTERNAL_ERROR
    http_status: int = 500

    def __init__(
        self,
        message: str,
        *,
        code: ErrorCode | None = None,
        http_status: int | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        if http_status is not None:
            self.http_status = http_status
        self.details = details or {}


class ValidationError(EthosError):
    code = ErrorCode.VALIDATION_ERROR
    http_status = 422


class NotFoundError(EthosError):
    code = ErrorCode.NOT_FOUND
    http_status = 404


class PermissionDenied(EthosError):
    code = ErrorCode.PERMISSION_DENIED
    http_status = 403


class VersionConflict(EthosError):
    code = ErrorCode.VERSION_CONFLICT
    http_status = 409


class IdempotencyKeyReused(EthosError):
    code = ErrorCode.IDEMPOTENCY_KEY_REUSED
    http_status = 409


class ProtocolError(EthosError):
    """Internal invariant broken, e.g. an unlocked state read on the write path."""

    code = ErrorCode.PROTOCOL_ERROR
    http_status = 500


class RuleViolation(EthosError):
    """A business rule refused the action. Recorded as a failed event."""

    code = ErrorCode.RULE_VIOLATION
    http_status = 409

    def __init__(
        self,
        message: str,
        *,
        failure_code: FailureCode = FailureCode.RULE_VIOLATION,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message, details=details)
        self.failure_code = failure_code
        self.http_status = FAILURE_CODE_TO_STATUS[failure_code]


class RetryClass(str, Enum):
    NONE = "none"
    CONFIRMED_ABORT = "confirmed_abort"
    INTEGRITY = "integrity"
    UNCERTAIN = "uncertain"


CONFIRMED_ABORT_STATES = frozenset({"40001", "40P01"})
UNCERTAIN_STATES = frozenset({"08006", "08003", "08001"})
INTEGRITY_STATES = frozenset({"23505", "23503", "23514"})

IDEMPOTENCY_UNIQUE_CONSTRAINT = "environment_events_idempotency_uk"


def classify_sqlstate(sqlstate: str | None) -> RetryClass:
    """Never retry an uncertain commit; reconcile by idempotency key instead."""
    if sqlstate is None:
        return RetryClass.NONE
    if sqlstate in CONFIRMED_ABORT_STATES:
        return RetryClass.CONFIRMED_ABORT
    if sqlstate in UNCERTAIN_STATES:
        return RetryClass.UNCERTAIN
    if sqlstate in INTEGRITY_STATES:
        return RetryClass.INTEGRITY
    return RetryClass.NONE
