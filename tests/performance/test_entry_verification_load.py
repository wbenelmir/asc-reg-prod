"""Provisional online-verification load harness (Phase 3 Prompt 5, ADR-0022).

Target: NFR-PERF-003 -- p95 online Digital Entry Pass / identity-reference
validation within 1.5 s (`ENTRY_VERIFICATION_TARGET_P95_MS`).

What it does: seeds one synthetic event, enrolls several entry devices each
with its own named operator and live checkpoint session, then drives the
REAL HTTP endpoints of the live test server from one thread per device, in a
closed loop (each device sends its next request as soon as the previous
result arrives -- a harder load than a human operator produces). It
measures the client-observed latency of every verification request (POST
to the result page, i.e. the whole online path: session and device checks,
signature verification, database lookups, access evaluation, audit, sample,
limits, decision ticket, rendering) and compares p95 with the target. It
also reports the server-side `VerificationSample` percentiles.

Documented synthetic assumptions (INFRA-001 volumes are still an OPEN
decision, so these are provisional and must be replaced by the approved
capacity profile before the release load test, NFR-PERF-004):

* participants holding an active pass: `ASC_PERF_PARTICIPANTS` (default 300)
  plus `ASC_PERF_FILLER` (default 3000) additional approved registrations,
  so lookups do not run against a trivially small table;
* checkpoint devices in parallel: `ASC_PERF_DEVICES` (default 6), each
  sending `ASC_PERF_SCANS_PER_DEVICE` (default 40) requests;
* request mix: 85% valid QR, 10% invalid QR, 5% registration-reference
  lookup, randomly ordered with a fixed seed;
* abuse limits are raised for the run (a load test is not abuse); every
  other control -- permission checks, device scope, audit -- stays on;
* the database, application server, and client share one local machine,
  so absolute numbers are indicative, not a capacity statement.

The report is written to
`var/test_artifacts/phase3/prompt5-perf/entry_verification_load.json`.
All data is synthetic.
"""

from __future__ import annotations

import http.cookiejar
import json
import math
import os
import platform
import random
import re
import statistics
import threading
import time
import urllib.parse
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

pytestmark = [pytest.mark.performance, pytest.mark.django_db(transaction=True)]

ROOT = Path(__file__).resolve().parents[2]
REPORT_PATH = ROOT / "var/test_artifacts/phase3/prompt5-perf/entry_verification_load.json"

PARTICIPANTS = int(os.environ.get("ASC_PERF_PARTICIPANTS", "300"))
FILLER = int(os.environ.get("ASC_PERF_FILLER", "3000"))
DEVICES = int(os.environ.get("ASC_PERF_DEVICES", "6"))
SCANS_PER_DEVICE = int(os.environ.get("ASC_PERF_SCANS_PER_DEVICE", "40"))
MIX = (("QR_VALID", 0.85), ("QR_INVALID", 0.10), ("REFERENCE", 0.05))
SEED = 20260923

_CSRF = re.compile(r'name="csrfmiddlewaretoken" value="([^"]+)"')


def _nearest_rank(values, fraction):
    ordered = sorted(values)
    return ordered[max(1, math.ceil(fraction * len(ordered))) - 1]


def _percentiles(values) -> dict:
    return {
        "count": len(values),
        "p50_ms": round(_nearest_rank(values, 0.50), 1),
        "p95_ms": round(_nearest_rank(values, 0.95), 1),
        "p99_ms": round(_nearest_rank(values, 0.99), 1),
        "max_ms": round(max(values), 1),
        "mean_ms": round(statistics.fmean(values), 1),
    }


def _seed_world():
    from django.utils import timezone

    from apps.core.crypto.signing import (
        InMemorySigningKeyProvider,
        set_signing_key_provider_for_testing,
    )
    from apps.entry.tests import factories
    from apps.people.models import Person, PersonStatus
    from apps.registrations.models import (
        Registration,
        RegistrationInternalStatus,
        RegistrationPublicStatus,
        RegistrationSourceKind,
    )

    provider = InMemorySigningKeyProvider(key_ids=("v1",), current="v1")
    set_signing_key_provider_for_testing(provider)
    factories.seed_reference_values()
    event = factories.make_event("PERF26")
    layout = factories.VenueLayout(event)
    setup = factories.AccreditationSetup(event, layout)
    staff = factories.make_user("perf.staff@example.test")
    factories.publish_key(provider=provider, actor=staff)

    tokens, references = [], []
    for index in range(PARTICIPANTS):
        person = factories.make_person(f"Synthetic Load Participant {index:05d}")
        registration = factories.make_registration(event=event, person=person)
        factories.assign(registration=registration, setup=setup, actor=staff)
        credential = factories.issue_active_pass(registration=registration, actor=staff)
        tokens.append(factories.token_for(credential))
        references.append(registration.public_reference)

    people = Person.objects.bulk_create(
        Person(
            status=PersonStatus.ACTIVE,
            display_name=f"Synthetic Filler {i:05d}",
            nationality_id="DZ",
        )
        for i in range(FILLER)
    )
    fillers = Registration.objects.bulk_create(
        Registration(
            public_reference=f"FILL-{uuid.uuid4().hex[:10].upper()}",
            event_edition=event,
            person=person,
            source_kind=RegistrationSourceKind.OPEN,
            source_context_key=f"open:{uuid.uuid4().hex}",
            public_status=RegistrationPublicStatus.APPROVED,
            internal_status=RegistrationInternalStatus.QUALIFICATION_COMPLETE,
            submitted_at=timezone.now(),
        )
        for person in people
    )
    # Owner decision IDV-Q1: in production every approved context has a
    # verified identity, so the fillers carry one too and the eligibility
    # join is measured at realistic size.
    from apps.people.tests.identity_fixtures import bulk_verified_identity_cases

    bulk_verified_identity_cases(fillers)

    admin = factories.make_user(
        "perf.device.admin@example.test", group_name="Entry Device Administrators", event=event
    )
    stations = []
    for index in range(DEVICES):
        operator = factories.make_user(
            f"perf.operator{index}@example.test",
            group_name="Entry Operators",
            event=event,
            gate=layout.gate_a,
        )
        _device, secret = factories.enroll_device(event=event, layout=layout, admin=admin)
        stations.append({"operator": operator, "secret": secret})
    return {
        "event": event,
        "layout": layout,
        "tokens": tokens,
        "references": references,
        "stations": stations,
        "reset": lambda: set_signing_key_provider_for_testing(None),
    }


class _Station:
    """One checkpoint device: its own cookie jar, operator session, CSRF."""

    def __init__(self, base_url: str, station: dict, zone_id: str):
        from django.conf import settings
        from django.contrib.auth import BACKEND_SESSION_KEY, HASH_SESSION_KEY, SESSION_KEY
        from django.contrib.sessions.backends.db import SessionStore
        from django.utils import timezone

        from apps.accounts import session_expiry

        # 127.0.0.1, not "localhost": http.cookiejar never matches cookies for
        # a dotless "localhost" host, and on Windows "localhost" first tries
        # IPv6 and adds a fixed connection delay to every request.
        self.base = base_url.replace("://localhost", "://127.0.0.1")
        user = station["operator"]
        now = timezone.now().isoformat()
        session = SessionStore()
        session[SESSION_KEY] = str(user.pk)
        session[BACKEND_SESSION_KEY] = settings.AUTHENTICATION_BACKENDS[0]
        session[HASH_SESSION_KEY] = user.get_session_auth_hash()
        session[session_expiry.OPERATIONAL_ESTABLISHED_AT_KEY] = now
        session[session_expiry.OPERATIONAL_LAST_ACTIVITY_AT_KEY] = now
        session.create()
        host = urllib.parse.urlparse(self.base).hostname
        self.jar = http.cookiejar.CookieJar()
        for name, value, path in (
            (settings.SESSION_COOKIE_NAME, session.session_key, "/"),
            (settings.ENTRY_DEVICE_COOKIE_NAME, station["secret"], "/entry/"),
        ):
            self.jar.set_cookie(
                http.cookiejar.Cookie(
                    0, name, value, None, False, host, False, False, path, True,
                    False, None, False, None, None, {},
                )
            )  # fmt: skip
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
        page = self._get("/entry/")
        self.csrf = _CSRF.search(page).group(1)
        self._post("/entry/", {"action": "start", "zone_id": zone_id})
        verify_page = self._get("/entry/verify/")
        if "data-entry-scan-form" not in verify_page:
            raise AssertionError("Load station did not reach the checkpoint verify screen.")
        self.csrf = _CSRF.search(verify_page).group(1)

    def _get(self, path: str) -> str:
        with self.opener.open(self.base + path, timeout=30) as response:
            return response.read().decode()

    def _post(self, path: str, data: dict) -> tuple[int, str]:
        body = urllib.parse.urlencode({"csrfmiddlewaretoken": self.csrf, **data}).encode()
        # The local live test server's http:// URL, never user input.
        request = urllib.request.Request(self.base + path, data=body, method="POST")  # noqa: S310
        with self.opener.open(request, timeout=30) as response:
            return response.status, response.read().decode()

    def verify(self, kind: str, value: str) -> tuple[float, int, str]:
        path, field = ("/entry/lookup/reference/", "reference") if kind == "REFERENCE" else (
            "/entry/verify/qr/",
            "token",
        )  # fmt: skip
        started = time.perf_counter()
        status, body = self._post(path, {field: value})
        elapsed = (time.perf_counter() - started) * 1000
        match = re.search(r'data-result="([A-Z_]+)"', body)
        return elapsed, status, match.group(1) if match else ""


def test_online_verification_p95_meets_the_provisional_target(live_server, settings):
    from apps.entry.models import VerificationSample
    from apps.entry.observability import verification_summary

    settings.ENTRY_LOOKUP_MAX_PER_WINDOW = 10_000
    settings.ENTRY_LOOKUP_ANOMALY_THRESHOLD = 10_000
    settings.ENTRY_INVALID_SCAN_MAX_PER_WINDOW = 10_000
    settings.ENTRY_INVALID_SCAN_ANOMALY_THRESHOLD = 10_000
    seed_started = time.perf_counter()
    world = _seed_world()
    seed_seconds = time.perf_counter() - seed_started
    try:
        zone_id = str(world["layout"].main.pk)
        stations = [_Station(live_server.url, s, zone_id) for s in world["stations"]]
        rng = random.Random(SEED)  # noqa: S311 - reproducible workload mix, not security
        kinds = [k for k, _w in MIX]
        weights = [w for _k, w in MIX]
        plans = [
            [rng.choices(kinds, weights)[0] for _ in range(SCANS_PER_DEVICE)] for _ in stations
        ]
        results: list[tuple[str, float, int, str]] = []
        lock = threading.Lock()

        def run(index: int) -> None:
            local = random.Random(SEED + index)  # noqa: S311 - reproducible workload
            for kind in plans[index]:
                if kind == "QR_VALID":
                    value = local.choice(world["tokens"])
                elif kind == "QR_INVALID":
                    value = f"synthetic-invalid-{local.randrange(10**9)}"
                else:
                    value = local.choice(world["references"])
                elapsed, status, result = stations[index].verify(kind, value)
                with lock:
                    results.append((kind, elapsed, status, result))

        load_started = time.perf_counter()
        with ThreadPoolExecutor(max_workers=len(stations)) as pool:
            list(pool.map(run, range(len(stations))))
        wall = time.perf_counter() - load_started

        latencies = [elapsed for _k, elapsed, _s, _r in results]
        by_kind = {
            kind: _percentiles([e for k, e, _s, _r in results if k == kind])
            for kind in kinds
            if any(k == kind for k, *_ in results)
        }
        statuses = {
            str(s): sum(1 for *_x, st, _r in results if st == s) for s in {r[2] for r in results}
        }
        outcomes = {}
        for kind, _e, _s, result in results:
            outcomes.setdefault(kind, {}).setdefault(result, 0)
            outcomes[kind][result] += 1
        from django.db import connection

        with connection.cursor() as cursor:
            cursor.execute("SHOW server_version")
            pg_version = cursor.fetchone()[0]
        server = verification_summary(event_edition=world["event"], since=world["event"].starts_at)
        report = {
            "target_p95_ms": settings.ENTRY_VERIFICATION_TARGET_P95_MS,
            "client_observed": _percentiles(latencies),
            "client_observed_by_kind": by_kind,
            "server_side_samples": {
                "count": server["total"],
                "p50_ms": server["p50_ms"],
                "p95_ms": server["p95_ms"],
                "p99_ms": server["p99_ms"],
                "max_ms": server["max_ms"],
            },
            "throughput_requests_per_second": round(len(results) / wall, 2),
            "wall_seconds": round(wall, 2),
            "seed_seconds": round(seed_seconds, 2),
            "http_statuses": statuses,
            "outcomes_by_kind": outcomes,
            "assumptions": {
                "participants_with_active_pass": PARTICIPANTS,
                "additional_approved_registrations": FILLER,
                "parallel_devices": DEVICES,
                "requests_per_device": SCANS_PER_DEVICE,
                "mix": dict(MIX),
                "load_model": "closed loop, one thread per device, no think time",
                "abuse_limits": "raised for the run; permission, scope, audit unchanged",
                "topology": "database, application server and client on one machine",
            },
            "environment": {
                "python": platform.python_version(),
                "platform": platform.platform(),
                "cpu_count": os.cpu_count(),
                "postgresql": pg_version,
                "server": "Django live test server (threaded WSGI), DEBUG off",
            },
            "sample_rows_recorded": VerificationSample.objects.filter(
                event_edition=world["event"]
            ).count(),
        }
        REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
        REPORT_PATH.write_text(json.dumps(report, indent=2), encoding="utf-8")

        assert len(results) == DEVICES * SCANS_PER_DEVICE
        assert statuses == {"200": len(results)}, statuses
        # Correctness under load: valid passes verify, invalid ones never do.
        assert set(outcomes.get("QR_VALID", {})) <= {"ALLOWED", "ALLOWED_WITH_ADVISORY"}
        assert set(outcomes.get("QR_INVALID", {})) == {"DENIED"}
        assert report["sample_rows_recorded"] == len(results)
        assert report["client_observed"]["p95_ms"] <= settings.ENTRY_VERIFICATION_TARGET_P95_MS, (
            report
        )
    finally:
        world["reset"]()
