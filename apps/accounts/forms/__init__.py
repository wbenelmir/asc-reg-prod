"""Participant OTP forms (AF-AUTH-01) and operational sign-in form."""

from __future__ import annotations

from django import forms
from django.utils.translation import gettext_lazy as _

from apps.core.normalization import normalize_digit_code
from apps.core.widgets import OTP_CODE_LENGTH, OtpCodeInput


class OtpRequestForm(forms.Form):
    # The solved ALTCHA payload (UX-4, M01; UX-C2 option M). The widget posts
    # it in its own hidden `human_check` input, so the template does not render
    # this field. Verified by the view, never here, so an invalid email never
    # consumes a solution.
    human_check = forms.CharField(required=False, widget=forms.HiddenInput, strip=True)
    email = forms.EmailField(
        label=_("Email address"),
        widget=forms.EmailInput(
            attrs={
                "autocomplete": "email",
                "inputmode": "email",
                "placeholder": _("name@example.org"),
            }
        ),
    )


class OtpVerifyForm(forms.Form):
    """The 6-digit sign-in code (UX-1, S-01 and S-02).

    The control is one text input. Typed or pasted separators (spaces,
    no-break spaces, hyphens) are dropped and Arabic-Indic digits become
    ASCII before the shape check. The server contract is unchanged: exactly
    six ASCII digits, so an SMS or email autofill and a pasted "123 456" work.
    """

    code = forms.CharField(
        label=_("Verification code"),
        max_length=12,
        widget=OtpCodeInput(),
        help_text=_("Enter the 6-digit code sent to your email."),
        error_messages={"required": _("Enter the 6-digit code using digits only.")},
    )

    def clean_code(self) -> str:
        code = normalize_digit_code(self.cleaned_data["code"])
        if len(code) != OTP_CODE_LENGTH or not code.isascii() or not code.isdigit():
            raise forms.ValidationError(
                _("Enter the 6-digit code using digits only."), code="invalid"
            )
        return code


class CaptchaAnswerInput(forms.TextInput):
    """The typed security-image answer is never rendered back: every page
    shows a new image with an empty field."""

    def format_value(self, value):
        return None


class OperationalSignInForm(forms.Form):
    email = forms.EmailField(
        label=_("Email address"), widget=forms.EmailInput(attrs={"autocomplete": "username"})
    )
    password = forms.CharField(
        label=_("Password"), widget=forms.PasswordInput(attrs={"autocomplete": "current-password"})
    )
    # Staff sign-in image CAPTCHA (apps.accounts.captcha_guard). Not required
    # here on purpose: the view verifies (and consumes) the session's
    # challenge first and shows the guard's own message; the template renders
    # these two fields itself, never with a value.
    captcha_key = forms.CharField(required=False, max_length=40, widget=forms.HiddenInput)
    captcha_answer = forms.CharField(
        required=False,
        max_length=16,
        label=_("Characters in the image"),
        widget=CaptchaAnswerInput(
            attrs={
                "autocomplete": "off",
                "autocapitalize": "characters",
                "spellcheck": "false",
                "class": "asc-captcha-answer",
            }
        ),
    )
