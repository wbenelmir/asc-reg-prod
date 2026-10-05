"""Accidental-credential scan over project-controlled files.

Developer execution-control decision: scan project-controlled files for
accidental credentials while explicitly excluding `.env`, `var/`, `.venv/`,
vendored minified assets, and generated local artifacts.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.check import (
    MARKER_ALLOWED_RELATIVE_FILES,
    check_env_file_is_excluded,
    check_no_leaked_credentials,
    check_unauthorized_suppression_markers,
    iter_manifest_files,
    iter_project_files,
)


def test_no_leaked_credentials_in_the_real_project() -> None:
    findings = check_no_leaked_credentials()
    assert findings == [], f"possible leaked credential(s) found: {findings}"


def test_env_itself_is_never_scanned(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("DATABASE_PASSWORD=a-real-looking-secret-value\n")
    (tmp_path / ".gitignore").write_text(
        ".env\n.env.*\n!.env.example\n.venv/\nvar/\nstaticfiles/\n"
    )

    scanned = {p.relative_to(tmp_path) for p in iter_project_files(tmp_path)}

    assert Path(".env") not in scanned


def test_var_and_venv_directories_are_never_scanned(tmp_path: Path) -> None:
    (tmp_path / "var" / "mail").mkdir(parents=True)
    (tmp_path / "var" / "mail" / "1.eml").write_text("password=leaked-if-scanned\n")
    (tmp_path / ".venv" / "lib").mkdir(parents=True)
    (tmp_path / ".venv" / "lib" / "site.py").write_text("password=leaked-if-scanned\n")

    findings = check_no_leaked_credentials(tmp_path)

    assert findings == []


def test_vendored_minified_assets_are_excluded_from_the_credential_scan(tmp_path: Path) -> None:
    vendor_dir = tmp_path / "static" / "vendor" / "example"
    vendor_dir.mkdir(parents=True)
    fixture_content = 'token="deadbeefdeadbeef"\n'  # secret-scan: allow
    (vendor_dir / "example.min.js").write_text(fixture_content)

    findings = check_no_leaked_credentials(tmp_path)

    assert findings == []


def test_detects_a_postgres_connection_string_with_embedded_password(tmp_path: Path) -> None:
    fixture_content = (
        'DSN = "postgresql://someuser:realpassword123@db.example.com/prod"\n'  # secret-scan: allow
    )
    (tmp_path / "leaky.py").write_text(fixture_content)

    findings = check_no_leaked_credentials(tmp_path)

    assert any("PostgreSQL" in f for f in findings)


def test_detects_a_private_key_block(tmp_path: Path) -> None:
    fixture_content = "-----BEGIN RSA PRIVATE KEY-----\nMIIB...\n"  # secret-scan: allow
    (tmp_path / "oops.txt").write_text(fixture_content)

    findings = check_no_leaked_credentials(tmp_path)

    assert any("private key" in f for f in findings)


def test_does_not_flag_our_own_underscored_env_var_names(tmp_path: Path) -> None:
    # DATABASE_PASSWORD, EMAIL_PROVIDER_API_KEY etc. are variable NAMES, not
    # secret shapes -- the dedicated .env.example placeholder check (a
    # different function) is what validates those.
    (tmp_path / "settings_like.py").write_text(
        'DATABASE_PASSWORD = require("DATABASE_PASSWORD")\n'
        'EMAIL_PROVIDER_API_KEY = env("EMAIL_PROVIDER_API_KEY")\n'
    )

    findings = check_no_leaked_credentials(tmp_path)

    assert findings == []


def test_does_not_flag_the_exact_safe_env_lookup_example(tmp_path: Path) -> None:
    # The exact shape named as a required negative case: an environment
    # lookup, not a hard-coded value.
    (tmp_path / "settings_like.py").write_text('SECRET_KEY = env("SECRET_KEY")\n')

    findings = check_no_leaked_credentials(tmp_path)

    assert findings == []


def test_does_not_flag_a_bare_variable_name_reference(tmp_path: Path) -> None:
    # A variable name appearing by itself (no assignment, no value) must
    # never be flagged -- e.g. mentioned in a docstring, a redaction list,
    # or a dataclass field declaration.
    (tmp_path / "notes.py").write_text(
        "# See SECRET_KEY, ACCESS_KEY, and SECRET_ACCESS_KEY for context.\n"
        "SECRET_KEY: str\n"  # secret-scan: allow
        "ACCESS_KEY: str\n"  # secret-scan: allow
    )

    findings = check_no_leaked_credentials(tmp_path)

    assert findings == []


@pytest.mark.parametrize(
    "assignment",
    [
        'secret_key = "hardcoded-secret-value"',  # secret-scan: allow
        'secret-key: "hardcoded-secret-value"',  # secret-scan: allow
        '"secret key" = "hardcoded-secret-value"',  # secret-scan: allow
        'SECRET_KEY = "hardcoded-secret-value"',  # secret-scan: allow
    ],
)
def test_detects_hardcoded_secret_key_shaped_values(tmp_path: Path, assignment: str) -> None:
    (tmp_path / "leaky.py").write_text(assignment + "\n")

    findings = check_no_leaked_credentials(tmp_path)

    assert findings, f"expected a finding for: {assignment!r}"
    assert any("secret-shaped" in f for f in findings)


@pytest.mark.parametrize(
    "assignment",
    [
        'access_key = "hardcoded-access-value"',  # secret-scan: allow
        'access-key: "hardcoded-access-value"',  # secret-scan: allow
        '"access key" = "hardcoded-access-value"',  # secret-scan: allow
        'ACCESS_KEY = "hardcoded-access-value"',  # secret-scan: allow
    ],
)
def test_detects_hardcoded_access_key_shaped_values(tmp_path: Path, assignment: str) -> None:
    (tmp_path / "leaky.py").write_text(assignment + "\n")

    findings = check_no_leaked_credentials(tmp_path)

    assert findings, f"expected a finding for: {assignment!r}"
    assert any("secret-shaped" in f for f in findings)


def test_detects_hardcoded_secret_key_in_a_structured_dict_context(tmp_path: Path) -> None:
    # Structured/dict/JSON configuration shape: the key itself is quoted,
    # with a closing quote appearing between the name and the separator.
    fixture_content = 'CONFIG = {"secret_key": "hardcoded-dict-value"}\n'  # secret-scan: allow
    (tmp_path / "config_like.py").write_text(fixture_content)

    findings = check_no_leaked_credentials(tmp_path)

    assert findings, "expected a finding for a quoted dict-style secret_key"


def test_does_not_flag_a_placeholder_secret_key_value(tmp_path: Path) -> None:
    (tmp_path / "example_like.py").write_text('secret_key = "__SET_IN_LOCAL_ENV__"\n')

    findings = check_no_leaked_credentials(tmp_path)

    assert findings == []


def test_manifest_excludes_env_and_includes_complete_vendor_runtime(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("DATABASE_PASSWORD=real\n")
    (tmp_path / "PROMPT3_REVIEW_MANIFEST.txt").write_text("generated output\n")
    (tmp_path / "PROMPT4_FINAL_REVIEW_MANIFEST.txt").write_text("generated output\n")
    vendor_dir = tmp_path / "static" / "vendor" / "example"
    vendor_dir.mkdir(parents=True)
    (vendor_dir / "example.min.css").write_text("body{color:red}")
    (vendor_dir / "PROVENANCE.md").write_text("# provenance\n")

    # Compare Path objects, not raw strings -- relative_to() renders with
    # the platform's native separator (backslash on Windows), which a
    # hardcoded forward-slash string comparison would mismatch.
    manifested = {p.relative_to(tmp_path) for p in iter_manifest_files(tmp_path)}

    assert Path(".env") not in manifested
    assert Path("PROMPT3_REVIEW_MANIFEST.txt") not in manifested
    assert Path("PROMPT4_FINAL_REVIEW_MANIFEST.txt") not in manifested
    assert Path("static/vendor/example/example.min.css") in manifested
    assert Path("static/vendor/example/PROVENANCE.md") in manifested


@pytest.mark.parametrize(
    "assignment",
    [
        'DATABASE_PASSWORD = "hardcoded-database-password-value"',  # secret-scan: allow
        'DATABASE_MIGRATION_PASSWORD = "hardcoded-migration-password"',  # secret-scan: allow
        'DJANGO_SECRET_KEY = "hardcoded-django-secret-key-value"',  # secret-scan: allow
        'S3_STORAGE_ACCESS_KEY_ID = "AKIAHARDCODEDVALUE1234"',  # secret-scan: allow
        'S3_STORAGE_SECRET_ACCESS_KEY = "hardcoded-s3-secret-access-key"',  # secret-scan: allow
        'EMAIL_PROVIDER_API_KEY = "hardcoded-email-provider-api-key"',  # secret-scan: allow
        'RATE_LIMIT_HMAC_KEY_V1 = "hardcoded-rate-limit-key-v1"',  # secret-scan: allow
        'RATE_LIMIT_HMAC_KEY_V2 = "hardcoded-rate-limit-key-v2"',  # secret-scan: allow
        'JWT_SIGNING_SECRET = "hardcoded-jwt-signing-secret"',  # secret-scan: allow
        'THIRD_PARTY_PASSWORD = "hardcoded-third-party-password"',  # secret-scan: allow
        'CUSTOM_PROVIDER_SECRET = "hardcoded-custom-provider-secret"',  # secret-scan: allow
        'PAYMENT_GATEWAY_SECRET_KEY = "hardcoded-gateway-secret-key"',  # secret-scan: allow
        'BACKUP_STORAGE_SECRET_ACCESS_KEY = "hardcoded-backup-secret"',  # secret-scan: allow
        'WEBHOOK_TOKEN = "hardcoded-webhook-token-value"',  # secret-scan: allow
        'ANALYTICS_API_KEY = "hardcoded-analytics-api-key"',  # secret-scan: allow
        'LEGACY_ACCESS_KEY = "hardcoded-legacy-access-key"',  # secret-scan: allow
    ],
)
def test_detects_every_required_prefixed_or_suffixed_identifier(
    tmp_path: Path, assignment: str
) -> None:
    (tmp_path / "leaky_settings.py").write_text(assignment + "\n")

    findings = check_no_leaked_credentials(tmp_path)

    assert findings, f"expected a finding for: {assignment!r}"


def test_detects_a_very_long_prefixed_access_key_identifier(tmp_path: Path) -> None:
    # Prompt 2 final closure pass §4: no arbitrary seven-segment limit --
    # this is an 8-word identifier.
    assignment = (
        "VERY_LONG_EXTERNAL_SERVICE_CONFIGURATION_PROVIDER_ACCESS_KEY = "
        '"hardcoded-long-identifier-value"'
    )  # secret-scan: allow
    (tmp_path / "leaky_settings.py").write_text(assignment + "\n")

    findings = check_no_leaked_credentials(tmp_path)

    assert findings, f"expected a finding for the long identifier: {assignment!r}"


@pytest.mark.parametrize(
    "credential_name",
    [
        "_".join(["A"] * 21 + ["ACCESS", "KEY"]),
        "X" * 50 + "_ACCESS_KEY",
        "PREFIX_" + "Y" * 50 + "_SECRET_KEY",
        "Z" * (128 - len("_ACCESS_KEY")) + "_ACCESS_KEY",
    ],
)
def test_detects_credential_names_with_only_the_total_length_bound(
    tmp_path: Path, credential_name: str
) -> None:
    assert len(credential_name) <= 128
    fixture = f'{credential_name} = "hardcoded-total-bound-secret"\n'  # secret-scan: allow
    (tmp_path / "leaky_settings.py").write_text(fixture)

    findings = check_no_leaked_credentials(tmp_path)

    assert findings, f"expected a finding for {credential_name!r}"


def test_does_not_flag_a_long_noncredential_identifier(tmp_path: Path) -> None:
    ordinary_name = "X" * 100 + "_DISPLAY_CONFIGURATION"
    (tmp_path / "ordinary.py").write_text(f'{ordinary_name} = "ordinary public value"\n')

    assert check_no_leaked_credentials(tmp_path) == []


def test_does_not_flag_a_credential_name_beyond_the_length_ceiling(tmp_path: Path) -> None:
    # A name that would otherwise be credential-SHAPED by suffix, fully
    # matchable by NAME_TOKEN_PATTERN's own segment/length bounds, but
    # whose TOTAL length exceeds the explicit 128-character ceiling, must
    # be rejected by that ceiling rather than silently accepted.
    absurd_name = "A_" + "X" * 39 + "_" + "Y" * 39 + "_" + "Z" * 39 + "_ACCESS_KEY"
    assert len(absurd_name) > 128
    assignment = f'{absurd_name} = "hardcoded-should-not-matter-value"'
    (tmp_path / "leaky_settings.py").write_text(assignment + "\n")

    findings = check_no_leaked_credentials(tmp_path)

    assert findings == []


def test_detects_a_hardcoded_credential_in_yaml_style_unquoted_form(tmp_path: Path) -> None:
    fixture_content = "database_password: hardcoded-unquoted-password-value\n"  # secret-scan: allow
    (tmp_path / "config_like.yaml").write_text(fixture_content)

    findings = check_no_leaked_credentials(tmp_path)

    assert findings, "expected a finding for a YAML-style unquoted hard-coded password"


def test_does_not_flag_a_yaml_style_unquoted_non_credential_value(tmp_path: Path) -> None:
    (tmp_path / "config_like.yaml").write_text("display_name: some ordinary configured value\n")

    findings = check_no_leaked_credentials(tmp_path)

    assert findings == []


def test_detects_a_quoted_passphrase_containing_spaces(tmp_path: Path) -> None:
    # Prompt 2 final closure pass §5.
    fixture_content = 'DATABASE_PASSWORD = "correct horse battery staple"\n'  # secret-scan: allow
    (tmp_path / "leaky.py").write_text(fixture_content)

    findings = check_no_leaked_credentials(tmp_path)

    assert findings, "expected a finding for a quoted passphrase containing spaces"
    assert any("secret-shaped" in f for f in findings)


def test_detects_a_yaml_style_unquoted_passphrase_containing_spaces(tmp_path: Path) -> None:
    fixture_content = "DATABASE_PASSWORD: correct horse battery staple\n"  # secret-scan: allow
    (tmp_path / "config_like.yaml").write_text(fixture_content)

    findings = check_no_leaked_credentials(tmp_path)

    assert findings, "expected a finding for a YAML-style unquoted passphrase with spaces"


def test_yaml_style_passphrase_with_a_trailing_comment_is_handled_safely(tmp_path: Path) -> None:
    line = "database_password: correct horse battery staple  # do not use\n"  # secret-scan: allow
    (tmp_path / "config_like.yaml").write_text(line)

    findings = check_no_leaked_credentials(tmp_path)

    assert findings, "expected a finding even with a trailing YAML comment"


def test_does_not_flag_a_type_annotated_declaration_with_a_default(tmp_path: Path) -> None:
    # A Python type annotation with a default value has the same
    # `name: <type> = <default>` shape as a YAML scalar once spaces are
    # allowed in the value -- it must never be mistaken for one.
    (tmp_path / "dataclass_like.py").write_text(
        "database_password: str | None = None\ns3_secret_access_key: str | None = None\n"
    )

    findings = check_no_leaked_credentials(tmp_path)

    assert findings == []


def test_detects_multiple_credentials_with_spaces_in_one_json_like_fixture(
    tmp_path: Path,
) -> None:
    fixture_content = (
        "{\n"
        '  "database_password": "correct horse battery staple",\n'  # secret-scan: allow
        '  "email_provider_api_key": "another passphrase with spaces"\n'  # secret-scan: allow
        "}\n"
    )
    (tmp_path / "config_like.json").write_text(fixture_content)

    findings = check_no_leaked_credentials(tmp_path)

    assert len(findings) >= 2, f"expected two findings, got: {findings}"


@pytest.mark.parametrize(
    "safe_lookup",
    [
        'DATABASE_PASSWORD = env("DATABASE_PASSWORD")',
        'SECRET_KEY = require("DJANGO_SECRET_KEY")',
        'S3_STORAGE_SECRET_ACCESS_KEY = os.environ["S3_STORAGE_SECRET_ACCESS_KEY"]',
    ],
)
def test_does_not_flag_the_required_safe_environment_lookup_examples(
    tmp_path: Path, safe_lookup: str
) -> None:
    (tmp_path / "settings_like.py").write_text(safe_lookup + "\n")

    findings = check_no_leaked_credentials(tmp_path)

    assert findings == [], f"safe lookup incorrectly flagged: {safe_lookup!r}"


def test_detects_a_hardcoded_credential_in_json_style_syntax(tmp_path: Path) -> None:
    fixture_content = (
        '{\n  "database_password": "hardcoded-json-password-value"\n}\n'  # secret-scan: allow
    )
    (tmp_path / "config_like.json").write_text(fixture_content)

    findings = check_no_leaked_credentials(tmp_path)

    assert findings, "expected a finding for a JSON-style hard-coded database_password"


def test_marker_is_authorized_in_an_approved_test_file(tmp_path: Path) -> None:
    allowed_relative = next(iter(MARKER_ALLOWED_RELATIVE_FILES))
    allowed_path = tmp_path / allowed_relative
    allowed_path.parent.mkdir(parents=True, exist_ok=True)
    allowed_path.write_text('token = "hardcoded-fixture-token-value"  # secret-scan: allow\n')

    marker_problems = check_unauthorized_suppression_markers(tmp_path)
    findings = check_no_leaked_credentials(tmp_path)

    assert marker_problems == []
    assert findings == []


def test_marker_is_rejected_outside_the_approved_allowlist(tmp_path: Path) -> None:
    (tmp_path / "apps_core_like.py").write_text(
        'token = "hardcoded-fixture-token-value"  # secret-scan: allow\n'
    )

    marker_problems = check_unauthorized_suppression_markers(tmp_path)

    assert marker_problems, "expected an unauthorized-marker finding"
    assert any("apps_core_like.py" in problem for problem in marker_problems)


def test_unauthorized_marker_does_not_suppress_the_real_finding(tmp_path: Path) -> None:
    (tmp_path / "apps_core_like.py").write_text(
        'token = "hardcoded-fixture-token-value"  # secret-scan: allow\n'
    )

    findings = check_no_leaked_credentials(tmp_path)

    assert findings, (
        "an unauthorized marker must never suppress a real finding -- "
        "check_unauthorized_suppression_markers() reports the misuse separately"
    )


def test_marker_on_one_line_cannot_suppress_a_credential_on_the_next_line(
    tmp_path: Path,
) -> None:
    # Prompt 2 correction §3 (v3): the marker suppresses ONLY the exact
    # line it appears on. A marker-bearing comment line immediately
    # before a real credential assignment must NOT suppress that
    # following line.
    allowed_relative = next(iter(MARKER_ALLOWED_RELATIVE_FILES))
    allowed_path = tmp_path / allowed_relative
    allowed_path.parent.mkdir(parents=True, exist_ok=True)
    allowed_path.write_text(
        "# secret-scan: allow (marker on this comment line only)\n"
        'unsuppressed_secret_key = "hardcoded-unsuppressed-value"\n'  # secret-scan: allow
    )

    findings = check_no_leaked_credentials(tmp_path)

    assert any(":2:" in f for f in findings), (
        "a marker on the preceding line must not suppress the next line's credential"
    )


def test_marker_suppresses_only_its_own_exact_line(tmp_path: Path) -> None:
    allowed_relative = next(iter(MARKER_ALLOWED_RELATIVE_FILES))
    allowed_path = tmp_path / allowed_relative
    allowed_path.parent.mkdir(parents=True, exist_ok=True)
    allowed_path.write_text(
        'suppressed_secret_key = "hardcoded-suppressed-value"  # secret-scan: allow\n'
        'unrelated_secret_key = "hardcoded-unrelated-value"\n'  # secret-scan: allow
    )

    findings = check_no_leaked_credentials(tmp_path)

    assert not any(":1:" in f for f in findings), "the marker's own line must be suppressed"
    assert any(":2:" in f for f in findings), "the following, unmarked line must still be detected"


def test_env_file_exclusion_covers_every_env_dotted_variant_except_example(
    tmp_path: Path,
) -> None:
    (tmp_path / ".env").write_text("DATABASE_PASSWORD=real\n")
    (tmp_path / ".env.local").write_text("DATABASE_PASSWORD=real\n")
    (tmp_path / ".env.production").write_text("DATABASE_PASSWORD=real\n")
    (tmp_path / ".env.staging").write_text("DATABASE_PASSWORD=real\n")
    (tmp_path / ".env.example").write_text("DATABASE_PASSWORD=__SET_IN_LOCAL_ENV__\n")

    scanned = {p.relative_to(tmp_path) for p in iter_project_files(tmp_path)}
    manifested = {p.relative_to(tmp_path) for p in iter_manifest_files(tmp_path)}

    for excluded_name in (".env", ".env.local", ".env.production", ".env.staging"):
        assert Path(excluded_name) not in scanned
        assert Path(excluded_name) not in manifested
    assert Path(".env.example") in manifested

    problems = check_env_file_is_excluded(tmp_path)
    assert problems == []


def test_manifest_includes_project_owned_binary_assets(tmp_path: Path) -> None:
    # A project-owned image (e.g. docs/design/assets/logo.svg) is exactly
    # the kind of file a file-inventory manifest should cover -- only
    # VENDORED compiled/minified assets are skipped, never a project-owned
    # binary asset merely because of its suffix.
    docs_dir = tmp_path / "docs" / "design" / "assets"
    docs_dir.mkdir(parents=True)
    (docs_dir / "logo.svg").write_bytes(b"<svg></svg>")

    manifested = {p.relative_to(tmp_path) for p in iter_manifest_files(tmp_path)}

    assert Path("docs/design/assets/logo.svg") in manifested
