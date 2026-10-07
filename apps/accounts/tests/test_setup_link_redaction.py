"""A staff credential setup link never reaches an application log line.

The first request of a setup link carries its single-use link_part in the path
(`/accounts/setup/<link_part>/`). Every log path of the application -- the
redaction function, the structured formatter (message, `extra=` fields,
exception text) and Django's own request logger -- masks it. Synthetic
secrets generated here; no real link is ever used."""

from __future__ import annotations

import io
import json
import logging
import secrets
from urllib.parse import quote

import pytest
from django.test import Client
from django.urls import reverse

from apps.accounts import administration as admin
from apps.core.logging_config import StructuredFormatter
from apps.core.redaction import MASK, RedactingLogFilter, redact


def _link_part() -> str:
    return secrets.token_urlsafe(32)


def _variants(link_part: str) -> list[str]:
    path = f"/accounts/setup/{link_part}/"
    full = f"https://registration.example.test{path}"
    return [
        f"GET {path} HTTP/1.1",
        f'10.0.0.1 - - [07/Oct/2026:10:00:00 +0000] "GET {path} HTTP/1.1" 200 0',
        f"Not Found: {path}",
        f"Not Found: /accounts/setup/{link_part}",
        full,
        f"{full}?lang=fr",
        f"/fr/accounts/setup/{link_part}/",
        f"/ACCOUNTS/SETUP/{link_part}/",
        f"/accounts/ops/sign-in/?next={quote(path, safe='')}",
        f"/accounts/ops/sign-in/?next={quote(quote(path, safe=''), safe='')}",
        f"referer={quote(full, safe='')}",
        f'<a href="{full}">link</a>',
    ]


@pytest.mark.parametrize("index", range(12))
def test_redact_masks_every_supported_form_of_a_setup_link(index):
    link_part = _link_part()
    text = _variants(link_part)[index]
    result = redact(text)
    assert link_part not in result
    assert MASK in result


def test_the_clean_setup_page_and_other_paths_are_left_alone():
    for text in (
        "GET /accounts/setup/ HTTP/1.1",
        "POST /accounts/setup/ HTTP/1.1",
        "/accounts/setup/?x=1",
        "GET /accounts/ops/sign-in/ HTTP/1.1",
        "/ops/staff-accounts/",
    ):
        assert redact(text) == text


def _structured(record: logging.LogRecord) -> dict:
    RedactingLogFilter().filter(record)
    return json.loads(StructuredFormatter().format(record))


def test_the_structured_formatter_masks_message_args_and_extra_fields():
    link_part = _link_part()
    record = logging.LogRecord(
        "django.request",
        logging.WARNING,
        __file__,
        1,
        "Not Found: %s",
        (f"/accounts/setup/{link_part}/",),
        None,
    )
    record.request_path = f"/accounts/setup/{link_part}/"
    record.context = {"url": f"https://registration.example.test/accounts/setup/{link_part}/"}
    line = json.dumps(_structured(record))
    assert link_part not in line
    assert line.count(MASK) >= 3


def test_the_structured_formatter_masks_exception_text_and_tracebacks():
    link_part = _link_part()
    try:
        raise ValueError(f"cannot open /accounts/setup/{link_part}/ for this session")
    except ValueError:
        import sys

        record = logging.LogRecord(
            "asc2026", logging.ERROR, __file__, 1, "failure", (), sys.exc_info()
        )
    record.stack_info = f"  File x, line 1, in view\n    open('/accounts/setup/{link_part}/')"
    payload = _structured(record)
    assert link_part not in json.dumps(payload)
    assert MASK in payload["exc_info"]
    assert MASK in payload["stack_info"]


@pytest.mark.django_db
def test_django_request_log_of_an_unknown_setup_link_is_masked():
    """An expired or mistyped link is redirected at once to the clean page,
    which answers 404 and is logged by Django: nothing in the handler chain's
    output holds the link part."""
    link_part = _link_part()
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(StructuredFormatter())
    handler.addFilter(RedactingLogFilter())
    logger = logging.getLogger("django.request")
    logger.addHandler(handler)
    client = Client()
    try:
        start = client.get(reverse("accounts:credential-setup-start", kwargs={"token": link_part}))
        page = client.get(start["Location"])
    finally:
        logger.removeHandler(handler)
    assert start.status_code == 302 and start["Location"] == reverse("accounts:credential-setup")
    assert start["Cache-Control"].startswith("no-store")
    assert start["Referrer-Policy"] == "no-referrer"
    assert page.status_code == 404 and page["Cache-Control"].startswith("no-store")
    output = stream.getvalue()
    assert "accounts/setup/" in output
    assert link_part not in output


def test_the_setup_url_built_for_the_email_is_masked_when_logged():
    link_part = _link_part()
    url = admin.setup_url(link_part)
    assert link_part in url
    assert link_part not in redact(f"sent link {url}")


# ---------------------------------------------------------------------------
# Secret characters that are themselves percent-encoded (review A02-01)
# ---------------------------------------------------------------------------


def _pct(char: str, *, double: bool = False, lower: bool = False) -> str:
    code = f"{ord(char):02x}" if lower else f"{ord(char):02X}"
    return f"%25{code}" if double else f"%{code}"


def _encoded_forms(link_part: str) -> dict[str, str]:
    first = _pct(link_part[0]) + link_part[1:]
    middle = len(link_part) // 2
    interior = link_part[:middle] + _pct(link_part[middle]) + link_part[middle + 1 :]
    every = "".join(_pct(c) for c in link_part)
    double = "".join(_pct(c, double=True) if i % 3 == 0 else c for i, c in enumerate(link_part))
    lower = "".join(_pct(c, lower=True) if i % 2 else c for i, c in enumerate(link_part))
    return {
        "first": f"GET /accounts/setup/{first}/ HTTP/1.1",
        "interior": f"GET /accounts/setup/{interior}/ HTTP/1.1",
        "every": f"Not Found: /accounts/setup/{every}/",
        "double": f"/accounts/ops/sign-in/?next=%252Faccounts%252Fsetup%252F{double}%252F",
        "lower-case": f"/accounts/setup/{lower}",
        "mixed-separators": f"/accounts%2Fsetup/{interior}%2F",
        "mixed-separators-2": f"/accounts/setup%2F{first}/?lang=ar",
        "referer": f'"https://registration.example.test/accounts/setup/{every}/" "Mozilla/5.0"',
        "full-url": f"https://registration.example.test/accounts%252Fsetup%252F{double}",
    }


def _fragments(link_part: str, size: int = 6) -> set[str]:
    return {link_part[i : i + size] for i in range(len(link_part) - size + 1)}


def _decoded(text: str) -> str:
    from urllib.parse import unquote

    for _ in range(3):
        text = unquote(text)
    return text


@pytest.mark.parametrize(
    "form",
    [
        "first",
        "interior",
        "every",
        "double",
        "lower-case",
        "mixed-separators",
        "mixed-separators-2",
        "referer",
        "full-url",
    ],
)
def test_no_fragment_of_an_encoded_secret_survives(form):
    link_part = _link_part()
    text = _encoded_forms(link_part)[form]
    assert link_part in _decoded(text)  # the synthetic form really holds the secret
    result = redact(text)
    assert MASK in result
    leftover = _decoded(result)
    assert not any(fragment in leftover for fragment in _fragments(link_part)), (form, result)


def test_unrelated_parts_of_a_log_line_are_kept():
    link_part = _link_part()
    encoded = _pct(link_part[0]) + link_part[1:]
    line = (
        f'10.0.0.1 - - [07/Oct/2026:10:00:00 +0000] "GET /accounts/setup/{encoded}/?lang=fr '
        'HTTP/1.1" 404 1234 "-" "Mozilla/5.0"'
    )
    result = redact(line)
    assert result == line.replace(encoded, MASK)
    assert '?lang=fr HTTP/1.1" 404 1234 "-" "Mozilla/5.0"' in result


def test_the_formatter_masks_an_encoded_secret_in_every_field():
    link_part = _link_part()
    every = "".join(_pct(c) for c in link_part)
    try:
        raise ValueError(f"cannot open /accounts/setup/{every}/")
    except ValueError:
        import sys

        record = logging.LogRecord(
            "django.request", logging.WARNING, __file__, 1, "Not Found: %s",
            (f"/accounts/setup/{every}/",), sys.exc_info(),
        )  # fmt: skip
    record.referer = f"https://registration.example.test/accounts/setup/{every}/"
    line = json.dumps(_structured(record))
    assert not any(fragment in _decoded(line) for fragment in _fragments(link_part))
