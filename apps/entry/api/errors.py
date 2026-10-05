"""Consistent, machine-readable API errors (TRD API-004).

Every error is `{"error": {"code", "message", "correlation_id"}}`: a stable
code, a SAFE localized message (never an exception's own text), and the
request correlation id. Nothing else -- no field value, token, or trace.
"""

from __future__ import annotations

from rest_framework import status as http
from rest_framework.exceptions import APIException, ParseError, PermissionDenied
from rest_framework.response import Response

from apps.core.middleware.correlation import get_correlation_id
from apps.entry.services import (
    EntryConcurrencyError,
    EntryPermissionError,
    EntryServiceError,
    EntryStateError,
)


def error_response(*, code: str, status: int, message: str = "") -> Response:
    from apps.entry.presentation import offline_error_message

    response = Response(
        {
            "error": {
                "code": code,
                "message": message or offline_error_message(code),
                "correlation_id": get_correlation_id() or "",
            }
        },
        status=status,
    )
    response["Cache-Control"] = "no-store"
    return response


def status_for(exc: EntryServiceError) -> int:
    from apps.entry.services.offline_devices import (
        DeviceNotAuthenticated,
        MfaStepUpRequired,
        OfflineRequestRejected,
        OfflineUnavailable,
    )

    if isinstance(exc, DeviceNotAuthenticated):
        return http.HTTP_401_UNAUTHORIZED
    if isinstance(exc, EntryPermissionError | MfaStepUpRequired):
        return http.HTTP_403_FORBIDDEN
    if isinstance(exc, OfflineRequestRejected):
        return http.HTTP_400_BAD_REQUEST
    if isinstance(exc, OfflineUnavailable):
        if exc.code == "RATE_LIMITED":
            return http.HTTP_429_TOO_MANY_REQUESTS
        return http.HTTP_409_CONFLICT
    if isinstance(exc, EntryConcurrencyError | EntryStateError):
        return http.HTTP_409_CONFLICT
    return http.HTTP_400_BAD_REQUEST


def api_exception_handler(exc, context):
    """DRF `EXCEPTION_HANDLER`: never leaks an exception message."""
    if isinstance(exc, EntryServiceError):
        return error_response(code=exc.code or type(exc).__name__.upper(), status=status_for(exc))
    if isinstance(exc, ParseError):
        return error_response(code="MALFORMED_REQUEST", status=http.HTTP_400_BAD_REQUEST)
    if isinstance(exc, PermissionDenied):
        return error_response(code="FORBIDDEN", status=http.HTTP_403_FORBIDDEN)
    if isinstance(exc, APIException):
        return error_response(code="REQUEST_REFUSED", status=exc.status_code)
    return None
