"""Static deployment configuration validation (accepted plan §5.6 A).

Every assertion here inspects configuration SHAPE only. No database
connection, no project table, nothing that could block `manage.py migrate`,
and no real S3/Redis/email-provider contact of any kind.
"""

from __future__ import annotations

import dataclasses

import pytest

from config.settings.validation import (
    DeploymentConfigurationError,
    DeploymentSettingsSnapshot,
    validate_deployment_configuration,
)


def _valid_snapshot() -> DeploymentSettingsSnapshot:
    return DeploymentSettingsSnapshot(
        settings_module_name="config.settings.staging",
        database_user="runtime_role",
        database_migration_user="migration_owner_role",
        database_password="synthetic-runtime-password",  # secret-scan: allow
        database_process_role="runtime",
        database_migration_password_present=False,
        redis_url="rediss://redis.example.invalid:6379/0",
        celery_broker_url="rediss://redis.example.invalid:6379/1",
        celery_task_always_eager=False,
        mfa_backend="synthetic.mfa.Backend",
        malware_scanner_backend="apps.documents.scanning.ClamdScanner",
        local_malware_scanner_backend="apps.documents.scanning.DeterministicStubScanner",
        clamd_scanner_backend="apps.documents.scanning.ClamdScanner",
        clamd_socket_path="",
        clamd_host="clamd.internal.example.invalid",
        clamd_port=3310,
        clamd_connect_timeout_seconds=5,
        clamd_scan_timeout_seconds=60,
        clamd_max_stream_bytes=25 * 1024 * 1024,
        largest_upload_bytes=8 * 1024 * 1024,
        email_backend="django.core.mail.backends.smtp.EmailBackend",
        local_otp_sink_backend="django.core.mail.backends.filebased.EmailBackend",
        smtp_email_backend="django.core.mail.backends.smtp.EmailBackend",
        email_host="smtp.example.invalid",
        email_port=587,
        email_host_user="synthetic-smtp-user",
        email_host_password="synthetic-smtp-password",  # secret-scan: allow
        email_use_tls=True,
        email_use_ssl=False,
        email_timeout=10,
        default_from_email="ASC 2026 <no-reply@example.invalid>",
        public_base_url="https://registration.example.invalid",
        s3_bucket_name="synthetic-bucket",
        s3_endpoint_url="https://s3.example.invalid",
        s3_access_key_id="synthetic-access-key",  # secret-scan: allow
        s3_secret_access_key="synthetic-secret-key",  # secret-scan: allow
        s3_region_name="us-east-1",
        trusted_proxy_forwarding_enabled=True,
        trusted_proxy_cidrs=["10.0.0.0/8"],
        rate_limit_hmac_active_versions=[1, 2],
        rate_limit_hmac_write_version=2,
        rate_limit_key_overlap_seconds=7200,
        rate_limit_minimum_required_overlap_seconds=3600,
        rate_limit_hmac_keys_by_version={1: "synthetic-key-v1", 2: "synthetic-key-v2"},
        otp_generator_backend="apps.accounts.otp.CsprngOtpGenerator",
        deterministic_otp_generator_backend="apps.accounts.otp.DeterministicTestOtpGenerator",
        identity_encryption_active_versions=[1, 2],
        identity_encryption_write_version=2,
        identity_encryption_keys_by_version={
            1: "synthetic-enc-key-v1",
            2: "synthetic-enc-key-v2",
        },  # secret-scan: allow
        identity_blind_index_hmac_active_versions=[1, 2],
        identity_blind_index_hmac_write_version=2,
        identity_blind_index_hmac_keys_by_version={  # secret-scan: allow
            1: "synthetic-bidx-key-v1",
            2: "synthetic-bidx-key-v2",
        },
    )


def test_valid_configuration_does_not_raise() -> None:
    validate_deployment_configuration(_valid_snapshot())  # must not raise


def _broken(**overrides) -> DeploymentSettingsSnapshot:
    return dataclasses.replace(_valid_snapshot(), **overrides)


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"database_migration_user": None}, id="missing-migration-owner"),
        pytest.param(
            {"database_migration_user": "runtime_role"}, id="migration-owner-equals-runtime"
        ),
        pytest.param({"database_password": None}, id="missing-database-password"),
        pytest.param(
            {"database_migration_password_present": True},
            id="migration-owner-password-present-in-a-runtime-process",
        ),
        pytest.param(
            {"database_process_role": "migration", "database_migration_password": None},
            id="missing-database-migration-password-in-the-migration-process",
        ),
        pytest.param({"database_process_role": "admin"}, id="unknown-database-process-role"),
        pytest.param({"redis_url": None}, id="missing-redis-url"),
        pytest.param({"celery_broker_url": None}, id="missing-celery-broker"),
        pytest.param({"celery_broker_url": "memory://"}, id="celery-broker-is-memory-transport"),
        pytest.param(
            {"celery_broker_url": "redis://redis.example.invalid:6379/1"},
            id="celery-broker-without-tls",
        ),
        pytest.param(
            {"celery_broker_url": "rediss://redis.example.invalid:6379/1?ssl_cert_reqs=none"},
            id="celery-broker-with-weakened-tls",
        ),
        pytest.param(
            {"celery_broker_url": "amqp://broker.example.invalid//"}, id="non-redis-broker"
        ),
        pytest.param({"celery_task_always_eager": True}, id="eager-mode-enabled"),
        # P4-4-C3: a missing MFA_BACKEND is no longer a violation (owner decision
        # MFA-01 revised, amendment A-11); see test_p4_4_c3_static_validation.py.
        pytest.param({"malware_scanner_backend": None}, id="missing-scanner-backend"),
        pytest.param(
            {
                "malware_scanner_backend": "apps.documents.scanning.DeterministicStubScanner",
            },
            id="scanner-backend-is-local-stub",
        ),
        pytest.param(
            {"malware_scanner_backend": "apps.documents.scanning.RejectingStubScanner"},
            id="scanner-backend-is-another-stub",
        ),
        pytest.param(
            {"malware_scanner_backend": "synthetic.scanner.Backend"}, id="scanner-not-clamd"
        ),
        pytest.param({"clamd_host": ""}, id="clamd-without-endpoint"),
        pytest.param(
            {"clamd_socket_path": "/run/clamav/clamd.ctl"}, id="clamd-with-both-endpoints"
        ),
        pytest.param({"clamd_port": 0}, id="clamd-port-out-of-range"),
        pytest.param({"clamd_connect_timeout_seconds": 0}, id="clamd-connect-timeout-zero"),
        pytest.param({"clamd_scan_timeout_seconds": 601}, id="clamd-scan-timeout-too-long"),
        pytest.param(
            {"clamd_max_stream_bytes": 1024 * 1024}, id="clamd-limit-below-largest-upload"
        ),
        pytest.param({"clamd_max_stream_bytes": 2**32}, id="clamd-limit-above-protocol"),
        pytest.param({"email_backend": None}, id="missing-email-backend"),
        pytest.param(
            {"email_backend": "django.core.mail.backends.filebased.EmailBackend"},
            id="email-backend-is-local-sink",
        ),
        pytest.param(
            {"email_backend": "django.core.mail.backends.console.EmailBackend"},
            id="email-backend-not-smtp",
        ),
        pytest.param({"email_host": ""}, id="missing-smtp-host"),
        pytest.param({"email_port": 0}, id="smtp-port-out-of-range"),
        pytest.param({"email_use_ssl": True}, id="smtp-tls-and-ssl-together"),
        pytest.param(
            {"email_use_tls": False, "email_host_user": "", "email_host_password": ""},
            id="unencrypted-smtp-to-a-remote-host",
        ),
        pytest.param({"email_host_password": ""}, id="smtp-user-without-password"),
        pytest.param(
            {"email_host": "127.0.0.1", "email_use_tls": False},
            id="smtp-credentials-without-tls-even-on-loopback",
        ),
        pytest.param({"email_timeout": None}, id="missing-smtp-timeout"),
        pytest.param({"email_timeout": 121}, id="smtp-timeout-too-long"),
        pytest.param({"default_from_email": ""}, id="missing-sender"),
        pytest.param({"default_from_email": "webmaster@localhost"}, id="localhost-sender"),
        pytest.param({"default_from_email": "not an address"}, id="malformed-sender"),
        pytest.param({"public_base_url": None}, id="missing-public-base-url"),
        pytest.param(
            {"public_base_url": "http://registration.example.invalid"},
            id="non-https-public-base-url",
        ),
        pytest.param(
            {"public_base_url": "https://user@example.invalid/claim?next=elsewhere"},
            id="unsafe-public-base-url-components",
        ),
        pytest.param({"s3_bucket_name": None}, id="missing-s3-bucket-name"),
        pytest.param({"s3_endpoint_url": None}, id="missing-s3-endpoint-url"),
        pytest.param({"s3_access_key_id": None}, id="missing-s3-access-key-id"),
        pytest.param({"s3_secret_access_key": None}, id="missing-s3-secret-access-key"),
        pytest.param({"s3_region_name": None}, id="missing-s3-region-name"),
        pytest.param(
            {"trusted_proxy_forwarding_enabled": True, "trusted_proxy_cidrs": []},
            id="forwarding-enabled-without-trusted-proxy-config",
        ),
        pytest.param({"rate_limit_hmac_write_version": None}, id="missing-write-version"),
        pytest.param(
            {"rate_limit_hmac_write_version": 99, "rate_limit_hmac_active_versions": [1, 2]},
            id="write-version-not-in-active-set",
        ),
        pytest.param({"rate_limit_hmac_active_versions": []}, id="no-active-versions"),
        pytest.param(
            {"rate_limit_hmac_active_versions": [1, -1], "rate_limit_hmac_write_version": 1},
            id="non-positive-version",
        ),
        pytest.param(
            {"rate_limit_hmac_active_versions": [1, 1], "rate_limit_hmac_write_version": 1},
            id="duplicate-versions",
        ),
        pytest.param(
            {"rate_limit_hmac_keys_by_version": {1: "synthetic-key-v1", 2: None}},
            id="missing-per-version-hmac-key",
        ),
        pytest.param(
            {
                "rate_limit_key_overlap_seconds": 10,
                "rate_limit_minimum_required_overlap_seconds": 3600,
            },
            id="overlap-too-short",
        ),
        pytest.param(
            {"otp_generator_backend": "apps.accounts.otp.DeterministicTestOtpGenerator"},
            id="deterministic-generator-outside-test-settings",
        ),
    ],
)
def test_each_violation_is_rejected(overrides: dict) -> None:
    with pytest.raises(DeploymentConfigurationError) as excinfo:
        validate_deployment_configuration(_broken(**overrides))
    # The error must never echo a secret value -- only variable names and
    # the nature of the problem. None of our synthetic fixture values look
    # like a real secret, but the message shape itself is asserted here.
    message = str(excinfo.value)
    assert "does not connect to PostgreSQL" in message


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param(
            {
                "email_host": "127.0.0.1",
                "email_use_tls": False,
                "email_host_user": "",
                "email_host_password": "",
            },
            id="unauthenticated-plain-smtp-to-a-loopback-relay",
        ),
        pytest.param(
            {"email_use_tls": False, "email_use_ssl": True, "email_port": 465},
            id="implicit-tls-smtp",
        ),
        pytest.param(
            {"clamd_socket_path": "/run/clamav/clamd.ctl", "clamd_host": ""},
            id="clamd-over-a-unix-socket",
        ),
        pytest.param(
            {"celery_broker_url": "rediss://redis.example.invalid:6379/1?ssl_ca_certs=/etc/ca.pem"},
            id="broker-with-a-private-ca",
        ),
        pytest.param(
            {
                "database_process_role": "migration",
                "database_password": None,
                "database_migration_password": "synthetic-migration-password",  # secret-scan: allow
                "database_migration_password_present": True,
            },
            id="the-migration-process",
        ),
    ],
)
def test_supported_variants_are_accepted(overrides: dict) -> None:
    validate_deployment_configuration(_broken(**overrides))  # must not raise


def test_multiple_violations_are_aggregated_into_one_error() -> None:
    # P4-4-C3: the third violation was a missing MFA_BACKEND, no longer one
    # (MFA-01 revised); a missing scanner backend takes its place.
    snapshot = _broken(redis_url=None, celery_broker_url=None, malware_scanner_backend=None)
    with pytest.raises(DeploymentConfigurationError) as excinfo:
        validate_deployment_configuration(snapshot)
    message = str(excinfo.value)
    assert "REDIS_URL" in message
    assert "CELERY_BROKER_URL" in message
    assert "MALWARE_SCANNER_BACKEND" in message


def test_missing_per_version_hmac_key_names_the_exact_variable() -> None:
    snapshot = _broken(rate_limit_hmac_keys_by_version={1: "synthetic-key-v1", 2: None})
    with pytest.raises(DeploymentConfigurationError) as excinfo:
        validate_deployment_configuration(snapshot)
    assert "RATE_LIMIT_HMAC_KEY_V2" in str(excinfo.value)


def test_no_configured_value_ever_appears_in_a_validation_error() -> None:
    """Every synthetic value in the valid snapshot must never leak into a
    validation error, even when a DIFFERENT field is what triggers one."""
    snapshot = _broken(redis_url=None)
    with pytest.raises(DeploymentConfigurationError) as excinfo:
        validate_deployment_configuration(snapshot)
    message = str(excinfo.value)
    for secret_value in (
        "synthetic-runtime-password",
        "synthetic-migration-password",
        "synthetic-smtp-password",
        "synthetic-smtp-user",
        "clamd.internal.example.invalid",
        "synthetic-access-key",
        "synthetic-secret-key",
        "synthetic-key-v1",
        "synthetic-key-v2",
    ):
        assert secret_value not in message
