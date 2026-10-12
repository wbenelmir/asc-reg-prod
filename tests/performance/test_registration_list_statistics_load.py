"""Registration list (UPD-05) and statistics (UPD-08) at representative size.

Seeds `ASC_PERF_REGISTRATIONS` (default 6000) synthetic registrations in one
event with a realistic status mix (about a third approved with a current
decision, withdrawn, cancelled, under review, submitted, not approved,
drafts), profiles and verified identity cases, then measures, as the
superadmin, through the real views:

* the registration list: first page, a middle page, the last page, a
  filtered page (status + nationality) and a private name search;
* the statistics page for the event;
* the XLSX export of the situation report.

For each: the number of SQL queries (must not grow with the number of
registrations: the list paginates in the database and the statistics are
aggregates) and the median wall time of three runs. The database, the
application and the client share one local machine, so the times are
indicative only. The report is written to
`var/test_artifacts/upd-2026-10/registration_list_statistics_load.json`.
All data is synthetic.
"""

from __future__ import annotations

import datetime
import json
import os
import platform
import statistics
import time
import uuid
from pathlib import Path

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

pytestmark = [pytest.mark.performance, pytest.mark.django_db]

ROOT = Path(__file__).resolve().parents[2]
REPORT_PATH = ROOT / "var/test_artifacts/upd-2026-10/registration_list_statistics_load.json"
COUNT = int(os.environ.get("ASC_PERF_REGISTRATIONS", "6000"))
#: Generous local ceilings: a regression (a query per row, an unpaginated
#: list) exceeds them by far; normal runs stay well below.
MAX_QUERIES = 80
MAX_MEDIAN_SECONDS = 5.0

STATUS_MIX = (
    ("APPROVED", 33),
    ("UNDER_REVIEW", 20),
    ("SUBMITTED", 15),
    ("WITHDRAWN", 12),
    ("NOT_APPROVED", 8),
    ("ADDITIONAL_INFORMATION_REQUIRED", 7),
    ("DRAFT", 5),
)


def _seed():
    from apps.accounts.models import OperationalUser, OperationalUserStatus
    from apps.entry.tests import factories
    from apps.people.models import Person, PersonStatus
    from apps.people.tests.identity_fixtures import bulk_verified_identity_cases
    from apps.registrations.models import (
        Registration,
        RegistrationInternalStatus,
        RegistrationProfile,
        RegistrationSourceKind,
    )
    from apps.reviews.models import RegistrationDecision

    factories.seed_reference_values()
    event = factories.make_event(f"PRF{uuid.uuid4().hex[:5].upper()}")
    admin = OperationalUser.objects.create_user(
        email=f"perf.superadmin.{uuid.uuid4().hex[:6]}@example.test",
        password=uuid.uuid4().hex,
        status=OperationalUserStatus.ACTIVE,
        is_superuser=True,
    )
    statuses = [status for status, weight in STATUS_MIX for _ in range(weight)]
    now = timezone.now()
    people = Person.objects.bulk_create(
        Person(
            status=PersonStatus.ACTIVE,
            display_name=f"Synthetic Person {i:05d}",
            nationality_id="DZ" if i % 4 else "FR",
        )
        for i in range(COUNT)
    )
    registrations = Registration.objects.bulk_create(
        Registration(
            public_reference=f"PRF-{uuid.uuid4().hex[:10].upper()}",
            event_edition=event,
            person=person,
            source_kind=RegistrationSourceKind.OPEN,
            source_context_key=f"open:{uuid.uuid4().hex}",
            public_status=statuses[i % len(statuses)],
            internal_status=RegistrationInternalStatus.QUALIFICATION_COMPLETE,
            submitted_at=None
            if statuses[i % len(statuses)] == "DRAFT"
            else now - datetime.timedelta(minutes=i),
            withdrawn_at=now if statuses[i % len(statuses)] == "WITHDRAWN" else None,
        )
        for i, person in enumerate(people)
    )
    RegistrationProfile.objects.bulk_create(
        RegistrationProfile(
            registration=registration,
            submitted_given_names=f"Given{i % 900:03d}",
            submitted_family_name=f"Family{i:05d}",
            submitted_full_name=f"Given{i % 900:03d} Family{i:05d}",
            nationality_code_id="DZ" if i % 4 else "FR",
            country_of_residence_id="DZ",
        )
        for i, registration in enumerate(registrations)
    )
    bulk_verified_identity_cases(
        [r for r in registrations if r.public_status in ("APPROVED", "UNDER_REVIEW")]
    )
    RegistrationDecision.objects.bulk_create(
        RegistrationDecision(
            registration=registration,
            sequence=1,
            outcome=registration.public_status,
            internal_reason_code="ELIGIBLE",
            is_current=True,
            decided_by=admin,
            decided_at=now,
            event_edition_id=event.pk,
        )
        for registration in registrations
        if registration.public_status in ("APPROVED", "NOT_APPROVED")
    )
    return event, admin


def _measure(client, method, url, data=None) -> dict:
    timings, queries, status = [], None, None
    for _ in range(3):
        with CaptureQueriesContext(connection) as captured:
            start = time.perf_counter()
            response = getattr(client, method)(url, data or {})
            timings.append(time.perf_counter() - start)
        queries = len(captured.captured_queries)
        status = response.status_code
    return {
        "status": status,
        "queries": queries,
        "median_s": round(statistics.median(timings), 3),
        "max_s": round(max(timings), 3),
    }


def test_registration_list_and_statistics_at_size():
    from apps.people.tests.conftest import staff_client

    started = time.perf_counter()
    event, admin = _seed()
    seed_seconds = round(time.perf_counter() - started, 1)
    client = staff_client(admin)
    list_url = reverse("registrations:ops-intake-list")
    last_page = (COUNT + 49) // 50
    results = {
        "list_first_page": _measure(client, "get", list_url, {"event": event.pk}),
        "list_middle_page": _measure(
            client, "get", list_url, {"event": event.pk, "page": last_page // 2}
        ),
        "list_last_page": _measure(client, "get", list_url, {"event": event.pk, "page": last_page}),
        "list_filtered": _measure(
            client,
            "get",
            list_url,
            {"event": event.pk, "status": "UNDER_REVIEW", "nationality": "FR"},
        ),
        "list_name_search": _measure(
            client, "post", reverse("registrations:ops-search"), {"q": "Given12"}
        ),
        "statistics_page": _measure(
            client, "get", reverse("registrations:statistics"), {"event": event.pk}
        ),
        "statistics_xlsx_situation": _measure(
            client,
            "get",
            reverse(
                "registrations:statistics-export",
                kwargs={"report_kind": "situation", "file_format": "xlsx"},
            ),
            {"event": event.pk},
        ),
    }
    report = {
        "status": "MEASURED_ON_TEST_HOST",
        "registrations": COUNT,
        "seed_seconds": seed_seconds,
        "platform": platform.platform(),
        "python": platform.python_version(),
        "method": "Django test client as the superadmin; 3 runs per request; "
        "CaptureQueriesContext query count of the last run",
        "results": results,
    }
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    for name, result in results.items():
        assert result["status"] in (200, 302), (name, result)
        assert result["queries"] <= MAX_QUERIES, (name, result)
        assert result["median_s"] <= MAX_MEDIAN_SECONDS, (name, result)
