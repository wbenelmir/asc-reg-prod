"""Thread, context and cleanup guarantees without a PostgreSQL substitute."""

import asyncio
from contextvars import ContextVar
from threading import get_ident

import pytest
from django.core.exceptions import SynchronousOnlyOperation
from django.utils.asyncio import async_unsafe

from tests.browser.database import database_call, database_fixture, database_sync


@async_unsafe("The real Django async guard must remain enabled.")
def guarded_probe(value):
    return value, get_ident()


def test_database_callback_runs_off_loop_with_context_and_guard_intact(monkeypatch):
    monkeypatch.delenv("DJANGO_ALLOW_ASYNC_UNSAFE", raising=False)
    context_value = ContextVar("browser_boundary_probe", default="missing")
    context_value.set("synthetic-context")

    async def scenario():
        loop = asyncio.get_running_loop()
        with pytest.raises(SynchronousOnlyOperation):
            guarded_probe("direct")
        value, worker = database_call(lambda: guarded_probe(context_value.get()))
        assert value == "synthetic-context"
        assert worker != get_ident()
        assert asyncio.get_running_loop() is loop
        with pytest.raises(SynchronousOnlyOperation):
            guarded_probe("still-protected")

    asyncio.run(scenario())


@pytest.mark.parametrize("fail", [False, True])
def test_worker_connection_cleanup_runs_on_its_own_thread(monkeypatch, fail):
    observed = []
    monkeypatch.setattr(
        "tests.browser.database.connections.close_all", lambda: observed.append(get_ident())
    )
    failure = ValueError("Synthetic callback failure")

    @database_sync
    def operation(value):
        observed.append(get_ident())
        if fail:
            raise failure
        return value

    if fail:
        with pytest.raises(ValueError) as caught:
            operation(42)
        assert caught.value is failure
    else:
        assert operation(42) == 42
    assert len(observed) == 2
    assert observed[0] == observed[1] != get_ident()


def test_nested_data_helpers_keep_one_connection_owner(monkeypatch):
    cleanup = []
    monkeypatch.setattr(
        "tests.browser.database.connections.close_all", lambda: cleanup.append(get_ident())
    )
    outer, inner = database_call(lambda: (get_ident(), database_call(get_ident)))
    assert outer == inner != get_ident()
    assert cleanup == [outer]


@pytest.mark.parametrize("failure_at", [None, "setup", "teardown"])
def test_yield_fixture_keeps_setup_teardown_and_cleanup_on_one_thread(monkeypatch, failure_at):
    events = []
    monkeypatch.setattr(
        "tests.browser.database.connections.close_all",
        lambda: events.append(("close", get_ident())),
    )

    @database_fixture
    def fixture():
        events.append(("setup", get_ident()))
        if failure_at == "setup":
            raise ValueError("Synthetic setup failure")
        yield 42
        events.append(("teardown", get_ident()))
        if failure_at == "teardown":
            raise ValueError("Synthetic teardown failure")

    generator = fixture()
    if failure_at == "setup":
        with pytest.raises(ValueError, match="setup"):
            next(generator)
    else:
        assert next(generator) == 42
        if failure_at == "teardown":
            with pytest.raises(ValueError, match="teardown"):
                next(generator)
        else:
            with pytest.raises(StopIteration):
                next(generator)
    assert events[-1][0] == "close"
    assert {thread for _, thread in events} == {events[0][1]}
    assert events[0][1] != get_ident()


def test_lazy_querysets_must_be_evaluated_inside_the_database_callback():
    from apps.core.models import Country

    with pytest.raises(TypeError, match="Evaluate querysets"):
        database_call(lambda: Country.objects.none())
