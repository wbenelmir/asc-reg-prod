"""Log redaction of NIN/passport/OTP-shaped secrets, tokens, and connection URLs.

Accepted plan, Prompt 2 CP 2g.
"""

from __future__ import annotations

import logging

import pytest

from apps.core.redaction import (
    MASK,
    RedactingLogFilter,
    extra_fields,
    redact,
    sanitize_persistent_payload,
)


def test_redacts_configured_secret_env_var_value(monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_PASSWORD", "hunter2-super-secret")
    message = "connection attempt using password hunter2-super-secret failed"
    assert "hunter2-super-secret" not in redact(message)
    assert MASK in redact(message)


def test_redacts_dynamically_named_v2_rate_limit_hmac_key(monkeypatch) -> None:
    # Only V1 is a hardcoded name anywhere in this project; V2+ must be
    # covered purely by the dynamic RATE_LIMIT_HMAC_KEY_V<n> pattern.
    monkeypatch.setenv("RATE_LIMIT_HMAC_KEY_V2", "rotated-v2-secret-value")
    message = "using key rotated-v2-secret-value for verification"
    result = redact(message)
    assert "rotated-v2-secret-value" not in result
    assert MASK in result


def test_redacts_a_v37_rate_limit_hmac_key_with_no_code_change(monkeypatch) -> None:
    monkeypatch.setenv("RATE_LIMIT_HMAC_KEY_V37", "far-future-version-secret")
    result = redact("rotating to far-future-version-secret")
    assert "far-future-version-secret" not in result


def test_ignores_a_similarly_named_but_non_matching_variable(monkeypatch) -> None:
    # RATE_LIMIT_HMAC_KEY_VX is not a valid version suffix (not digits) --
    # confirms the pattern is anchored and specific, not a loose substring.
    monkeypatch.setenv("RATE_LIMIT_HMAC_KEY_VX", "should-not-be-treated-as-a-version-key")
    # This value is not in any redaction list, so it is correctly left
    # alone -- proving the pattern only matches the documented shape.
    result = redact("plain text with should-not-be-treated-as-a-version-key inside")
    assert "should-not-be-treated-as-a-version-key" in result


def test_redacts_postgres_connection_url_password() -> None:
    message = (
        "using dsn postgresql://asc2026_app:s3cr3t@localhost:5433/asc2026_dev"  # secret-scan: allow
    )
    result = redact(message)
    assert "s3cr3t" not in result
    assert "asc2026_app" in result  # username is not a secret; only the password is masked
    assert MASK in result


def test_redacts_redis_url_password() -> None:
    message = "broker at rediss://:brokerpass@redis.example.invalid:6379/0"  # secret-scan: allow
    result = redact(message)
    assert "brokerpass" not in result
    assert MASK in result


def test_redacts_generic_key_value_secret_patterns() -> None:
    message = 'config dump: password="hunter2", token=abc123'  # secret-scan: allow
    result = redact(message)
    assert "hunter2" not in result
    assert "abc123" not in result


def test_redacts_bearer_token() -> None:
    message = "Authorization: Bearer eyFakeJwt.Payload.Signature"  # secret-scan: allow
    result = redact(message)
    assert "eyFakeJwt.Payload.Signature" not in result


def test_leaves_ordinary_messages_unchanged() -> None:
    message = "registration submitted for event ASC2026"
    assert redact(message) == message


def test_logging_filter_mutates_the_record_message() -> None:
    log_filter = RedactingLogFilter()
    record = logging.LogRecord(
        name="asc2026",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="password=hunter2-super-secret",
        args=(),
        exc_info=None,
    )

    keep = log_filter.filter(record)

    assert keep is True
    assert "hunter2-super-secret" not in record.getMessage()


def test_logging_filter_redacts_a_secret_passed_as_a_positional_arg(monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_PASSWORD", "positional-arg-secret")
    log_filter = RedactingLogFilter()
    record = logging.LogRecord(
        name="asc2026",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="connecting with password %s",
        args=("positional-arg-secret",),
        exc_info=None,
    )

    log_filter.filter(record)

    assert "positional-arg-secret" not in record.getMessage()


def test_logging_filter_redacts_a_secret_passed_as_a_mapping_arg(monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_PASSWORD", "mapping-arg-secret")
    log_filter = RedactingLogFilter()
    record = logging.LogRecord(
        name="asc2026",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="connecting with password %(password)s",
        # A single mapping arg is passed as a 1-tuple containing the dict --
        # this is exactly what `Logger.debug(msg, {"a": 1})` produces
        # internally via `*args`, per the stdlib logging documentation.
        args=({"password": "mapping-arg-secret"},),  # secret-scan: allow
        exc_info=None,
    )

    log_filter.filter(record)

    assert "mapping-arg-secret" not in record.getMessage()


def test_extra_fields_redacts_string_values_and_passes_through_others(monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_PASSWORD", "extra-field-secret")
    record = logging.LogRecord(
        name="asc2026",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="operational sign-in",
        args=(),
        exc_info=None,
    )
    record.actor_note = "used password extra-field-secret to sign in"
    record.attempt_count = 3

    fields = extra_fields(record)

    assert "extra-field-secret" not in fields["actor_note"]
    assert fields["attempt_count"] == 3


@pytest.mark.parametrize(
    "message",
    [
        's3_storage_secret_access_key="prefixed-suffixed-value-1"',  # secret-scan: allow
        "secret-key: prefixed-suffixed-value-2",  # secret-scan: allow
        "secret key = prefixed-suffixed-value-3",  # secret-scan: allow
        "database_migration_password=prefixed-suffixed-value-4",  # secret-scan: allow
        "JWT_SIGNING_SECRET=prefixed-suffixed-value-5",  # secret-scan: allow
        "rate_limit_hmac_key_v3=prefixed-suffixed-value-6",  # secret-scan: allow
    ],
)
def test_redacts_every_prefixed_or_suffixed_credential_name_shape(message: str) -> None:
    result = redact(message)

    assert "prefixed-suffixed-value" not in result
    assert MASK in result


def test_does_not_redact_a_non_credential_generic_assignment() -> None:
    message = "display_name=John Doe, event=ASC2026"
    assert redact(message) == message


def test_redacts_a_very_long_prefixed_access_key_identifier() -> None:
    # Prompt 2 final closure pass §4: no arbitrary seven-segment limit.
    message = (
        "VERY_LONG_EXTERNAL_SERVICE_CONFIGURATION_PROVIDER_ACCESS_KEY="
        "hardcoded-long-identifier-value"
    )
    result = redact(message)

    assert "hardcoded-long-identifier-value" not in result
    assert MASK in result


def test_does_not_redact_a_credential_name_beyond_the_length_ceiling() -> None:
    absurd_name = "A_" + "X" * 39 + "_" + "Y" * 39 + "_" + "Z" * 39 + "_ACCESS_KEY"
    assert len(absurd_name) > 128
    message = f"{absurd_name}=hardcoded-should-not-matter-value"

    result = redact(message)

    assert result == message


def test_redacts_a_quoted_passphrase_containing_spaces() -> None:
    # Prompt 2 final closure pass §5.
    message = 'DATABASE_PASSWORD = "correct horse battery staple"'  # secret-scan: allow
    result = redact(message)

    assert "correct horse battery staple" not in result
    assert MASK in result


def test_redacts_an_unquoted_passphrase_containing_spaces() -> None:
    message = "database_password=correct horse battery staple"  # secret-scan: allow
    result = redact(message)

    assert "correct horse battery staple" not in result
    assert MASK in result


@pytest.mark.parametrize(
    "credential_name",
    [
        "_".join(["A"] * 21 + ["ACCESS", "KEY"]),
        "X" * 50 + "_ACCESS_KEY",
        "PREFIX_" + "Y" * 50 + "_SECRET_KEY",
        "Z" * (128 - len("_ACCESS_KEY")) + "_ACCESS_KEY",
    ],
)
def test_redacts_credential_names_with_only_the_total_length_bound(
    credential_name: str,
) -> None:
    assert len(credential_name) <= 128
    secret = "hardcoded-total-bound-secret"  # secret-scan: allow

    result = redact(f'{credential_name} = "{secret}"')

    assert secret not in result
    assert MASK in result


def test_leaves_a_long_noncredential_identifier_unchanged() -> None:
    ordinary_name = "X" * 100 + "_DISPLAY_CONFIGURATION"
    message = f'{ordinary_name} = "ordinary public value"'

    assert redact(message) == message


@pytest.mark.parametrize(
    "message, retained_suffix",
    [
        ("DATABASE_PASSWORD: one two three four five six seven", ""),  # secret-scan: allow
        (
            "DATABASE_PASSWORD: one two three four five "  # secret-scan: allow
            "six seven eight nine ten",
            "",
        ),
        (
            'DATABASE_PASSWORD: "one two three four five '  # secret-scan: allow
            'six seven eight nine ten"',
            "",
        ),
        (
            "PASSWORD: one two three four five six seven # deployment note",  # secret-scan: allow
            "# deployment note",
        ),
        (
            "PASSWORD: one two three four five six seven, user=asc2026",  # secret-scan: allow
            ", user=asc2026",
        ),
        (
            "PASSWORD: one two three four five six seven; user=asc2026",  # secret-scan: allow
            "; user=asc2026",
        ),
    ],
)
def test_redacts_complete_multiword_values_without_leaving_a_suffix(
    message: str, retained_suffix: str
) -> None:
    result = redact(message)

    for word in (
        "one",
        "two",
        "three",
        "four",
        "five",
        "six",
        "seven",
        "eight",
        "nine",
        "ten",
    ):
        assert word not in result
    assert MASK in result
    if retained_suffix:
        assert retained_suffix in result


def test_multiword_redaction_does_not_change_an_ordinary_sentence() -> None:
    message = "registration remains open for all eligible conference participants"

    assert redact(message) == message


def test_extra_fields_recursively_redacts_a_nested_list_and_dict(monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_PASSWORD", "nested-structure-secret")
    record = logging.LogRecord(
        name="asc2026",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="operational action",
        args=(),
        exc_info=None,
    )
    record.attempts = [{"detail": "used nested-structure-secret here"}, {"detail": "clean"}]

    fields = extra_fields(record)

    assert "nested-structure-secret" not in fields["attempts"][0]["detail"]
    assert fields["attempts"][1]["detail"] == "clean"


def test_extra_fields_redacts_a_set_of_strings(monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_PASSWORD", "set-member-secret")
    record = logging.LogRecord(
        name="asc2026",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="operational action",
        args=(),
        exc_info=None,
    )
    record.notes = {"used set-member-secret once", "clean note"}

    fields = extra_fields(record)

    assert not any("set-member-secret" in item for item in fields["notes"])
    assert "clean note" in fields["notes"]


def test_extra_fields_handles_a_self_referential_dict_without_recursing_forever(
    monkeypatch,
) -> None:
    monkeypatch.setenv("DATABASE_PASSWORD", "cyclic-extra-secret")
    record = logging.LogRecord(
        name="asc2026",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="operational action",
        args=(),
        exc_info=None,
    )
    cyclic: dict[str, object] = {"note": "holds cyclic-extra-secret"}
    cyclic["self"] = cyclic
    record.cyclic = cyclic

    fields = extra_fields(record)

    assert "cyclic-extra-secret" not in str(fields["cyclic"])


def test_extra_fields_stringifies_and_redacts_an_unsupported_object(monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_PASSWORD", "unsupported-object-secret")

    class ArbitraryValue:
        def __str__(self) -> str:  # pragma: no cover - defensive shape only
            return "ArbitraryValue(password=unsupported-object-secret)"  # secret-scan: allow

    record = logging.LogRecord(
        name="asc2026",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="operational action",
        args=(),
        exc_info=None,
    )
    record.odd_value = ArbitraryValue()

    fields = extra_fields(record)

    assert "unsupported-object-secret" not in fields["odd_value"]


def test_extra_fields_excludes_standard_logrecord_and_correlation_attributes() -> None:
    record = logging.LogRecord(
        name="asc2026",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="ordinary message",
        args=(),
        exc_info=None,
    )
    record.correlation_id = "abc-123"

    fields = extra_fields(record)

    for standard_attr in ("name", "msg", "args", "levelname", "pathname", "correlation_id"):
        assert standard_attr not in fields


def test_persistent_payload_masks_domain_sensitive_keys_recursively() -> None:
    payload = {
        "status": "accepted",
        "otp_value": str(246810),
        "nested": {"passport_number": "P" + str(1234567)},
        "items": [{"storage_key": "private/object"}],
    }
    assert sanitize_persistent_payload(payload) == {
        "status": "accepted",
        "otp_value": MASK,
        "nested": {"passport_number": MASK},
        "items": [{"storage_key": MASK}],
    }
