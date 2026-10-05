from __future__ import annotations

from django.urls import path

from . import offline_views, views
from .api import views as api_views

app_name = "entry"

urlpatterns = [
    # Enrolled-device PWA (Phase 4 Prompt 2, ADR-0023). The service worker is
    # served from /entry/, so its scope can never exceed /entry/.
    path("entry/offline/", offline_views.offline_shell, name="offline-shell"),
    path("entry/sw.js", offline_views.service_worker, name="service-worker"),
    path("entry/manifest.webmanifest", offline_views.web_manifest, name="web-manifest"),
    # Device API boundary (DRF, binding decision P2-A).
    path(
        "entry/api/v1/offline/heartbeat/",
        api_views.HeartbeatView.as_view(),
        name="api-heartbeat",
    ),
    path(
        "entry/api/v1/offline/provision/",
        api_views.ProvisionView.as_view(),
        name="api-provision",
    ),
    path("entry/api/v1/offline/package/", api_views.PackageView.as_view(), name="api-package"),
    path("entry/api/v1/offline/delta/", api_views.DeltaView.as_view(), name="api-delta"),
    path(
        "entry/api/v1/offline/self-test/",
        api_views.SelfTestView.as_view(),
        name="api-self-test",
    ),
    path("entry/api/v1/offline/grant/", api_views.GrantView.as_view(), name="api-grant"),
    path(
        "entry/api/v1/offline/wipe-report/",
        api_views.WipeReportView.as_view(),
        name="api-wipe-report",
    ),
    # Synchronization of signed offline operations (Phase 4 Prompt 3).
    path("entry/api/v1/offline/sync/", api_views.SyncView.as_view(), name="api-sync"),
    path(
        "entry/api/v1/offline/sync/quarantine/",
        api_views.QuarantineView.as_view(),
        name="api-sync-quarantine",
    ),
    # Supervisor reconciliation queue (operations back office).
    path(
        "ops/entry/events/<uuid:event_pk>/reconciliation/",
        offline_views.reconciliation_queue,
        name="reconciliation-queue",
    ),
    path(
        "ops/entry/reconciliation/<str:public_id>/",
        offline_views.reconciliation_case,
        name="reconciliation-case",
    ),
    path(
        "ops/entry/reconciliation/<str:public_id>/action/",
        offline_views.reconciliation_action,
        name="reconciliation-action",
    ),
    # Offline preparation administration (operations back office).
    path(
        "ops/entry/events/<uuid:event_pk>/offline/",
        offline_views.event_offline_setting,
        name="event-offline-setting",
    ),
    path(
        "ops/entry/devices/<str:public_id>/block-offline/",
        offline_views.device_block_offline,
        name="device-block-offline",
    ),
    path(
        "ops/entry/devices/<str:public_id>/emergency-wipe/",
        offline_views.device_emergency_wipe,
        name="device-emergency-wipe",
    ),
    # Checkpoint (enrolled device + signed-in operator + live checkpoint session).
    path("entry/", views.checkpoint_home, name="home"),
    path("entry/verify/", views.verify, name="verify"),
    path("entry/verify/qr/", views.verify_qr_view, name="verify-qr"),
    path("entry/lookup/identity/", views.lookup_identity_view, name="lookup-identity"),
    path("entry/lookup/reference/", views.lookup_reference_view, name="lookup-reference"),
    path("entry/lookup/manual/", views.manual_search_view, name="lookup-manual"),
    path("entry/select/", views.select_candidate, name="select-candidate"),
    path("entry/decision/", views.record_decision, name="decision"),
    path("entry/override/", views.record_override_view, name="override"),
    path("entry/photo/<str:nonce>/", views.participant_photo, name="photo"),
    path("entry/monitor/", views.checkpoint_monitor, name="monitor"),
    # Passive connection check; listed in settings.OPERATIONAL_PASSIVE_PATHS.
    path("entry/status/", views.checkpoint_status, name="status"),
    path("entry/session/end/", views.end_checkpoint_session, name="session-end"),
    path("entry/device/activate/", views.device_activate, name="device-activate"),
    # Device administration (operations back office).
    path("ops/entry/events/<uuid:event_pk>/devices/", views.device_list, name="device-list"),
    path(
        "ops/entry/events/<uuid:event_pk>/observability/",
        views.observability_dashboard,
        name="observability",
    ),
    path("ops/entry/devices/<str:public_id>/", views.device_detail, name="device-detail"),
    path("ops/entry/devices/<str:public_id>/scope/", views.device_rescope, name="device-rescope"),
    path("ops/entry/devices/<str:public_id>/suspend/", views.device_suspend, name="device-suspend"),
    path("ops/entry/devices/<str:public_id>/resume/", views.device_resume, name="device-resume"),
    path("ops/entry/devices/<str:public_id>/revoke/", views.device_revoke, name="device-revoke"),
    path(
        "ops/entry/devices/<str:public_id>/activation-code/",
        views.device_activation_code,
        name="device-activation-code",
    ),
]
