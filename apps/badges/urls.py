"""Digital Entry Pass routes (Phase 3 Prompt 2).

Operational routes live under `ops/` exactly like the existing review and
accreditation surfaces. Every state change is a POST-only route; nothing
here accepts a credential value, a QR string, or a fallback reference in a
URL path or query string.
"""

from __future__ import annotations

from django.urls import path

from . import views

app_name = "badges"

urlpatterns = [
    # Operational credential surface
    path(
        "ops/badges/registrations/<uuid:pk>/credential/",
        views.registration_credential,
        name="registration-credential",
    ),
    path(
        "ops/badges/registrations/<uuid:pk>/credential/generate/",
        views.credential_generate,
        name="credential-generate",
    ),
    path(
        "ops/badges/credentials/<uuid:pk>/activate/",
        views.credential_activate,
        name="credential-activate",
    ),
    path(
        "ops/badges/credentials/<uuid:pk>/suspend/",
        views.credential_suspend,
        name="credential-suspend",
    ),
    path(
        "ops/badges/credentials/<uuid:pk>/resume/",
        views.credential_resume,
        name="credential-resume",
    ),
    path(
        "ops/badges/credentials/<uuid:pk>/revoke/",
        views.credential_revoke,
        name="credential-revoke",
    ),
    path(
        "ops/badges/credentials/<uuid:pk>/replace/",
        views.credential_replace,
        name="credential-replace",
    ),
    # Controlled fallback-reference lookup (exact match only)
    path(
        "ops/badges/fallback-reference/",
        views.fallback_reference_lookup,
        name="fallback-reference-lookup",
    ),
    # Verification keys (public material only)
    path("ops/badges/verification-keys/", views.verification_keys, name="verification-keys"),
    path(
        "ops/badges/verification-keys/<uuid:pk>/promote/",
        views.verification_key_promote,
        name="verification-key-promote",
    ),
    path(
        "ops/badges/verification-keys/<uuid:pk>/retire/",
        views.verification_key_retire,
        name="verification-key-retire",
    ),
    path(
        "ops/badges/verification-keys/<uuid:pk>/revoke/",
        views.verification_key_revoke,
        name="verification-key-revoke",
    ),
    path(
        "ops/badges/verification-keys/set/",
        views.verification_key_set,
        name="verification-key-set",
    ),
    # Participant-facing
    path("my-passes/", views.participant_passes, name="participant-passes"),
    # Addressed by the series' random public identifier, never by a database
    # primary key or a time-ordered UUIDv7 (TRD s13.3: every externally
    # exposed resource carries a random public identifier).
    path(
        "my-passes/<slug:public_id>/print/",
        views.participant_pass_print,
        name="participant-pass-print",
    ),
    # Authenticated QR image endpoint. The URL carries only the series'
    # random public identifier -- never the credential token.
    path(
        "my-passes/<slug:public_id>/qr.png",
        views.participant_pass_qr,
        name="participant-pass-qr",
    ),
    # Generic physical badge stock (Phase 3 Prompt 3, ADR-0020)
    path(
        "ops/badges/stock/<uuid:event_pk>/",
        views.stock_dashboard,
        name="stock-dashboard",
    ),
    path(
        "ops/badges/stock/<uuid:event_pk>/locations/create/",
        views.stock_location_create,
        name="stock-location-create",
    ),
    path(
        "ops/badges/stock/<uuid:event_pk>/batches/create/",
        views.print_batch_create,
        name="print-batch-create",
    ),
    path(
        "ops/badges/stock/batches/<uuid:pk>/",
        views.print_batch_detail,
        name="print-batch-detail",
    ),
    path(
        "ops/badges/stock/batches/<uuid:pk>/status/",
        views.print_batch_status_change,
        name="print-batch-status-change",
    ),
    path(
        "ops/badges/stock/batches/<uuid:pk>/receive/",
        views.print_batch_receive,
        name="print-batch-receive",
    ),
    path(
        "ops/badges/stock/<uuid:event_pk>/locations/<uuid:source_pk>/transfer/",
        views.stock_transfer_create,
        name="stock-transfer-create",
    ),
    path(
        "ops/badges/stock/<uuid:event_pk>/locations/<uuid:location_pk>/adjust/",
        views.stock_adjustment_create,
        name="stock-adjustment-create",
    ),
    path(
        "ops/badges/stock/<uuid:event_pk>/locations/<uuid:location_pk>/reconcile/",
        views.stock_reconciliation_create,
        name="stock-reconciliation-create",
    ),
    path(
        "ops/badges/stock/<uuid:event_pk>/locations/<uuid:location_pk>/allocate/",
        views.stock_allocation_create,
        name="stock-allocation-create",
    ),
    path(
        "ops/badges/stock/<uuid:event_pk>/allocations/<uuid:pk>/release/",
        views.stock_allocation_release,
        name="stock-allocation-release",
    ),
    # Badge issuance -- exact Registration Context handover
    path(
        "ops/badges/registrations/<uuid:pk>/badge/",
        views.registration_badge_issuance,
        name="registration-badge-issuance",
    ),
    path(
        "ops/badges/registrations/<uuid:pk>/badge/issue/",
        views.badge_issue,
        name="badge-issue",
    ),
    path(
        "ops/badges/registrations/<uuid:registration_pk>/badge-issuances/<uuid:pk>/replace/",
        views.badge_issuance_replace,
        name="badge-issuance-replace",
    ),
    path(
        "ops/badges/registrations/<uuid:registration_pk>/badge-issuances/<uuid:pk>/return/",
        views.badge_issuance_return,
        name="badge-issuance-return",
    ),
    path(
        "ops/badges/registrations/<uuid:registration_pk>/badge-issuances/<uuid:pk>/lost/",
        views.badge_issuance_mark_lost,
        name="badge-issuance-mark-lost",
    ),
    path(
        "ops/badges/registrations/<uuid:registration_pk>/badge-issuances/<uuid:pk>/void/",
        views.badge_issuance_void,
        name="badge-issuance-void",
    ),
]
