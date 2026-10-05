"""Real sync-driver lifecycle and PostgreSQL isolation regression coverage."""

import asyncio

import pytest
from django.core.exceptions import SynchronousOnlyOperation
from django.utils.asyncio import async_unsafe

from tests.browser.database import database_call


@pytest.fixture
def synchronous_lifecycle():
    with pytest.raises(RuntimeError, match="no running event loop"):
        asyncio.get_running_loop()
    yield
    with pytest.raises(RuntimeError, match="no running event loop"):
        asyncio.get_running_loop()


@pytest.fixture
def playwright(synchronous_lifecycle, playwright):
    yield playwright


@pytest.mark.parametrize("iteration", [1, 2])
def test_driver_lifecycle_keeps_django_guard_enabled(playwright, iteration):
    """The real driver needs no browser binary or PostgreSQL for this proof."""

    @async_unsafe("Synthetic operation protected by Django")
    def guarded_operation():
        return iteration

    asyncio.get_running_loop()
    with pytest.raises(SynchronousOnlyOperation):
        guarded_operation()
    assert database_call(guarded_operation) == iteration


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("iteration", [1, 2])
def test_browser_database_work_commits_and_flushes_between_tests(live_server, page, iteration):
    from apps.core.models import Country

    # Reusing this primary key across both cases proves actual Django flush
    # still works after Playwright stops; no database mocking or bypass.
    assert not database_call(lambda: Country.objects.filter(pk="ZZ").exists())
    with pytest.raises(SynchronousOnlyOperation):
        Country.objects.count()
    database_call(lambda: Country.objects.create(code="ZZ", name="Synthetic Boundary Country"))
    assert database_call(lambda: Country.objects.get(pk="ZZ").name) == "Synthetic Boundary Country"
    response = page.goto(f"{live_server.url}/healthz")
    assert response.status == 200
    assert iteration in (1, 2)
