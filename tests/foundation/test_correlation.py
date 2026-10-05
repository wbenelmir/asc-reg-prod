"""Request correlation ID middleware and logging filter (Prompt 2 CP 2g)."""

from __future__ import annotations

import logging

from django.http import HttpResponse
from django.test import RequestFactory

from apps.core.middleware.correlation import (
    CORRELATION_ID_HEADER,
    CorrelationIdLogFilter,
    CorrelationIdMiddleware,
    get_correlation_id,
)


def _middleware(get_response):
    return CorrelationIdMiddleware(get_response)


def test_assigns_a_correlation_id_when_none_supplied() -> None:
    captured = {}

    def get_response(request):
        captured["id_during_request"] = get_correlation_id()
        return HttpResponse()

    middleware = _middleware(get_response)
    request = RequestFactory().get("/healthz")

    response = middleware(request)

    assert captured["id_during_request"]
    assert response.headers[CORRELATION_ID_HEADER] == captured["id_during_request"]
    # Context is cleared once the request completes.
    assert get_correlation_id() is None


def test_propagates_a_well_formed_incoming_correlation_id() -> None:
    def get_response(request):
        return HttpResponse()

    middleware = _middleware(get_response)
    request = RequestFactory().get("/healthz", HTTP_X_CORRELATION_ID="abc-123_XYZ")

    response = middleware(request)

    assert response.headers[CORRELATION_ID_HEADER] == "abc-123_XYZ"


def test_rejects_an_oversized_or_malformed_incoming_correlation_id() -> None:
    def get_response(request):
        return HttpResponse()

    middleware = _middleware(get_response)
    # Contains characters outside [A-Za-z0-9_-] -- must not be trusted verbatim.
    request = RequestFactory().get("/healthz", HTTP_X_CORRELATION_ID="not; safe; value")

    response = middleware(request)

    assert response.headers[CORRELATION_ID_HEADER] != "not; safe; value"


def test_log_filter_injects_dash_when_no_request_is_active() -> None:
    log_filter = CorrelationIdLogFilter()
    record = logging.LogRecord(
        name="asc2026",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="no request in flight",
        args=(),
        exc_info=None,
    )
    assert log_filter.filter(record) is True
    assert record.correlation_id == "-"
