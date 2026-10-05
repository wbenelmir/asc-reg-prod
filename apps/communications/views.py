"""Read-only, scope-filtered operations view for delivery observability."""

from django.shortcuts import render

from apps.accounts.policies import operational_permission_required
from apps.communications.selectors import communication_messages_visible_to


@operational_permission_required(
    "communications.view_communicationmessage",
    login_url="accounts:operational-sign-in",
)
def operations_messages(request):
    messages = (
        communication_messages_visible_to(request.user)
        .select_related("event_edition", "registration", "template_version__template")
        .prefetch_related("attempts")
        .order_by("-created_at")[:100]
    )
    return render(
        request,
        "communications/operations_messages.html",
        {"communication_messages": messages},
    )
