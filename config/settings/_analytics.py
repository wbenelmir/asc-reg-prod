"""Google Analytics 4: the one place that defines its configuration.

Used by `config.settings.production` only; every other settings module keeps
`ANALYTICS_GA4_MEASUREMENT_ID = ""` (base), so development, test and staging
never load Google's tag.

* `ANALYTICS_GA4_MEASUREMENT_ID` (environment, optional): the production web
  stream's measurement ID. Unset means `DEFAULT_GA4_MEASUREMENT_ID`; an empty
  value switches analytics off. Anything else must look like `G-XXXXXXXXXX`.
* `ga4_content_security_policy` adds exactly the sources Google documents for
  Google Analytics without Ads features ("Content Security Policy" guide of
  the Google tag platform): the tag loader host for scripts, the collection
  hosts for `connect-src` and `img-src`. No 'unsafe-inline', no nonce, no
  bare wildcard: the tag is started by the same-origin
  `static/js/asc-analytics.js`, never by an inline snippet.
"""

from __future__ import annotations

import re

from django.core.exceptions import ImproperlyConfigured

#: The production web stream of https://reg.africanstartupconference.org.
DEFAULT_GA4_MEASUREMENT_ID = "G-FDXGG6H52P"

MEASUREMENT_ID_PATTERN = re.compile(r"G-[A-Z0-9]{4,20}")

GOOGLE_TAG_ORIGIN = "https://www.googletagmanager.com"
GA4_CONNECT_SOURCES = (
    GOOGLE_TAG_ORIGIN,
    "https://*.google-analytics.com",
    "https://*.analytics.google.com",
    "https://*.google.com",
)
GA4_IMAGE_SOURCES = (
    GOOGLE_TAG_ORIGIN,
    "https://*.google-analytics.com",
)


def measurement_id_from(raw: str | None) -> str:
    """The validated measurement ID, or "" when analytics is switched off."""
    value = (DEFAULT_GA4_MEASUREMENT_ID if raw is None else raw).strip()
    if value and not MEASUREMENT_ID_PATTERN.fullmatch(value):
        raise ImproperlyConfigured("ANALYTICS_GA4_MEASUREMENT_ID must look like G-XXXXXXXXXX.")
    return value


def _extend(sources: tuple[str, ...], extra: tuple[str, ...]) -> tuple[str, ...]:
    return (*sources, *(source for source in extra if source not in sources))


def ga4_content_security_policy(policy: dict, measurement_id: str) -> dict:
    """`policy` with the Google Analytics sources added when `measurement_id`
    is set; the same policy, unchanged, when analytics is off."""
    if not measurement_id:
        return policy
    return {
        **policy,
        "script-src": _extend(tuple(policy["script-src"]), (GOOGLE_TAG_ORIGIN,)),
        "connect-src": _extend(tuple(policy["connect-src"]), GA4_CONNECT_SOURCES),
        "img-src": _extend(tuple(policy["img-src"]), GA4_IMAGE_SOURCES),
    }
