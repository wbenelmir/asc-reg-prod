"""Django admin registrations for apps.reviews (read-only evidence browsing).

No admin action performs a business transition -- every state change stays
in `apps.reviews.services`, never in an admin `save_model`/action.
"""

from __future__ import annotations

from django.contrib import admin

from .models import (
    ChecklistDefinition,
    ChecklistResult,
    DuplicateCandidateResolution,
    InformationRequest,
    InformationResponse,
    InternalReviewNote,
    RegistrationDecision,
    RequestItem,
    ResponseItem,
    ReviewAssignment,
    ReviewCase,
)


@admin.register(ReviewCase)
class ReviewCaseAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "registration",
        "case_type",
        "queue_code",
        "status",
        "priority",
        "opened_at",
    )
    list_filter = ("case_type", "queue_code", "status")
    search_fields = ("id", "registration__public_reference")
    readonly_fields = [f.name for f in ReviewCase._meta.fields]


@admin.register(ReviewAssignment)
class ReviewAssignmentAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "review_case",
        "assigned_user",
        "assigned_group",
        "is_current",
        "started_at",
    )
    list_filter = ("is_current",)
    readonly_fields = [f.name for f in ReviewAssignment._meta.fields]


@admin.register(ChecklistDefinition)
class ChecklistDefinitionAdmin(admin.ModelAdmin):
    list_display = ("id", "event_edition", "case_type", "version_label", "is_active")
    list_filter = ("case_type", "is_active")


@admin.register(ChecklistResult)
class ChecklistResultAdmin(admin.ModelAdmin):
    list_display = ("id", "review_case", "item_code", "result", "recorded_at")
    list_filter = ("result",)
    readonly_fields = [f.name for f in ChecklistResult._meta.fields]


@admin.register(InternalReviewNote)
class InternalReviewNoteAdmin(admin.ModelAdmin):
    list_display = ("id", "review_case", "created_by", "created_at")
    readonly_fields = ("id", "review_case", "created_by", "created_at", "updated_at")
    # `note_encrypted` deliberately excluded from list_display/readonly
    # display -- never surface encrypted note content in admin list views.
    exclude = ("note_encrypted",)


@admin.register(InformationRequest)
class InformationRequestAdmin(admin.ModelAdmin):
    list_display = ("id", "registration", "sequence", "status", "purpose", "sent_at")
    list_filter = ("status", "purpose")
    search_fields = ("id", "registration__public_reference")


@admin.register(RequestItem)
class RequestItemAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "information_request",
        "kind",
        "field_code",
        "document_type",
        "is_required",
    )


@admin.register(InformationResponse)
class InformationResponseAdmin(admin.ModelAdmin):
    list_display = ("id", "information_request", "submitted_by_person", "submitted_at")
    readonly_fields = [f.name for f in InformationResponse._meta.fields]


@admin.register(ResponseItem)
class ResponseItemAdmin(admin.ModelAdmin):
    list_display = ("id", "response", "request_item", "document")
    exclude = ("value_encrypted",)


@admin.register(DuplicateCandidateResolution)
class DuplicateCandidateResolutionAdmin(admin.ModelAdmin):
    list_display = ("id", "review_case", "outcome", "reviewed_by", "reviewed_at")
    list_filter = ("outcome",)
    readonly_fields = [f.name for f in DuplicateCandidateResolution._meta.fields]


@admin.register(RegistrationDecision)
class RegistrationDecisionAdmin(admin.ModelAdmin):
    list_display = ("id", "registration", "sequence", "outcome", "is_current", "decided_at")
    list_filter = ("outcome", "is_current")
    search_fields = ("id", "registration__public_reference")
    readonly_fields = [f.name for f in RegistrationDecision._meta.fields]
