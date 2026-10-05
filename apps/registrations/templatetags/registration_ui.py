"""Registration wizard presentation tags (UI/UX Completion Gate, Checkpoint 1).

Read-only: the stepper reflects `Registration.current_step` (the furthest
step reached, advanced only by `services.advance_current_step`). It never
changes progress and never grants access; each step view still applies its
own participant and draft checks.
"""

from __future__ import annotations

from django import template
from django.urls import reverse
from django.utils.translation import gettext_lazy as _

register = template.Library()

#: (step code, label, URL name) in wizard order. Mirrors
#: `services._STEP_ORDER` (asserted by the registration UI tests).
WIZARD_STEPS: tuple[tuple[str, object, str], ...] = (
    ("identity", _("Identity"), "registrations:step-identity"),
    ("contact", _("Contact"), "registrations:step-contact"),
    ("professional", _("Professional"), "registrations:step-professional"),
    ("interests", _("Interests"), "registrations:step-interests"),
    ("review", _("Review"), "registrations:step-review"),
    ("notices", _("Notices"), "registrations:step-notices"),
)

_CODES = [code for code, _label, _url in WIZARD_STEPS]


def wizard_step_states(current: str, furthest: str | None) -> list[dict]:
    """States: `current` (this page), `done` (before the furthest step
    reached: linked, with a check), `available` (the furthest step itself,
    when the participant is editing an earlier one: linked), `upcoming`
    (not reached yet: plain text)."""
    current_index = _CODES.index(current)
    furthest_index = _CODES.index(furthest) if furthest in _CODES else current_index
    furthest_index = max(furthest_index, current_index)
    steps = []
    for index, (code, label, url_name) in enumerate(WIZARD_STEPS):
        if index == current_index:
            state = "current"
        elif index < furthest_index:
            state = "done"
        elif index == furthest_index:
            state = "available"
        else:
            state = "upcoming"
        steps.append(
            {
                "code": code,
                "number": index + 1,
                "label": label,
                "url": reverse(url_name),
                "state": state,
            }
        )
    return steps


@register.inclusion_tag("components/stepper.html")
def wizard_stepper(registration, current: str) -> dict:
    furthest = getattr(registration, "current_step", None)
    steps = wizard_step_states(current, furthest)
    number = _CODES.index(current) + 1
    return {
        "steps": steps,
        "current_number": number,
        "progress_percent": round(number * 100 / len(steps)),
    }


@register.simple_tag
def wizard_previous_url(current: str) -> str:
    """URL of the step before `current`, or "" on the first step."""
    index = _CODES.index(current)
    return reverse(WIZARD_STEPS[index - 1][2]) if index > 0 else ""
