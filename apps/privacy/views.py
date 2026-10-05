"""Public, read-only legal information (UXR-C1, UXR-F02; UI/UX PUB-07).

Anyone can read the current Privacy Notice and Registration Terms before
giving an email address. The page only reads: it records no acceptance,
consent or audit event, and it never picks an arbitrary version. It uses the
same "current published version, with the English fallback" selector as the
registration wizard, and it shows the language of the version actually
displayed. A missing document is shown as unavailable, never replaced by
invented text. The published drafts keep their own draft labels and
"to be confirmed" markers; nothing here claims legal or ANPDP approval.
"""

from __future__ import annotations

from django.shortcuts import render
from django.utils.translation import get_language, get_language_info
from django.utils.translation import gettext_lazy as _
from django.views.decorators.http import require_GET

from apps.privacy.selectors import effective_published_version_with_fallback

#: The documents shown, in order: (code, anchor id, heading).
LEGAL_DOCUMENTS = (
    ("PRIVACY_NOTICE", "privacy-notice", _("Privacy Notice")),
    ("TERMS", "registration-terms", _("Registration Terms")),
)

#: Public text, but the page carries the language switcher's CSRF token, so it
#: is never stored by a shared or browser cache.
CACHE_POLICY = "private, no-store"


@require_GET
def legal_information(request):
    requested = get_language() or "en"
    documents = []
    for code, anchor, heading in LEGAL_DOCUMENTS:
        version = effective_published_version_with_fallback(code, requested)
        shown_language = version.language if version is not None else None
        documents.append(
            {
                "anchor": anchor,
                "heading": heading,
                "version": version,
                "language": shown_language,
                "language_name": (
                    get_language_info(shown_language)["name_local"] if shown_language else ""
                ),
                "is_fallback": version is not None and shown_language != requested,
                "is_rtl": bool(shown_language and get_language_info(shown_language)["bidi"]),
            }
        )
    response = render(request, "privacy/legal_information.html", {"documents": documents})
    response["Cache-Control"] = CACHE_POLICY
    return response
