"""Device API views (Phase 4 Prompts 2 and 3). HTTP coordination only: every
decision and state change lives in `apps.entry.services.offline_*`.

Authentication is explicit per view, never inherited:

* the device: the HttpOnly `Path=/entry/` device cookie (ADR-0021), plus --
  for every signed call -- a proof made with the device's registered
  non-extractable signing key over (purpose, single-use nonce, body digest),
  sent in `X-ASC-Device-Nonce` / `X-ASC-Device-Signature`;
* the person, where a call needs one: the ordinary operational session
  (provisioning and self-test need a device administrator signed in ON the
  device; a grant needs the operator's live checkpoint session).

CSRF is enforced on every call, whether or not a user is signed in. Every
response is `no-store` and, once the device is known, carries the next
nonce in `X-ASC-Next-Nonce`.
"""

from __future__ import annotations

import json

from django.conf import settings
from django.http import HttpResponse
from django.middleware.csrf import CsrfViewMiddleware
from rest_framework.authentication import BaseAuthentication
from rest_framework.exceptions import ParseError, PermissionDenied
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.entry import session_state
from apps.entry.services import EntryPermissionError
from apps.entry.services.devices import authenticate_device
from apps.entry.services.offline_devices import (
    DeviceNotAuthenticated,
    authenticate_device_any_status,
    heartbeat,
    issue_nonce,
    provision_device,
    record_evidence_report,
    record_self_test,
)

NONCE_HEADER = "HTTP_X_ASC_DEVICE_NONCE"
SIGNATURE_HEADER = "HTTP_X_ASC_DEVICE_SIGNATURE"
_MAX_BODY_BYTES = 64 * 1024


class _CsrfCheck(CsrfViewMiddleware):
    def _reject(self, request, reason):
        return reason


def _enforce_csrf(django_request) -> None:
    check = _CsrfCheck(lambda request: None)
    check.process_request(django_request)
    if check.process_view(django_request, None, (), {}) is not None:
        raise PermissionDenied("CSRF verification failed.")


class OperationalSessionUser(BaseAuthentication):
    """The ordinary operational session user (already authenticated, and
    re-validated for status and lifetime, by the Django middleware stack).
    CSRF is enforced separately, for every call, by `DeviceApiView`."""

    def authenticate(self, request):
        user = getattr(request._request, "user", None)
        if user is not None and user.is_authenticated and user.is_active:
            return (user, None)
        return None


class DeviceApiView(APIView):
    """Base class: POST only, JSON only, CSRF always, no-store always."""

    authentication_classes = [OperationalSessionUser]
    permission_classes = [AllowAny]
    http_method_names = ["post"]
    #: Resolve devices in any status (so directives reach them) or only
    #: operational ones (the default for anything that grants capability).
    any_status = False

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        _enforce_csrf(request._request)
        self.device = None
        raw = request._request.body
        if len(raw) > _MAX_BODY_BYTES:
            raise ParseError("Request body too large.")
        self.raw_body = raw or b"{}"
        try:
            payload = json.loads(self.raw_body)
        except ValueError as exc:
            raise ParseError("Malformed JSON.") from exc
        if not isinstance(payload, dict):
            raise ParseError("Malformed JSON.")
        self.payload = payload
        cookie = request._request.COOKIES.get(settings.ENTRY_DEVICE_COOKIE_NAME)
        self.device = (
            authenticate_device_any_status(cookie)
            if self.any_status
            else authenticate_device(cookie)
        )
        if self.device is None:
            raise DeviceNotAuthenticated("No enrolled device.")

    @property
    def nonce(self):
        return self.request._request.META.get(NONCE_HEADER)

    @property
    def signature(self):
        return self.request._request.META.get(SIGNATURE_HEADER)

    def session_user(self):
        user = self.request.user
        if user is None or not user.is_authenticated:
            raise EntryPermissionError("Sign in on this device first.", code="NOT_SIGNED_IN")
        return user

    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        response["Cache-Control"] = "no-store"
        device = getattr(self, "device", None)
        if device is not None:
            response["X-ASC-Next-Nonce"] = issue_nonce(device)
        return response


class HeartbeatView(DeviceApiView):
    """Passive: server time, directives, nonce; accepts a signed state report."""

    any_status = True

    def post(self, request):
        return Response(
            heartbeat(
                device=self.device,
                report=self.payload.get("report"),
                nonce=self.nonce,
                signature=self.signature,
                body=self.raw_body,
            )
        )


class ProvisionView(DeviceApiView):
    def post(self, request):
        provisioning = provision_device(
            device=self.device,
            actor=self.session_user(),
            signing_key_spki=self.payload.get("signing_key"),
            unwrap_key_spki=self.payload.get("unwrap_key"),
            nonce=self.nonce,
            signature=self.signature,
            body=self.raw_body,
        )
        return Response(
            {
                "device": provisioning.device.public_id,
                "trust_anchors": provisioning.trust_anchors,
            },
            status=201,
        )


class PackageView(DeviceApiView):
    """The newest READY package: the stored bytes, byte-identical on retry;
    204 when the device already holds that version; or 202 BUILDING (with
    `Retry-After`) while the device's one open build runs. The request never
    builds a package itself."""

    def post(self, request):
        from apps.entry.offline_contract import (
            PACKAGE_RESPONSE_BUILDING,
            PACKAGE_RESPONSE_NOT_MODIFIED,
        )
        from apps.entry.services.offline_packages import download_package

        have = self.payload.get("have_version")
        if have is not None and (isinstance(have, bool) or not isinstance(have, int)):
            raise ParseError("have_version must be an integer.")
        result = download_package(
            device=self.device,
            have_version=have,
            nonce=self.nonce,
            signature=self.signature,
            body=self.raw_body,
        )
        if result.status == PACKAGE_RESPONSE_BUILDING:
            response = Response(
                {
                    "status": PACKAGE_RESPONSE_BUILDING,
                    "package_version": result.build.package_version if result.build else None,
                    "retry_after_seconds": result.retry_after_seconds,
                },
                status=202,
            )
            response["Retry-After"] = str(result.retry_after_seconds)
            return response
        if result.status == PACKAGE_RESPONSE_NOT_MODIFIED:
            response = HttpResponse(status=204)
        else:
            response = HttpResponse(result.raw, content_type="application/json")
        response["X-ASC-Package-Version"] = str(result.package.package_version)
        return response


class DeltaView(DeviceApiView):
    def post(self, request):
        from apps.entry.services.offline_packages import issue_delta

        _delta, raw = issue_delta(
            device=self.device,
            package_public_id=self.payload.get("package_id"),
            nonce=self.nonce,
            signature=self.signature,
            body=self.raw_body,
        )
        return HttpResponse(raw, content_type="application/json")


class SelfTestView(DeviceApiView):
    def post(self, request):
        ready = record_self_test(
            device=self.device,
            actor=self.session_user(),
            package_public_id=self.payload.get("package_id"),
            checks=self.payload.get("checks"),
            nonce=self.nonce,
            signature=self.signature,
            body=self.raw_body,
        )
        return Response({"ready": ready})


class GrantView(DeviceApiView):
    def post(self, request):
        from apps.entry.services.offline_grants import issue_operator_grant
        from apps.entry.services.sessions import CheckpointUnavailable, resolve_checkpoint

        user = self.session_user()
        device_session_id, operator_session_id = session_state.checkpoint_ids(request._request)
        try:
            checkpoint = resolve_checkpoint(
                device=self.device,
                device_session_id=device_session_id,
                operator_session_id=operator_session_id,
                user=user,
            )
        except CheckpointUnavailable as exc:
            raise EntryPermissionError("No live checkpoint session.", code="NO_CHECKPOINT") from exc
        grant, compact = issue_operator_grant(
            checkpoint=checkpoint,
            nonce=self.nonce,
            signature=self.signature,
            body=self.raw_body,
        )
        return Response(
            {
                "grant": compact,
                "grant_id": grant.public_id,
                "expires_at": int(grant.expires_at.timestamp()),
            },
            status=201,
        )


class WipeReportView(DeviceApiView):
    """Evidence metadata a device reports right before an emergency wipe."""

    any_status = True

    def post(self, request):
        record_evidence_report(
            device=self.device,
            report=self.payload,
            nonce=self.nonce,
            signature=self.signature,
            body=self.raw_body,
        )
        return Response({"recorded": True}, status=201)


class SyncView(DeviceApiView):
    """Ordered, idempotent upload of signed offline operations (Phase 4
    Prompt 3, ADR-0024). Any device status: a device that is no longer
    operational still uploads its evidence, which is quarantined, never
    applied. Each acknowledgement is per operation; a durable one is signed."""

    any_status = True

    def post(self, request):
        from apps.entry.services.offline_sync import synchronize

        result = synchronize(
            device=self.device,
            payload=self.payload,
            nonce=self.nonce,
            signature=self.signature,
            body=self.raw_body,
        )
        return Response(result.as_json())


class QuarantineView(APIView):
    """Quarantine ingestion for a REVOKED device (binding decision P2-F).

    A revoked device has no device credential any more, so this endpoint
    has no cookie authentication: every operation must instead carry a
    valid signature by a signing key registered to the named device (the
    key is retired at revocation, never deleted). Nothing unattributable is
    stored, nothing is ever applied, and uploads are rate-limited per
    device. JSON only, CSRF always, `no-store`, no nonce (each operation is
    individually signed and idempotent by its id)."""

    authentication_classes = []
    permission_classes = [AllowAny]
    http_method_names = ["post"]

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        _enforce_csrf(request._request)
        raw = request._request.body
        if len(raw) > _MAX_BODY_BYTES:
            raise ParseError("Request body too large.")
        try:
            payload = json.loads(raw or b"{}")
        except ValueError as exc:
            raise ParseError("Malformed JSON.") from exc
        if not isinstance(payload, dict):
            raise ParseError("Malformed JSON.")
        self.payload = payload

    def post(self, request):
        from apps.entry.services.offline_sync import synchronize_quarantine

        return Response(synchronize_quarantine(payload=self.payload).as_json())

    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        response["Cache-Control"] = "no-store"
        return response
