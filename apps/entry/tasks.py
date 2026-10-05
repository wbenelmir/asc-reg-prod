"""Idempotent, retry-safe Celery tasks for apps.entry (Phase 3 Prompt 4).

`expire_entry_access` persists the expiry of lapsed entry devices and of
lapsed temporary external-security accounts. Neither sweep is load-bearing
for security: an expired device already fails `authenticate_device`, and an
expired account already fails `OperationalUser.is_active`, on the very next
request. The sweep makes expiry durable, ends open sessions, and writes the
audit evidence. It is safe to run repeatedly and concurrently (every row is
re-checked under a row lock). As with every task in this project, it is
exercised locally in eager mode only (ADR-0009); no beat schedule is
configured here -- the `expire_entry_access` management command is the
supported local/manual trigger.
"""

from __future__ import annotations

from celery import shared_task


def run_entry_expiry(*, now=None) -> dict[str, int]:
    from apps.accounts.services import expire_lapsed_temporary_accounts
    from apps.entry.services.devices import expire_lapsed_devices

    return {
        "devices": expire_lapsed_devices(now=now),
        "accounts": expire_lapsed_temporary_accounts(now=now),
    }


@shared_task(name="entry.expire_entry_access")
def expire_entry_access_task() -> dict[str, int]:
    return run_entry_expiry()


@shared_task(name="entry.prune_verification_samples")
def prune_verification_samples_task() -> int:
    """Delete telemetry samples past `ENTRY_METRICS_RETENTION_DAYS` (Prompt 5).

    Idempotent. Samples hold no participant data; pruning is data
    minimization, not evidence handling (audit rows are never touched).
    """
    from apps.entry.observability import prune_verification_samples

    return prune_verification_samples()


@shared_task(name="entry.build_offline_package")
def build_offline_package_task(build_id: str) -> int | None:
    """Run ONE requested package build (Phase 4 Prompt 2, independent re-review
    correction B). Idempotent and safe under redelivery or concurrent
    workers: `run_package_build` claims the build with a lease, commits only
    while it still holds the lease, and re-validates the device under its
    row lock. A failure is recorded on the build (the lease is released or
    the build fails after `ENTRY_OFFLINE_BUILD_MAX_ATTEMPTS`) and the next
    device request re-dispatches it; nothing propagates into the request
    that enqueued it. Returns the package version, or None."""
    from apps.entry.services.offline_packages import run_package_build

    try:
        package = run_package_build(build_id)
    except Exception:  # noqa: BLE001 - recorded on the build row; see docstring
        return None
    return package.package_version if package is not None else None


@shared_task(name="entry.cleanup_offline_packages")
def cleanup_offline_packages_task() -> dict[str, int]:
    """Expire lapsed packages, withdraw closed events, purge retained
    ciphertext past `ENTRY_OFFLINE_CIPHERTEXT_RETENTION_SECONDS`, prune
    expired nonces (Phase 4 Prompt 2). Package data only: there is no
    device evidence on the server to delete, and none is deleted."""
    from apps.entry.services.offline_packages import cleanup_offline_packages

    return cleanup_offline_packages()
