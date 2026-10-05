"""Participant-facing status and accessibility checks without database access."""

from __future__ import annotations

from types import SimpleNamespace

from django import forms
from django.template.loader import render_to_string
from django.test import SimpleTestCase
from django.utils import translation


class RegistrationFeedbackTests(SimpleTestCase):
    def test_every_public_status_has_its_own_localized_explanation(self):
        headings = {
            "DRAFT": "Your request has not been submitted yet",
            "SUBMITTED": "Request received — awaiting acceptance",
            "UNDER_REVIEW": "Your request is under review",
            "ADDITIONAL_INFORMATION_REQUIRED": (
                "Information needed — your participation is not yet approved"
            ),
            "APPROVED": "Your participation has been approved",
            "NOT_APPROVED": "Your participation has not been approved",
            "WITHDRAWN": "This request is closed",
        }
        for language in ("en", "fr", "ar"):
            with translation.override(language):
                for status, heading in headings.items():
                    with self.subTest(language=language, status=status):
                        html = render_to_string(
                            "registrations/_status_guidance.html",
                            {"registration": SimpleNamespace(public_status=status)},
                        )
                        self.assertIn(f'data-registration-guidance="{status}"', html)
                        self.assertIn(translation.gettext(heading), html)
                        if language != "en":
                            self.assertNotEqual(translation.gettext(heading), heading)
                        if status in ("SUBMITTED", "UNDER_REVIEW"):
                            self.assertIn(
                                translation.gettext(
                                    "Submitting a registration request does not mean your "
                                    "participation has been approved. Please wait for the "
                                    "organizers' decision."
                                ),
                                html,
                            )
                        if status != "APPROVED":
                            self.assertNotIn(
                                "<strong>"
                                + translation.gettext("Your participation has been approved")
                                + "</strong>",
                                html,
                            )

    def test_help_and_validation_feedback_follow_the_control_and_stay_linked(self):
        class ExampleForm(forms.Form):
            organization = forms.CharField(
                label="Organization", help_text="Use the official organization name."
            )

        field = ExampleForm(data={})["organization"]
        html = render_to_string("components/field.html", {"field": field})
        self.assertLess(html.index('id="id_organization"'), html.index('id="id_organization_help"'))
        self.assertLess(
            html.index('id="id_organization"'), html.index('id="id_organization_error"')
        )
        self.assertIn('aria-describedby="id_organization_help id_organization_error"', html)
        self.assertIn('aria-invalid="true"', html)
        self.assertIn('for="id_organization"', html)

    def test_conference_identity_names_the_event_and_year_in_every_language(self):
        editions = {"en": "5th edition", "fr": "5e édition", "ar": "الدورة الخامسة"}
        for language in ("en", "fr", "ar"):
            with translation.override(language), self.subTest(language=language):
                html = render_to_string("partials/conference_identity.html")
                self.assertIn(translation.gettext("African Startup Conference"), html)
                self.assertIn('dir="ltr">2026</bdi>', html)
                self.assertIn(editions[language], html)
                self.assertIn('class="asc-conference-edition"', html)
                self.assertIn('alt="" aria-hidden="true"', html)

    def test_receipt_delivery_copy_distinguishes_submission_from_acceptance(self):
        registration = SimpleNamespace(
            public_status="SUBMITTED",
            public_reference="TEST2026-R-000001",
            get_public_status_display=lambda: "Submitted",
        )
        for status in ("SENT", "DELIVERED", "QUEUED", "FAILED", "SENDING", None):
            with translation.override("en"), self.subTest(status=status):
                html = render_to_string(
                    "registrations/confirmation.html",
                    {
                        "registration": registration,
                        "confirmation_message": SimpleNamespace(status=status),
                    },
                )
                self.assertIn("Request received — awaiting acceptance", html)
                self.assertNotIn("Registration confirmed", html)
                self.assertEqual(
                    "acknowledging receipt of your request has been sent" in html,
                    status in ("SENT", "DELIVERED"),
                )
                if status == "FAILED":
                    self.assertIn("receipt email could not be sent", html)

    def test_draft_details_do_not_claim_that_a_receipt_was_sent(self):
        html = render_to_string(
            "registrations/confirmation.html",
            {
                "registration": SimpleNamespace(public_status="DRAFT"),
                "confirmation_message": SimpleNamespace(status="SENT"),
            },
        )
        self.assertIn("Your request has not been submitted yet", html)
        self.assertNotIn("acknowledging receipt of your request has been sent", html)

    def test_each_shell_contains_one_header_logo_and_no_second_brand_strip(self):
        for shell in (
            "base_public",
            "base_workspace",
            "base_wizard",
            "base_operations",
            "base_entry",
            "base_error",
        ):
            for language in ("en", "fr", "ar"):
                with translation.override(language), self.subTest(shell=shell, language=language):
                    html = render_to_string(f"layouts/{shell}.html")
                    self.assertEqual(html.count("data-conference-identity"), 1)
                    self.assertEqual(html.count('src="/static/img/brand/asc-logo.svg"'), 1)
                    self.assertNotIn("asc-conference-lockup.png", html)
                    self.assertNotIn('class="asc-conference-identity"', html)
