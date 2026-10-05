#!/usr/bin/env python
"""Provider-neutral quality and verification commands (accepted plan §9).

    uv run python scripts/check.py <subcommand>

Every function here is importable (`from scripts.check import ...`), so the
exact same logic backs both the CLI entrypoint and the automated test suite
in `tests/foundation/` -- there is one source of truth, not two.

**No command in this module requires, invokes, or depends on Git or a
`.git` directory.** Per developer execution-control decision, this project
is local-filesystem-only at this stage: `.gitignore` is retained as a
future safety file, but nothing here relies on it being interpreted by a
version-control tool. Local-safety verification is instead a direct,
provider-neutral filesystem/content check.

Subcommands implemented in Prompt 2:
    local-safety  verify .gitignore text, .env existence/exclusion, and
                  that .env.example holds placeholders only (no Git needed)
    secret-scan   scan project-controlled files for accidental credentials
                  (excludes .env, var/, .venv/, vendored/minified/generated
                  artifacts)
    manifest      print a SHA-256 manifest of project-owned files
    assets        recompute vendored-asset checksums against PROVENANCE.md
    db-probe      read-only PostgreSQL connectivity + version probe against
                  the approved local database identity (credentials redacted)
    deploy        run `manage.py check --deploy` for staging and production with
                  documented synthetic, non-secret environment values
    all           the full acceptance check -- REQUIRES the real
                  local PostgreSQL 17 probe to succeed; missing DB
                  configuration is a failure here, not a skip
    all-no-db     DB-optional developer convenience (skips only the live
                  probe). NEVER the full acceptance check -- says so in
                  its own output every time it runs
    db-state      Prompt 3 CP 3a read-only pre-flight database-state gate
    audit-isolation  Prompt 3 CP 3b / deployment-team post-migration check
                  of the honest two-level audit append-only contract
                  (ADR-0008); never counted in `all`'s aggregate pass/fail
    build-static  build the static files of a runtime artifact: `collectstatic`
                  with the staging settings and the synthetic, non-secret
                  values below (no real secret is needed to build)
    artifact-manifest  record the SHA-256 of every file of a frozen runtime
                  artifact: --root <artifact> --output <file outside it>
    artifact-verify  verify a runtime artifact against its manifest, read-only:
                  --root <artifact> --manifest <file>
    release-readiness  P4-4-C1 (R-03): READY / BLOCKED / NOT_ASSESSED for
                  the release prerequisites in `apps.core.release_readiness`
                  (`manage.py release_readiness`). `all` reports it next to,
                  never inside, the local regression result
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

# `apps.core.credential_shapes` is shared with the log-redaction layer
# (Prompt 2 correction §2/§5) so the two can never drift apart on
# credential-name coverage. Running this file directly as
# `uv run python scripts/check.py <subcommand>` puts only `scripts/` on
# `sys.path` (Python's own script-directory rule), not the project root --
# so the project root is added explicitly here, before that import,
# rather than relying on pytest's rootdir insertion (which only happens
# for the test suite, never for direct CLI invocation).
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from apps.core.credential_shapes import (  # noqa: E402
    NAME_TOKEN_PATTERN,
    is_credential_shaped_name,
)

# ---------------------------------------------------------------------------
# Local-safety contract -- provider-neutral, no Git required (developer
# execution-control decision: this project does not use Git at this stage).
# Three independent, purely filesystem-based checks:
#   1. .gitignore TEXT retains the required rules (kept as a future safety
#      file; this does not simulate what a VCS would do with them).
#   2. .env exists locally and is structurally excluded from every scan
#      this project's own tooling performs.
#   3. .env.example holds placeholders only -- never a real-looking secret.
# ---------------------------------------------------------------------------

REQUIRED_GITIGNORE_RULES: tuple[str, ...] = (
    ".env",
    ".env.*",
    "!.env.example",
    ".venv/",
    "var/",
    "staticfiles/",
)

# Directory NAMES (matched against any path component) that are never
# scanned by check_no_leaked_credentials() or included in
# compute_project_manifest(). This is the authoritative exclusion set that
# check_env_file_is_excluded() verifies `.env` is actually covered by --
# not merely asserted to be.
SECRET_SCAN_EXCLUDED_DIR_NAMES: frozenset[str] = frozenset(
    {
        ".venv",
        ".git",  # defensive; this project does not use Git, but never scan it if it reappears
        "__pycache__",
        ".ruff_cache",
        ".pytest_cache",  # generated by running pytest
        ".claude",  # local tool configuration, not project-owned
        "var",  # var/private/ and var/mail/ -- local runtime state, never scanned
        "staticfiles",  # generated collectstatic output
        "node_modules",
    }
)

# `.env` and EVERY `.env.*` file are excluded structurally, with exactly
# one documented exception: `.env.example` (project-owned, placeholders
# only, included in the manifest/archive and validated separately).
# Matched by basename so it applies at any depth, mirroring `.gitignore`'s
# own `.env` / `.env.*` / `!.env.example` rules (Prompt 2 correction §3).
_ENV_EXAMPLE_BASENAME = ".env.example"


def _is_excluded_env_file(path: Path) -> bool:
    name = path.name
    if name == _ENV_EXAMPLE_BASENAME:
        return False
    return name == ".env" or name.startswith(".env.")


# Vendored third-party directories: binary/minified content, covered by
# their own PROVENANCE.md checksum instead of a credential scan.
SECRET_SCAN_VENDOR_DIR_PARTS: tuple[tuple[str, ...], ...] = (
    ("static", "vendor"),
    ("tests", "assets", "vendor"),
)

# This project's own out-of-scope licensed reference material -- never
# inspected, scanned, indexed, or summarized (Finder licence rules;
# see docs/design/finder_inventory.md).
SECRET_SCAN_EXCLUDED_TOP_LEVEL_DIRS: frozenset[str] = frozenset({"reference"})

# File suffixes never worth scanning as text (binary, generated, or lock
# artifacts already covered by their own dedicated verification).
SECRET_SCAN_EXCLUDED_SUFFIXES: frozenset[str] = frozenset(
    {".pyc", ".map", ".png", ".jpg", ".jpeg", ".svg", ".ico", ".woff", ".woff2", ".ttf"}
)


def _is_excluded_path(path: Path, base_dir: Path) -> bool:
    relative = path.relative_to(base_dir)
    parts = relative.parts
    if not parts:
        return False
    if parts[0] in SECRET_SCAN_EXCLUDED_TOP_LEVEL_DIRS:
        return True
    if any(part in SECRET_SCAN_EXCLUDED_DIR_NAMES for part in parts[:-1]):
        return True
    if _is_excluded_env_file(path):
        return True
    for vendor_parts in SECRET_SCAN_VENDOR_DIR_PARTS:
        if parts[: len(vendor_parts)] == vendor_parts:
            return True
    if path.suffix.lower() in SECRET_SCAN_EXCLUDED_SUFFIXES:
        return True
    return False


def iter_project_files(base_dir: Path | None = None):
    """Yield every project-controlled file, applying the shared exclusion set.

    Excludes: `.env` itself, `var/`, `.venv/`, `__pycache__`, `.ruff_cache`,
    `staticfiles/`, vendored asset directories entirely (their compiled
    content is credential-scan noise, not a credential-scan target),
    `reference/` (out of scope for this project entirely), and non-text
    binary asset suffixes. Used by the credential scan.
    """
    base_dir = base_dir or BASE_DIR
    for path in sorted(base_dir.rglob("*")):
        if path.is_dir():
            continue
        if _is_excluded_path(path, base_dir):
            continue
        yield path


def _is_vendored_dir(parts: tuple[str, ...]) -> bool:
    return any(parts[: len(v)] == v for v in SECRET_SCAN_VENDOR_DIR_PARTS)


def iter_manifest_files(base_dir: Path | None = None):
    """Yield every project-owned file to include in the SHA-256 manifest.

    Unlike `iter_project_files()` (used for the credential scan, which has
    no use for binary/image files and skips them by suffix), the manifest
    is a file-INVENTORY: a project-owned binary asset such as
    `docs/design/assets/logo.svg` is exactly the kind of file whose
    integrity a manifest should record, so no suffix-based exclusion is
    applied here.

    Exclusions: `.env`, files under an excluded directory name (`.venv`,
    `var/`, generated caches, local tool configuration), and `reference/`
    (out of scope for this project entirely). Vendored compiled assets are
    intentionally included: the review manifest is also the authoritative
    archive inventory, so excluding those files would produce a ZIP that
    cannot pass the project's own asset-integrity check.
    """
    base_dir = base_dir or BASE_DIR
    for path in sorted(base_dir.rglob("*")):
        if path.is_dir():
            continue
        relative = path.relative_to(base_dir)
        parts = relative.parts
        if not parts:
            continue
        if relative.name in {
            "REVIEW_MANIFEST_SHA256.txt",
            "SHA256SUMS.txt",
        } or relative.name.endswith("_REVIEW_MANIFEST.txt"):
            # A review checksum manifest cannot truthfully contain its own
            # checksum. Exclude every phase's manifest so this remains true
            # when the current review package number advances.
            continue
        if parts[0] in SECRET_SCAN_EXCLUDED_TOP_LEVEL_DIRS:
            continue
        if any(part in SECRET_SCAN_EXCLUDED_DIR_NAMES for part in parts[:-1]):
            continue
        if _is_excluded_env_file(path):
            continue
        yield path


def check_gitignore_text_contract(base_dir: Path | None = None) -> list[str]:
    """Return problem descriptions; empty means `.gitignore` retains every required rule.

    Pure text inspection of `.gitignore`'s own content. Does not invoke Git,
    does not require a `.git` directory, and does not simulate ignore-path
    matching -- `.gitignore` is kept only as a future safety file per
    developer decision.
    """
    base_dir = base_dir or BASE_DIR
    gitignore_path = base_dir / ".gitignore"
    if not gitignore_path.is_file():
        return [".gitignore is missing"]
    lines = {line.strip() for line in gitignore_path.read_text(encoding="utf-8").splitlines()}
    return [
        f".gitignore is missing required rule: {rule!r}"
        for rule in REQUIRED_GITIGNORE_RULES
        if rule not in lines
    ]


def check_env_file_is_excluded(base_dir: Path | None = None) -> list[str]:
    """Return problem descriptions; empty means every local env file is provably excluded.

    "Provably" means structurally: `.env` and every `.env.*` file (except
    the one documented exception, `.env.example`) must be in the same
    exclusion set that `iter_project_files()` and `iter_manifest_files()`
    actually apply -- verified here by asking those functions directly
    rather than by trusting a separate claim elsewhere. This function
    never opens or reads any excluded file's content; it only checks
    filesystem presence and membership in the scan/manifest output.
    """
    base_dir = base_dir or BASE_DIR
    problems: list[str] = []
    env_path = base_dir / ".env"
    if not env_path.is_file():
        problems.append(".env does not exist locally")
        return problems

    scanned_paths = {p.relative_to(base_dir) for p in iter_project_files(base_dir)}
    manifested_paths = {p.relative_to(base_dir) for p in iter_manifest_files(base_dir)}

    if Path(".env") in scanned_paths:
        problems.append(".env was NOT excluded from iter_project_files() -- fix the exclusion set")
    if Path(".env") in manifested_paths:
        problems.append(".env was NOT excluded from iter_manifest_files() -- fix the exclusion set")

    # Every OTHER real .env.* file actually present locally must also be
    # excluded (not just .env itself). .env.example is the one documented
    # exception and must remain included in both.
    for candidate in sorted(base_dir.glob(".env.*")):
        if candidate.name == _ENV_EXAMPLE_BASENAME:
            continue
        relative = candidate.relative_to(base_dir)
        if relative in scanned_paths:
            problems.append(f"{relative} was NOT excluded from iter_project_files()")
        if relative in manifested_paths:
            problems.append(f"{relative} was NOT excluded from iter_manifest_files()")

    if Path(_ENV_EXAMPLE_BASENAME) not in manifested_paths:
        problems.append(f"{_ENV_EXAMPLE_BASENAME} must remain included in iter_manifest_files()")

    return problems


PLACEHOLDER_PATTERN = re.compile(r"^__[A-Z0-9_]+__$")

# Names that legitimately hold a real, non-secret literal default in
# .env.example (booleans, host/port/name defaults, numeric windows).
_ENV_EXAMPLE_NON_SECRET_NAMES: frozenset[str] = frozenset(
    {
        "DJANGO_ALLOWED_HOSTS",
        "DATABASE_HOST",
        "DATABASE_PORT",
        "DATABASE_NAME",
        "DATABASE_USER",
        "TRUSTED_PROXY_FORWARDING_ENABLED",
        "TRUSTED_PROXY_CIDRS",
        "RATE_LIMIT_HMAC_ACTIVE_VERSIONS",
        "RATE_LIMIT_HMAC_WRITE_VERSION",
        "RATE_LIMIT_KEY_OVERLAP_SECONDS",
        "IDENTITY_ENCRYPTION_ACTIVE_VERSIONS",
        "IDENTITY_ENCRYPTION_WRITE_VERSION",
        "IDENTITY_BLIND_INDEX_HMAC_ACTIVE_VERSIONS",
        "IDENTITY_BLIND_INDEX_HMAC_WRITE_VERSION",
        # Phase 2 Prompt 2 V2 correction pass: a local-default absolute
        # origin, never a secret -- staging/production still `require()`
        # it explicitly rather than falling back to this value.
        "DJANGO_PUBLIC_BASE_URL",
    }
)


def check_env_example_placeholders_only(base_dir: Path | None = None) -> list[str]:
    """Return problem descriptions; empty means `.env.example` holds placeholders only.

    Every assignment must be either a documented placeholder
    (`__SET_IN_LOCAL_ENV__`, `__SET_BY_DEPLOYMENT_TEAM__`, or similar), a
    genuinely non-secret literal default (an approved variable name, from
    `_ENV_EXAMPLE_NON_SECRET_NAMES`), or blank. Never a real-looking value.
    """
    base_dir = base_dir or BASE_DIR
    example_path = base_dir / ".env.example"
    if not example_path.is_file():
        return [".env.example is missing"]
    problems: list[str] = []
    for line_number, raw_line in enumerate(
        example_path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        name = name.strip()
        value = value.strip()
        if not value:
            continue
        if PLACEHOLDER_PATTERN.match(value):
            continue
        if name in _ENV_EXAMPLE_NON_SECRET_NAMES:
            continue
        problems.append(f".env.example line {line_number}: {name!r} has a non-placeholder value")
    return problems


DEPLOYMENT_ENV_EXAMPLE = Path("deploy") / "staging.env.example"

#: Names whose literal value in the deployment template is a non-secret
#: default (ports, booleans, timeouts, key-version numbers, module paths).
_DEPLOYMENT_EXAMPLE_NON_SECRET_NAMES: frozenset[str] = frozenset(
    {
        "DJANGO_SETTINGS_MODULE",
        "DATABASE_PORT",
        "DATABASE_CONN_MAX_AGE",
        "HUMAN_CHECK_CHALLENGE_MAX_PER_WINDOW",
        "HUMAN_CHECK_CHALLENGE_WINDOW_SECONDS",
        "HUMAN_CHECK_CHALLENGE_FALLBACK_MAX_PER_WINDOW",
        "CLAMD_PORT",
        "CLAMD_CONNECT_TIMEOUT_SECONDS",
        "CLAMD_SCAN_TIMEOUT_SECONDS",
        "CLAMD_MAX_STREAM_BYTES",
        "EMAIL_PORT",
        "EMAIL_USE_TLS",
        "EMAIL_USE_SSL",
        "EMAIL_TIMEOUT_SECONDS",
        "RATE_LIMIT_HMAC_ACTIVE_VERSIONS",
        "RATE_LIMIT_HMAC_WRITE_VERSION",
        "IDENTITY_ENCRYPTION_ACTIVE_VERSIONS",
        "IDENTITY_ENCRYPTION_WRITE_VERSION",
        "IDENTITY_BLIND_INDEX_HMAC_ACTIVE_VERSIONS",
        "IDENTITY_BLIND_INDEX_HMAC_WRITE_VERSION",
        "QR_SIGNING_ACTIVE_KEY_VERSIONS",
        "QR_SIGNING_WRITE_KEY_VERSION",
        "DJANGO_SECURE_HSTS_SECONDS",
        "DJANGO_SECURE_HSTS_INCLUDE_SUBDOMAINS",
        "DJANGO_SECURE_HSTS_PRELOAD",
        "ASC_HTTP_LISTEN",
        "ASC_HTTP_THREADS",
        "NIN_PROVIDER_BACKEND",
    }
)

#: Variables the deployed settings require; the template must list each one.
_DEPLOYMENT_REQUIRED_NAMES: tuple[str, ...] = (
    "DJANGO_SETTINGS_MODULE",
    "DJANGO_SECRET_KEY",
    "DJANGO_ALLOWED_HOSTS",
    "DJANGO_PUBLIC_BASE_URL",
    "DATABASE_HOST",
    "DATABASE_PORT",
    "DATABASE_NAME",
    "DATABASE_USER",
    "DATABASE_PASSWORD",
    "DATABASE_MIGRATION_USER",
    "CELERY_BROKER_URL",
    "REDIS_URL",
    "S3_STORAGE_BUCKET_NAME",
    "S3_STORAGE_ENDPOINT_URL",
    "S3_STORAGE_ACCESS_KEY_ID",
    "S3_STORAGE_SECRET_ACCESS_KEY",
    "S3_STORAGE_REGION_NAME",
    "CLAMD_SOCKET_PATH",
    "CLAMD_HOST",
    "EMAIL_HOST",
    "DEFAULT_FROM_EMAIL",
    "RATE_LIMIT_HMAC_KEY_V1",
    "IDENTITY_ENCRYPTION_KEY_V1",
    "IDENTITY_BLIND_INDEX_HMAC_KEY_V1",
)


def check_deployment_env_example(base_dir: Path | None = None) -> list[str]:
    """Return problem descriptions; empty means `deploy/staging.env.example` is safe
    and in step with the settings.

    Every value is a placeholder, blank, or a non-secret default of an approved
    name; every name is read by the settings (or the HTTP entry point); every
    required name is present. Never prints a value.
    """
    base_dir = base_dir or BASE_DIR
    path = base_dir / DEPLOYMENT_ENV_EXAMPLE
    if not path.is_file():
        return [f"{DEPLOYMENT_ENV_EXAMPLE.as_posix()} is missing"]
    known_sources = "\n".join(
        candidate.read_text(encoding="utf-8")
        for candidate in [
            *sorted((base_dir / "config" / "settings").glob("*.py")),
            base_dir / "config" / "serve.py",
        ]
        if candidate.is_file()
    )
    label = DEPLOYMENT_ENV_EXAMPLE.as_posix()
    problems: list[str] = []
    names: set[str] = set()
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        name, value = name.strip(), value.strip()
        names.add(name)
        if f'"{name}"' not in known_sources and not name.endswith("_V1"):
            problems.append(f"{label} line {line_number}: {name!r} is not read by the settings")
        if not value or PLACEHOLDER_PATTERN.match(value):
            continue
        if name in _DEPLOYMENT_EXAMPLE_NON_SECRET_NAMES:
            continue
        problems.append(f"{label} line {line_number}: {name!r} has a non-placeholder value")
    for required in _DEPLOYMENT_REQUIRED_NAMES:
        if required not in names:
            problems.append(f"{DEPLOYMENT_ENV_EXAMPLE.as_posix()} does not list {required}")
    return problems


def cmd_local_safety(_args: argparse.Namespace) -> int:
    problems = [
        *check_gitignore_text_contract(),
        *check_env_file_is_excluded(),
        *check_env_example_placeholders_only(),
        *check_deployment_env_example(),
    ]
    if problems:
        print("LOCAL SAFETY CONTRACT FAILED:")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print(
        "local safety contract OK: .gitignore retains required rules; "
        ".env exists locally and is excluded from every project scan; "
        ".env.example holds placeholders only. (No Git command was used.)"
    )
    return 0


# ---------------------------------------------------------------------------
# Accidental-credential scan (developer execution-control decision).
# Shape-based for connection strings/tokens/AWS keys/private-key blocks;
# NAME-based (via apps.core.credential_shapes, shared with the log-
# redaction layer so the two can never drift apart -- Prompt 2 correction
# §2/§5) for the generic "credential name = hard-coded value" case, which
# must cover every prefixed/suffixed identifier the project actually uses
# (`DATABASE_PASSWORD`, `S3_STORAGE_SECRET_ACCESS_KEY`,
# `RATE_LIMIT_HMAC_KEY_V<n>`, ...), not just bare words like "secret".
# Complements, does not replace, check_env_example_placeholders_only() above.
# ---------------------------------------------------------------------------

_SIMPLE_CREDENTIAL_SHAPE_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"postgres(?:ql)?://[^:/@\s]+:[^@/\s]+@"),
        "embedded PostgreSQL connection password",
    ),
    (re.compile(r"rediss?://(?:[^:@/\s]*:)?[^@/\s]+@"), "embedded Redis connection password"),
    (re.compile(r"(?i)Authorization:\s*Bearer\s+[A-Za-z0-9\-_.]{8,}"), "Bearer token"),
    (re.compile(r"\b(AKIA|ASIA)[0-9A-Z]{16}\b"), "AWS access key ID"),
    (re.compile(r"-----BEGIN (RSA |EC |OPENSSH |DSA |)PRIVATE KEY-----"), "private key block"),
)

# Pattern A: a QUOTED assignment, or a structured/dict/JSON key -- value
# pair. Requires a MANDATORY quote around the value
# (`name = "..."` / `name: '...'` / `"name": "..."`). This is what an
# accidentally pasted real secret looks like in source, and it structurally
# excludes ordinary code such as `password = os.environ[...]` or
# `SECRET_KEY = env("SECRET_KEY")`, which have no opening quote right after
# `=`/`:`. An optional closing quote is allowed between the name and the
# separator so `"secret_key": "..."` is caught the same way a plain
# assignment is. The captured name is validated SEMANTICALLY afterward via
# `is_credential_shaped_name()` -- this regex only proposes candidates.
#
# The value class is `[^"']` (quotes only) rather than `[^\s"']` -- a
# QUOTED value's own closing quote is what bounds the match, so a
# passphrase containing spaces (`"correct horse battery staple"`) is
# still safely and precisely delimited (Prompt 2 final closure pass §5).
_QUOTED_ASSIGNMENT_PATTERN = re.compile(
    rf"(?<![A-Za-z0-9_])({NAME_TOKEN_PATTERN})"
    r'(["\']?\s*[=:]\s*)(["\'])(?!__)([^"\']{6,})\3'
)

# Pattern B: an unquoted YAML-style scalar assignment with no quotes at
# all -- `name: value` -- colon only, deliberately NOT also `name = value`
# / `name=value`: an `=`-based unquoted separator turns out to match far
# too much ordinary prose in comments and docstrings that merely mention
# a `name=value` shape in passing (e.g. this very module's own docstrings
# discussing `password=` as an example), which quoted assignments
# (Pattern A) and the colon-based YAML shape do not suffer from nearly as
# often. The raw rest-of-line is captured broadly (spaces included, so a
# passphrase like
# `correct horse battery staple` is captured whole) with NO restrictive
# lookahead baked into the regex itself -- an earlier version tried to
# reject quoted/placeholder/call-shaped values with lookaheads positioned
# right after a backtrackable `\s*`, which the regex engine could defeat
# by backtracking that `\s*` to zero width, sliding the lookahead's check
# position onto the whitespace itself (where it trivially passes) instead
# of the real value that follows -- silently reintroducing exactly the
# `SECRET_KEY = env("SECRET_KEY")` false positive this scanner must never
# produce. All of that filtering is instead done in Python by
# `_is_rejectable_unquoted_value()`, below, against the final
# comment-stripped, trimmed value string, where there is no backtracking
# to defeat it.
_UNQUOTED_VALUE_PATTERN = re.compile(rf"(?<![A-Za-z0-9_])({NAME_TOKEN_PATTERN})\s*:\s*(.+)$")

# A code-shaped call or subscript at the START of the value -- `foo(`,
# `foo.bar(`, `foo[` -- indicates a lookup expression such as
# `env("NAME")` or `os.environ["NAME"]`, never a hard-coded literal.
_CALL_OR_SUBSCRIPT_VALUE_PATTERN = re.compile(r"^[\w.]*[(\[]")


def _strip_yaml_trailing_comment(raw_value: str) -> str:
    """Return `raw_value` with a trailing `# comment` removed, if present.

    A `#` preceded by whitespace starts a trailing comment in the simple
    unquoted config shapes this scanner targets; a `#` with no preceding
    whitespace is treated as part of the value itself (conservative: a
    value that happens to contain `#` is still detected in full, at the
    cost of occasionally keeping a comment that abuts the value with no
    separating space -- the safer failure direction for a credential
    scanner).
    """
    comment_start = re.search(r"\s#", raw_value)
    if comment_start:
        return raw_value[: comment_start.start()]
    return raw_value


def _is_rejectable_unquoted_value(value: str) -> bool:
    """True if a trimmed, comment-stripped unquoted `value` must be ignored.

    Rejects: an empty value; a value that is itself quoted (Pattern A
    already handles quoted values -- this is Pattern B's own capture
    incidentally including the quotes because its regex has no quote
    lookahead, per the note on `_UNQUOTED_VALUE_PATTERN` above); an
    approved placeholder (`__..._..._..__`); a code-shaped call or
    subscript lookup; a value containing `=` (a Python type-annotated
    declaration with a default, e.g. `database_password: str | None =
    None`, has exactly this `name: <type> = <default>` shape and must
    never be mistaken for a YAML-style hard-coded scalar -- a genuine
    YAML/config passphrase essentially never contains a literal `=`); or
    a value shorter than the minimum credential length.
    """
    if not value:
        return True
    if value[0] in "\"'":
        return True
    if value.startswith("__"):
        return True
    if "=" in value:
        return True
    if _CALL_OR_SUBSCRIPT_VALUE_PATTERN.match(value):
        return True
    return len(value) < 6


def _scan_pattern_for_credential_names(
    line: str, pattern: re.Pattern[str], value_group: int
) -> list[str]:
    """Return the raw value text for every credential-NAME-shaped match.

    Deliberately NOT a plain `pattern.finditer()`: both `_UNQUOTED_VALUE_
    PATTERN` (whose value runs to end-of-line) and, less commonly,
    `_QUOTED_ASSIGNMENT_PATTERN` can match a NON-credential-shaped false
    name (e.g. a false name like "see docs" immediately followed by a
    colon and then a genuine secret assignment) whose greedy value then
    swallows a REAL credential that follows it, hiding it from ever being
    matched on its own -- the same class of bug documented and fixed for
    the log redactor in
    `apps.core.redaction._redact_generic_credential_assignments`. On a
    rejected (non-credential-shaped) name, this only skips past the
    rejected NAME token itself and retries from there, so a genuine
    credential immediately after is still found.
    """
    values: list[str] = []
    pos = 0
    line_length = len(line)
    while pos <= line_length:
        match = pattern.search(line, pos)
        if match is None:
            break
        name = match.group(1)
        if is_credential_shaped_name(name):
            values.append(match.group(value_group))
            pos = match.end()
        else:
            pos = match.end(1)
    return values


def _find_generic_credential_findings(line: str) -> list[str]:
    """Return descriptions for every credential-NAME-shaped match on `line`.

    Tries both the quoted-assignment/structured shape and the unquoted
    (YAML- or shell-style) shape; each CANDIDATE name is validated with
    `is_credential_shaped_name()` (shared with the redaction layer) before
    being reported, so a name like "the value" or "some sentence" is
    proposed by the permissive regex but then correctly rejected. Both
    shapes detect values containing spaces (Prompt 2 final closure pass §5).
    """
    findings: list[str] = []
    for value in _scan_pattern_for_credential_names(line, _QUOTED_ASSIGNMENT_PATTERN, 4):
        if value in KNOWN_SYNTHETIC_CREDENTIAL_VALUES:
            continue
        findings.append("generic key=value secret-shaped assignment")
    for raw_value in _scan_pattern_for_credential_names(line, _UNQUOTED_VALUE_PATTERN, 2):
        value = _strip_yaml_trailing_comment(raw_value).strip()
        if _is_rejectable_unquoted_value(value):
            continue
        if value in KNOWN_SYNTHETIC_CREDENTIAL_VALUES:
            continue
        findings.append("generic key: value secret-shaped assignment (YAML-style)")
    return findings


# Inline suppression marker, in the same spirit as a linter's own inline
# suppression comment. Reserved EXCLUSIVELY for lines in the approved test
# files in `MARKER_ALLOWED_RELATIVE_FILES` below that deliberately
# construct a synthetic, credential-SHAPED string to test the scanner or
# the log-redaction logic itself -- never to hide a genuine finding
# anywhere else. `check_unauthorized_suppression_markers()` reports an
# error if this text appears in any other project file (Prompt 2
# correction §4).
SECRET_SCAN_ALLOW_MARKER = "# secret-scan: allow"  # noqa: S105

# This module's own canonical definition of the marker text necessarily
# contains that exact string as DATA (the line immediately above), not as
# an actual suppression comment -- excluded here by exact path so
# `check_unauthorized_suppression_markers()` does not flag its own
# defining module. No other line in this file may use the marker; nothing
# else here needs to.
_MARKER_DEFINITION_RELATIVE_FILE = "scripts/check.py"

# The ONLY files the marker may appear in. Deliberately an explicit,
# reviewable allowlist rather than "anywhere under tests/" -- every entry
# here is a scanner or log-redaction test file whose entire purpose
# includes constructing synthetic credential-shaped fixtures.
MARKER_ALLOWED_RELATIVE_FILES: frozenset[str] = frozenset(
    {
        "tests/foundation/test_secret_scan.py",
        "tests/foundation/test_redaction.py",
        "tests/foundation/test_logging_config.py",
        "tests/foundation/test_s3_storage.py",
        "tests/foundation/test_health_endpoints.py",
        "tests/foundation/test_private_storage.py",
        "tests/foundation/test_static_validation.py",
    }
)


def check_unauthorized_suppression_markers(base_dir: Path | None = None) -> list[str]:
    """Return findings for every use of the suppression marker OUTSIDE the
    approved test files (Prompt 2 correction §4).

    The marker must never silently suppress a finding in application code,
    settings, documentation, or any ordinary project file -- only in the
    small, explicit set of scanner/redaction test fixtures it exists for.
    """
    base_dir = base_dir or BASE_DIR
    findings: list[str] = []
    for path in iter_project_files(base_dir):
        relative = path.relative_to(base_dir)
        relative_posix = str(relative).replace(os.sep, "/")
        if relative_posix in MARKER_ALLOWED_RELATIVE_FILES:
            continue
        if relative_posix == _MARKER_DEFINITION_RELATIVE_FILE:
            continue
        try:
            lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            continue
        for line_number, line in enumerate(lines, start=1):
            if SECRET_SCAN_ALLOW_MARKER in line:
                findings.append(
                    f"{relative}:{line_number}: unauthorized secret-scan suppression "
                    "marker outside the approved test files"
                )
    return findings


def check_no_leaked_credentials(base_dir: Path | None = None) -> list[str]:
    """Return findings; an empty list means no credential-shaped content was found.

    Scans every file `iter_project_files()` yields (which already excludes
    `.env`, every other `.env.*` file, `var/`, `.venv/`, generated
    artifacts, and vendored/minified assets) whose suffix looks like text
    source, line by line. A line carrying `SECRET_SCAN_ALLOW_MARKER` is
    skipped -- and ONLY that exact line: the marker does NOT reach the
    line before or after it (Prompt 2 correction §3, v3), so a marker
    placed to suppress one synthetic fixture line can never accidentally
    suppress an unrelated adjacent line -- but ONLY when that marker usage
    is itself authorized (see `check_unauthorized_suppression_markers()`,
    which is run separately and independently reports a marker found
    anywhere else as its own finding, so an unauthorized marker can never
    silently suppress a real credential here). Never reads or reports any
    excluded env file's content -- it is excluded from the walk entirely,
    not merely filtered after reading.
    """
    base_dir = base_dir or BASE_DIR
    findings: list[str] = []
    for path in iter_project_files(base_dir):
        if path.suffix.lower() not in _TEXT_SCAN_SUFFIXES:
            continue
        relative = path.relative_to(base_dir)
        relative_posix = str(relative).replace(os.sep, "/")
        marker_is_authorized = relative_posix in MARKER_ALLOWED_RELATIVE_FILES
        try:
            lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            continue
        for line_number, line in enumerate(lines, start=1):
            suppressed = marker_is_authorized and SECRET_SCAN_ALLOW_MARKER in line
            if suppressed:
                continue
            for pattern, description in _SIMPLE_CREDENTIAL_SHAPE_PATTERNS:
                if pattern.search(line):
                    findings.append(f"{relative}:{line_number}: possible {description}")
            for description in _find_generic_credential_findings(line):
                findings.append(f"{relative}:{line_number}: possible {description}")
    return findings


_TEXT_SCAN_SUFFIXES: frozenset[str] = frozenset(
    {
        ".py",
        ".md",
        ".toml",
        ".txt",
        ".cfg",
        ".ini",
        ".json",
        ".yml",
        ".yaml",
        ".html",
        ".css",
        ".js",
        "",  # "" matches extensionless files, e.g. .python-version
    }
)


def cmd_secrets(_args: argparse.Namespace) -> int:
    findings = check_no_leaked_credentials()
    marker_problems = check_unauthorized_suppression_markers()
    if findings or marker_problems:
        if findings:
            print("POSSIBLE LEAKED CREDENTIALS FOUND:")
            for finding in findings:
                print(f"  - {finding}")
        if marker_problems:
            print("UNAUTHORIZED SUPPRESSION MARKER USAGE FOUND:")
            for problem in marker_problems:
                print(f"  - {problem}")
        return 1
    print(
        "credential scan OK: no leaked-credential shapes found in project-controlled "
        "files (.env, every other .env.* file, var/, .venv/, generated artifacts, and "
        "vendored assets excluded by construction); every suppression-marker use is "
        "authorized."
    )
    return 0


# ---------------------------------------------------------------------------
# SHA-256 manifest of project-owned files (replaces `git status`/`git diff`
# as the file-inventory mechanism, per developer execution-control decision).
# ---------------------------------------------------------------------------


def compute_project_manifest(base_dir: Path | None = None) -> dict[str, str]:
    """Return {relative_path: sha256_hexdigest} for every project-owned file.

    Excludes `.env`, `var/`, `.venv/`, and generated artifacts throughout.
    Vendored compiled/minified assets are included even though their hashes
    are also recorded in `PROVENANCE.md`. This manifest doubles as the exact
    review-archive inventory, and omitting those runtime files would make an
    extracted review archive incomplete.
    """
    base_dir = base_dir or BASE_DIR
    manifest: dict[str, str] = {}
    for path in iter_manifest_files(base_dir):
        relative = str(path.relative_to(base_dir)).replace(os.sep, "/")
        manifest[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return manifest


def cmd_manifest(_args: argparse.Namespace) -> int:
    manifest = compute_project_manifest()
    for relative_path, digest in manifest.items():
        print(f"{digest}  {relative_path}")
    print(f"\n{len(manifest)} file(s) manifested.", file=sys.stderr)
    return 0


# ---------------------------------------------------------------------------
# Vendored-asset checksum verification (accepted plan §5.3)
# ---------------------------------------------------------------------------

_CHECKSUM_TABLE_ROW = re.compile(r"^\|\s*`([^`]+)`\s*\|\s*`([0-9a-f]{64})`\s*\|\s*$")

VENDOR_DIRS = [
    BASE_DIR / "static" / "vendor" / "bootstrap",
    BASE_DIR / "static" / "vendor" / "htmx",
    # Arabic interface font (Phase 3 Prompt 5; developer-held extended license).
    BASE_DIR / "static" / "vendor" / "thmanyah",
    # ALTCHA widget, self-hosted (UX-C2, owner decision UX-D01 option M).
    BASE_DIR / "static" / "vendor" / "altcha",
]


def _parse_provenance_checksums(provenance_path: Path) -> dict[str, str]:
    checksums: dict[str, str] = {}
    for line in provenance_path.read_text(encoding="utf-8").splitlines():
        match = _CHECKSUM_TABLE_ROW.match(line.strip())
        if match:
            filename, digest = match.groups()
            checksums[filename] = digest
    return checksums


def check_vendored_asset_checksums(vendor_dirs: list[Path] | None = None) -> list[str]:
    """Return problem descriptions; an empty list means every listed asset
    exists with a matching checksum AND no unexpected extra file exists.

    `PROVENANCE.md` itself, plus every filename in its own checksum
    table, form the COMPLETE, exact allowlist for each vendor directory
    (Prompt 2 final closure pass §9) -- this makes the directory a closed
    set rather than a checklist that only ever grows: a file added there
    without a corresponding `PROVENANCE.md` entry is itself a finding,
    not silently ignored.
    """
    vendor_dirs = vendor_dirs if vendor_dirs is not None else VENDOR_DIRS
    problems: list[str] = []
    for vendor_dir in vendor_dirs:
        provenance_path = vendor_dir / "PROVENANCE.md"
        if not provenance_path.is_file():
            problems.append(f"{vendor_dir}: missing PROVENANCE.md")
            continue
        expected = _parse_provenance_checksums(provenance_path)
        if not expected:
            problems.append(f"{provenance_path}: no checksum rows found")
            continue
        for filename, expected_digest in expected.items():
            file_path = vendor_dir / filename
            if not file_path.is_file():
                problems.append(f"{file_path}: file listed in PROVENANCE.md is missing")
                continue
            actual_digest = hashlib.sha256(file_path.read_bytes()).hexdigest()
            if actual_digest != expected_digest:
                problems.append(
                    f"{file_path}: checksum mismatch "
                    f"(expected {expected_digest}, got {actual_digest})"
                )

        allowed_names = set(expected.keys()) | {"PROVENANCE.md"}
        actual_names = {entry.name for entry in vendor_dir.iterdir() if entry.is_file()}
        for unexpected_name in sorted(actual_names - allowed_names):
            problems.append(
                f"{vendor_dir / unexpected_name}: unexpected file not listed in "
                "PROVENANCE.md's exact allowlist"
            )
    return problems


def cmd_assets(_args: argparse.Namespace) -> int:
    problems = check_vendored_asset_checksums()
    if problems:
        print("VENDORED ASSET CHECKSUM CHECK FAILED:")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print("vendored asset checksums OK (Bootstrap, htmx, Thmanyah Sans, ALTCHA).")
    return 0


# ---------------------------------------------------------------------------
# Read-only PostgreSQL 17 probe (accepted plan §5.10). Credentials redacted;
# no table is created; PostgreSQL 16 is rejected if somehow reached.
# ---------------------------------------------------------------------------

_REQUIRED_DB_VARS = (
    "DATABASE_HOST",
    "DATABASE_PORT",
    "DATABASE_NAME",
    "DATABASE_USER",
    "DATABASE_PASSWORD",
)

# The approved Phase 1 local database identity. A successful CONNECTION
# alone is not sufficient evidence: `.env` could be misconfigured to point
# at some other database that happens to exist and accept the connection.
# Comparing the server's own answers against these fixed, approved values
# is what actually catches that (accepted plan §5.10). Never a secret.
EXPECTED_DATABASE_NAME = "asc2026_dev"
EXPECTED_DATABASE_USER = "asc2026_app"
EXPECTED_DATABASE_ENCODING = "UTF8"


def evaluate_db_probe_report(report: dict[str, str]) -> list[str]:
    """Return problem descriptions for an otherwise-successful probe report.

    Pure and side-effect-free: never opens a connection, never receives a
    password or connection URL -- `report` only ever contains the non-secret
    fields `run_db_probe()` builds. Kept separate from `run_db_probe()`
    precisely so it is directly unit-testable without a real database.
    """
    problems: list[str] = []
    server_version = report.get("server_version", "")
    if "PostgreSQL 17" not in server_version:
        problems.append(
            "server is not PostgreSQL 17.x (accepted plan §5.10); reports "
            f"{server_version!r}. PostgreSQL 16 never satisfies this baseline."
        )
    if report.get("database") != EXPECTED_DATABASE_NAME:
        problems.append(
            f"connected database {report.get('database')!r} does not match "
            f"the approved Phase 1 local database {EXPECTED_DATABASE_NAME!r}."
        )
    if report.get("role") != EXPECTED_DATABASE_USER:
        problems.append(
            f"connected role {report.get('role')!r} does not match the "
            f"approved Phase 1 local runtime role {EXPECTED_DATABASE_USER!r}."
        )
    if report.get("encoding") != EXPECTED_DATABASE_ENCODING:
        problems.append(
            f"database encoding {report.get('encoding')!r} does not match "
            f"the required {EXPECTED_DATABASE_ENCODING!r}."
        )
    return problems


def run_db_probe() -> tuple[int, dict[str, str]]:
    """Connect read-only and report {server_version, host, port, database, role, encoding}.

    Returns (exit_code, report). `report` never contains a password or a
    connection URL. Exit code 2 means required configuration is missing;
    1 means the connection failed, the driver is missing, or the server
    fails `evaluate_db_probe_report()` (wrong major version, database name,
    runtime user, or encoding); 0 means success.
    """
    missing = [name for name in _REQUIRED_DB_VARS if not os.environ.get(name)]
    if missing:
        return 2, {"error": f"missing required environment variable(s): {', '.join(missing)}"}

    host = os.environ["DATABASE_HOST"]
    port = os.environ["DATABASE_PORT"]
    name = os.environ["DATABASE_NAME"]
    user = os.environ["DATABASE_USER"]
    password = os.environ["DATABASE_PASSWORD"]

    try:
        import psycopg
    except ImportError:
        return 2, {"error": "psycopg is not installed in this environment"}

    try:
        connection = psycopg.connect(
            host=host, port=port, dbname=name, user=user, password=password, connect_timeout=5
        )
    except Exception:
        # The driver's own exception text can embed the DSN. Never surface it.
        return 1, {
            "error": "connection failed",
            "host": host,
            "port": port,
            "database": name,
            "role": user,
        }

    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT version(), current_database(), current_user, "
                "(SELECT pg_encoding_to_char(encoding) FROM pg_database "
                " WHERE datname = current_database())"
            )
            version, dbname, dbuser, encoding = cursor.fetchone()
    finally:
        connection.close()

    server_version_short = version.split(",")[0]
    report = {
        "server_version": server_version_short,
        "host": host,
        "port": port,
        "database": dbname,
        "role": dbuser,
        "encoding": encoding,
    }
    problems = evaluate_db_probe_report(report)
    if problems:
        report["error"] = "; ".join(problems)
        return 1, report
    return 0, report


def cmd_db_probe(_args: argparse.Namespace) -> int:
    exit_code, report = run_db_probe()
    if exit_code != 0:
        print(f"db-probe: FAILED - {report.get('error', 'unknown error')}")
        for key, value in report.items():
            if key != "error":
                print(f"  {key:14s}: {value}")
        return exit_code
    print("db-probe: OK")
    for key, value in report.items():
        print(f"  {key:14s}: {value}")
    return 0


# ---------------------------------------------------------------------------
# Prompt 3 CP 3a: read-only pre-flight database-state gate (accepted plan
# §6.2, ADR-0011). Never creates a table (not even `django_migrations`);
# never reports a credential or connection URL.
# ---------------------------------------------------------------------------


def run_db_state() -> tuple[int, dict[str, object]]:
    """Report sanitized database state. Never creates a table or reports a secret.

    Returns (exit_code, report). `report` never contains a password or
    connection URL. exit code 2 = missing configuration/driver; 1 = connection
    failed, OR the database is not in the clean pre-Prompt-3 state (an
    unexpected applied migration or an unexpected table exists); 0 = clean.
    """
    missing = [name for name in _REQUIRED_DB_VARS if not os.environ.get(name)]
    if missing:
        return 2, {"error": f"missing required environment variable(s): {', '.join(missing)}"}

    host = os.environ["DATABASE_HOST"]
    port = os.environ["DATABASE_PORT"]
    name = os.environ["DATABASE_NAME"]
    user = os.environ["DATABASE_USER"]
    password = os.environ["DATABASE_PASSWORD"]

    try:
        import psycopg
    except ImportError:
        return 2, {"error": "psycopg is not installed in this environment"}

    try:
        connection = psycopg.connect(
            host=host, port=port, dbname=name, user=user, password=password, connect_timeout=5
        )
    except Exception:
        return 1, {
            "error": "connection failed",
            "host": host,
            "port": port,
            "database": name,
            "role": user,
        }

    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT version(), current_database(), current_user, "
                "(SELECT pg_encoding_to_char(encoding) FROM pg_database "
                " WHERE datname = current_database())"
            )
            version, dbname, dbuser, encoding = cursor.fetchone()

            # Read-only existence check -- `to_regclass` never creates anything.
            cursor.execute("SELECT to_regclass('public.django_migrations') IS NOT NULL")
            (migrations_table_exists,) = cursor.fetchone()

            applied_migrations: list[str] = []
            if migrations_table_exists:
                cursor.execute("SELECT app, name FROM django_migrations ORDER BY id")
                applied_migrations = [f"{app}.{mig_name}" for app, mig_name in cursor.fetchall()]

            cursor.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public' ORDER BY table_name"
            )
            existing_tables = [row[0] for row in cursor.fetchall()]
    finally:
        connection.close()

    is_clean = not migrations_table_exists and not existing_tables
    report: dict[str, object] = {
        "database": dbname,
        "host": host,
        "port": port,
        "server_version": version.split(",")[0],
        "role": dbuser,
        "encoding": encoding,
        "django_migrations_table_exists": migrations_table_exists,
        "applied_migrations": applied_migrations,
        "existing_tables": existing_tables,
        "is_clean_pre_prompt3_state": is_clean,
    }
    return (0 if is_clean else 1), report


def cmd_db_state(_args: argparse.Namespace) -> int:
    exit_code, report = run_db_state()
    if "error" in report:
        print(f"db-state: FAILED - {report['error']}")
        for key, value in report.items():
            if key != "error":
                print(f"  {key:32s}: {value}")
        return exit_code
    print(f"db-state: {'CLEAN' if report['is_clean_pre_prompt3_state'] else 'NOT CLEAN'}")
    for key, value in report.items():
        print(f"  {key:32s}: {value}")
    if not report["is_clean_pre_prompt3_state"]:
        print(
            "\nBLOCKER: an unexpected applied migration or table exists. Stop; "
            "do not continue with model generation or migration application. "
            "No corrective or destructive action is taken automatically."
        )
    return exit_code


# ---------------------------------------------------------------------------
# Prompt 3 CP 3b / deployment-team post-migration check (ADR-0008): the
# honest two-level audit append-only contract. Connects with the SAME
# credentials the application runs with (locally, the single owning role) --
# this command's entire point is to report what those credentials can and
# cannot do, never to escalate privilege to check something else.
# ---------------------------------------------------------------------------

AUDIT_EVENT_TABLE = "audit_event"
AUDIT_EVENT_TRIGGER = "audit_event_prevent_update_delete"


def run_audit_isolation_check() -> tuple[int, dict[str, object]]:
    """Report the two-level audit append-only contract (ADR-0008), honestly.

    Fact A: the local `BEFORE UPDATE OR DELETE` trigger exists and is
    enabled -- this CAN and MUST be satisfied locally.
    Fact B: the runtime role does not own the table and holds no
    UPDATE/DELETE/TRUNCATE/DDL authority on it (directly or via PUBLIC),
    holding only INSERT+SELECT -- this is NOT satisfied in the local
    single-owning-role setup, and is never reported as passing, waived, or
    proven by this command when it is not actually satisfied.

    Exit code 0 only when BOTH facts hold (a genuinely separated-role
    deployment). Locally this returns 1 -- a real, expected, documented
    failure of Fact B, never silently downgraded to a pass.
    """
    missing = [name for name in _REQUIRED_DB_VARS if not os.environ.get(name)]
    if missing:
        return 2, {"error": f"missing required environment variable(s): {', '.join(missing)}"}

    host = os.environ["DATABASE_HOST"]
    port = os.environ["DATABASE_PORT"]
    name = os.environ["DATABASE_NAME"]
    user = os.environ["DATABASE_USER"]
    password = os.environ["DATABASE_PASSWORD"]

    try:
        import psycopg
    except ImportError:
        return 2, {"error": "psycopg is not installed in this environment"}

    try:
        connection = psycopg.connect(
            host=host, port=port, dbname=name, user=user, password=password, connect_timeout=5
        )
    except Exception:
        return 1, {"error": "connection failed", "host": host, "port": port, "database": name}

    try:
        with connection.cursor() as cursor:
            cursor.execute(f"SELECT to_regclass('public.{AUDIT_EVENT_TABLE}') IS NOT NULL")
            (table_exists,) = cursor.fetchone()
            if not table_exists:
                return 1, {
                    "error": (
                        f"{AUDIT_EVENT_TABLE} table does not exist -- run this check only "
                        "after audit.0001 has been migrated"
                    )
                }

            # AUDIT_EVENT_TABLE is this module's own fixed constant, never
            # user input -- not a real injection vector.
            # AUDIT_EVENT_TABLE is this module's own fixed constant, never
            # user input -- not a real injection vector (noqa: S608 below).
            trigger_query = (
                "SELECT tgenabled FROM pg_trigger "  # noqa: S608
                f"WHERE tgrelid = '{AUDIT_EVENT_TABLE}'::regclass "
                "AND tgname = %s AND NOT tgisinternal"
            )
            cursor.execute(trigger_query, [AUDIT_EVENT_TRIGGER])
            trigger_row = cursor.fetchone()
            trigger_present = trigger_row is not None
            trigger_enabled = trigger_present and trigger_row[0] != "D"

            owner_query = (
                "SELECT pg_get_userbyid(relowner) FROM pg_class "  # noqa: S608
                f"WHERE oid = '{AUDIT_EVENT_TABLE}'::regclass"
            )
            cursor.execute(owner_query)
            (table_owner,) = cursor.fetchone()
            role_is_owner = table_owner == user

            cursor.execute(
                "SELECT DISTINCT privilege_type FROM information_schema.role_table_grants "
                "WHERE table_name = %s AND grantee IN (%s, 'PUBLIC')",
                [AUDIT_EVENT_TABLE, user],
            )
            grants = {row[0] for row in cursor.fetchall()}
    finally:
        connection.close()

    forbidden_grants = grants & {"UPDATE", "DELETE", "TRUNCATE"}
    has_required_grants = {"INSERT", "SELECT"} <= grants
    role_isolation_satisfied = not role_is_owner and not forbidden_grants and has_required_grants
    trigger_ok = trigger_present and trigger_enabled

    report: dict[str, object] = {
        "trigger_present": trigger_present,
        "trigger_enabled": trigger_enabled,
        "trigger_ok__local_layer": trigger_ok,
        "runtime_role": user,
        "table_owner": table_owner,
        "role_is_table_owner": role_is_owner,
        "runtime_role_privileges_on_audit_event": sorted(grants),
        "forbidden_privileges_held": sorted(forbidden_grants),
        "role_isolation_satisfied__deployment_layer": role_isolation_satisfied,
    }
    return (0 if (trigger_ok and role_isolation_satisfied) else 1), report


def cmd_audit_isolation(_args: argparse.Namespace) -> int:
    exit_code, report = run_audit_isolation_check()
    if "error" in report:
        print(f"audit-isolation: FAILED - {report['error']}")
        return exit_code
    print("audit-isolation (ADR-0008 two-level contract):")
    for key, value in report.items():
        print(f"  {key:42s}: {value}")
    print()
    if report["trigger_ok__local_layer"]:
        print(
            "  Fact A (local trigger): SATISFIED -- the append-only trigger is present and enabled."
        )
    else:
        print("  Fact A (local trigger): NOT SATISFIED.")
    if report["role_isolation_satisfied__deployment_layer"]:
        print("  Fact B (deployment role isolation): SATISFIED.")
    else:
        print(
            "  Fact B (deployment role isolation): NOT SATISFIED -- expected in the local "
            "single-owning-role setup (ADR-0008). This is never a pass, waiver, or proof of "
            "real database-role privilege isolation; only a genuine post-migration run "
            "against the deployment team's separated migration-owner and restricted "
            "runtime roles proves that."
        )
    return exit_code


# ---------------------------------------------------------------------------
# Static deploy checks (accepted plan §5.6 A, §9). Configuration-only:
# proves NOTHING about real PostgreSQL ownership or grants.
# ---------------------------------------------------------------------------

SYNTHETIC_DEPLOY_ENV: dict[str, str] = {
    "DJANGO_SETTINGS_MODULE": "",  # overridden per-invocation via --settings
    "DJANGO_SECRET_KEY": "synthetic-deploy-check-secret-key-never-used-for-real-traffic",
    "DJANGO_ALLOWED_HOSTS": "deploy-check.example.invalid",
    "DATABASE_HOST": "db.deploy-check.example.invalid",
    "DATABASE_PORT": "5432",
    "DATABASE_NAME": "synthetic_deploy_check_db",
    "DATABASE_USER": "synthetic_runtime_role",
    "DATABASE_PASSWORD": "synthetic-not-a-real-password",
    "DATABASE_MIGRATION_USER": "synthetic_migration_owner_role",
    "REDIS_URL": "rediss://synthetic.deploy-check.example.invalid:6379/0",
    "CELERY_BROKER_URL": "rediss://synthetic.deploy-check.example.invalid:6379/1",
    "MFA_BACKEND": "synthetic.mfa.DeployCheckBackend",
    "MALWARE_SCANNER_BACKEND": "apps.documents.scanning.ClamdScanner",
    "CLAMD_HOST": "clamd.deploy-check.example.invalid",
    "EMAIL_HOST": "smtp.deploy-check.example.invalid",
    "EMAIL_PORT": "587",
    "EMAIL_USE_TLS": "True",
    "DEFAULT_FROM_EMAIL": "ASC 2026 <no-reply@deploy-check.example.invalid>",
    "TRUSTED_PROXY_FORWARDING_ENABLED": "False",
    "TRUSTED_PROXY_CIDRS": "",
    "RATE_LIMIT_HMAC_ACTIVE_VERSIONS": "1",
    "RATE_LIMIT_HMAC_WRITE_VERSION": "1",
    "RATE_LIMIT_HMAC_KEY_V1": "synthetic-not-a-real-hmac-key",
    "RATE_LIMIT_KEY_OVERLAP_SECONDS": "3600",
    "IDENTITY_ENCRYPTION_ACTIVE_VERSIONS": "1",
    "IDENTITY_ENCRYPTION_WRITE_VERSION": "1",
    "IDENTITY_ENCRYPTION_KEY_V1": "synthetic-not-a-real-encryption-key",
    "IDENTITY_BLIND_INDEX_HMAC_ACTIVE_VERSIONS": "1",
    "IDENTITY_BLIND_INDEX_HMAC_WRITE_VERSION": "1",
    "IDENTITY_BLIND_INDEX_HMAC_KEY_V1": "synthetic-not-a-real-blind-index-key",
    "S3_STORAGE_BUCKET_NAME": "synthetic-deploy-check-bucket",
    "S3_STORAGE_ENDPOINT_URL": "https://s3.deploy-check.example.invalid",
    "S3_STORAGE_ACCESS_KEY_ID": "synthetic-not-a-real-access-key",
    "S3_STORAGE_SECRET_ACCESS_KEY": "synthetic-not-a-real-secret-key",
    "S3_STORAGE_REGION_NAME": "us-east-1",
    "DJANGO_PUBLIC_BASE_URL": "https://deploy-check.example.invalid",
}

# Every value above is a documented, non-secret, deploy-check-only sentinel
# (accepted plan §5.6 A / §9): never read from `.env`, never sent to a live
# service, never used for real traffic. Reviewed here by EXACT value, not
# by name or prefix, so the credential scanner (below) does not flag this
# module's own documented synthetic constants. This is a narrow, auditable
# exemption over a fixed set of specific known values, not a name- or
# prefix-based bypass -- a real secret cannot incidentally collide with one
# of these long, self-describing sentinel strings, and the exemption
# cannot be broadened without editing `SYNTHETIC_DEPLOY_ENV` itself, which
# is reviewed on every change.
#: Added only for the migration settings modules (`config.settings.*_migration`).
#: The runtime modules refuse to start when the migration-owner password is in
#: their environment, so it is never part of `SYNTHETIC_DEPLOY_ENV` itself.
SYNTHETIC_MIGRATION_ENV_EXTRA: dict[str, str] = {
    "DATABASE_MIGRATION_PASSWORD": "synthetic-not-a-real-password-2",
}

KNOWN_SYNTHETIC_CREDENTIAL_VALUES: frozenset[str] = frozenset(
    {*SYNTHETIC_DEPLOY_ENV.values(), *SYNTHETIC_MIGRATION_ENV_EXTRA.values()}
)

DEPLOY_CHECK_SETTINGS_MODULES: tuple[str, ...] = (
    "config.settings.staging",
    "config.settings.production",
    "config.settings.staging_migration",
    "config.settings.production_migration",
)


def synthetic_deploy_env(settings_module: str) -> dict[str, str]:
    """The synthetic, non-secret environment for one deployed settings module."""
    env = {**SYNTHETIC_DEPLOY_ENV, "DJANGO_SETTINGS_MODULE": settings_module}
    if settings_module.endswith("_migration"):
        env.update(SYNTHETIC_MIGRATION_ENV_EXTRA)
    return env


def run_static_deploy_check(settings_module: str) -> subprocess.CompletedProcess:
    """Run `manage.py check --deploy` with synthetic, non-secret env values.

    This never touches the real `.env` file and never connects to a live
    PostgreSQL, Redis, or provider service -- it validates configuration
    SHAPE only (accepted plan §5.6 A). It does not and cannot prove real
    database ownership or grants.
    """
    inherited = {
        key: value
        for key, value in os.environ.items()
        if key not in SYNTHETIC_DEPLOY_ENV and key not in SYNTHETIC_MIGRATION_ENV_EXTRA
    }
    env = {**inherited, **synthetic_deploy_env(settings_module)}
    return subprocess.run(
        [sys.executable, "manage.py", "check", "--deploy", f"--settings={settings_module}"],
        cwd=BASE_DIR,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def cmd_deploy(_args: argparse.Namespace) -> int:
    overall = 0
    for settings_module in DEPLOY_CHECK_SETTINGS_MODULES:
        result = run_static_deploy_check(settings_module)
        status = "OK" if result.returncode == 0 else "FAILED"
        print(
            f"deploy check [{settings_module}]: {status} (configuration-only; "
            f"proves nothing about real database ownership or grants)"
        )
        if result.stdout.strip():
            print(result.stdout.strip())
        if result.returncode != 0:
            if result.stderr.strip():
                print(result.stderr.strip())
            overall = 1
    return overall


# ---------------------------------------------------------------------------
# Runtime artifact: static build, manifest and read-only verification
# ---------------------------------------------------------------------------


def run_static_build(base_dir: Path | None = None) -> subprocess.CompletedProcess:
    """Run `collectstatic` for the deployed settings without any real secret.

    The staging settings select the manifest static storage and validate the
    whole configuration at import, so they need values; the documented
    synthetic ones are enough because the output depends only on the static
    sources. Bytecode is not written, so the build leaves only `staticfiles/`.
    """
    base_dir = base_dir or BASE_DIR
    inherited = {
        key: value
        for key, value in os.environ.items()
        if key not in SYNTHETIC_DEPLOY_ENV and key not in SYNTHETIC_MIGRATION_ENV_EXTRA
    }
    env = {
        **inherited,
        **synthetic_deploy_env("config.settings.staging"),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    return subprocess.run(
        [
            sys.executable,
            "manage.py",
            "collectstatic",
            "--noinput",
            "--settings=config.settings.staging",
        ],
        cwd=base_dir,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def cmd_build_static(_args: argparse.Namespace) -> int:
    result = run_static_build()
    output_lines = (result.stdout or "").strip().splitlines()
    print(f"build-static: {output_lines[-1] if output_lines else 'no output'}")
    if result.returncode != 0:
        print((result.stderr or "").strip()[-2000:])
    return result.returncode


def compute_artifact_manifest(root: Path) -> list[str]:
    """`<sha256>  <path>` for every file under `root`, sorted, POSIX paths.

    A symbolic link is recorded by its target text (`symlink:<target>`), never
    followed, so a virtual environment's interpreter link is covered too.
    """
    lines: list[str] = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            lines.append(f"symlink:{os.readlink(path)}  {relative}")
        elif path.is_file():
            digest = hashlib.sha256()
            with path.open("rb") as handle:
                for block in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(block)
            lines.append(f"{digest.hexdigest()}  {relative}")
    return lines


def artifact_digest(lines: list[str]) -> str:
    return hashlib.sha256(("\n".join(lines) + "\n").encode("utf-8")).hexdigest()


def _artifact_paths(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="scripts/check.py artifact-*")
    parser.add_argument("--root", required=True)
    parser.add_argument("--output")
    parser.add_argument("--manifest")
    return parser.parse_args(argv)


def cmd_artifact_manifest(args: argparse.Namespace) -> int:
    options = _artifact_paths(args.extra)
    root = Path(options.root).resolve()
    if not options.output:
        print("artifact-manifest: --output is required")
        return 2
    output = Path(options.output).resolve()
    if output == root or root in output.parents:
        print("artifact-manifest: --output must be outside the artifact")
        return 2
    if not root.is_dir():
        print("artifact-manifest: --root is not a directory")
        return 2
    lines = compute_artifact_manifest(root)
    output.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"artifact-manifest: {len(lines)} entries; artifact SHA-256 {artifact_digest(lines)}")
    return 0


def verify_artifact(root: Path, manifest: Path) -> dict[str, list[str]]:
    recorded = [line for line in manifest.read_text(encoding="utf-8").splitlines() if line]
    current = compute_artifact_manifest(root)
    recorded_map = {line.split("  ", 1)[1]: line for line in recorded}
    current_map = {line.split("  ", 1)[1]: line for line in current}
    return {
        "missing": sorted(set(recorded_map) - set(current_map)),
        "added": sorted(set(current_map) - set(recorded_map)),
        "changed": sorted(
            path
            for path in set(recorded_map) & set(current_map)
            if recorded_map[path] != current_map[path]
        ),
    }


def cmd_artifact_verify(args: argparse.Namespace) -> int:
    options = _artifact_paths(args.extra)
    if not options.manifest:
        print("artifact-verify: --manifest is required")
        return 2
    root = Path(options.root).resolve()
    differences = verify_artifact(root, Path(options.manifest))
    if any(differences.values()):
        for kind, paths in differences.items():
            for path in paths[:50]:
                print(f"artifact-verify: {kind}: {path}")
        print("artifact-verify: FAILED -- the artifact differs from its manifest")
        return 1
    lines = compute_artifact_manifest(root)
    print(f"artifact-verify: OK ({len(lines)} entries, artifact SHA-256 {artifact_digest(lines)})")
    return 0


# ---------------------------------------------------------------------------
# P4-4-C1 (R-03): release readiness, reported separately from the local gate
# ---------------------------------------------------------------------------

#: `manage.py release_readiness` exit code for each overall status.
RELEASE_READINESS_EXIT_CODES = {"READY": 0, "BLOCKED": 1, "NOT_ASSESSED": 2}


def run_release_readiness() -> tuple[int, dict[str, object]]:
    """Run `manage.py release_readiness --json` in a child process.

    Returns (exit_code, report). Anything other than a well-formed report
    from the command (a crash, unparsable output, a non-object report, a
    missing, non-string or unknown status, or an exit code that disagrees with
    the status) is NOT_ASSESSED, never READY.
    The child's stderr is not echoed: it could carry a driver message.
    """
    result = subprocess.run(
        [
            sys.executable,
            "manage.py",
            "release_readiness",
            "--json",
            "--settings=config.settings.local",
        ],
        cwd=BASE_DIR,
        capture_output=True,
        text=True,
        check=False,
    )
    expected = RELEASE_READINESS_EXIT_CODES
    try:
        report = json.loads(result.stdout)
        overall = report["overall"]
    except (ValueError, KeyError, TypeError):  # fmt: skip
        return 2, {"overall": "NOT_ASSESSED", "error": "the assessment produced no report"}
    # P4-4-C2: `overall` must be a string before it is used as a key; a list or
    # an object from the child is unhashable and used to raise TypeError here.
    if (
        not isinstance(overall, str)
        or overall not in expected
        or expected[overall] != result.returncode
    ):
        return 2, {"overall": "NOT_ASSESSED", "error": "the assessment result is inconsistent"}
    return result.returncode, report


def _print_release_readiness(report: dict[str, object]) -> None:
    print(f"  overall: {report.get('overall')}")
    if "error" in report:
        print(f"  {report['error']}")
    for item in report.get("items", []):
        print(f"  [{item['status']}] {item['key']} ({item['reference']})")
        for reason in item.get("reasons", []):
            print(f"      - {reason}")
    # P4-4-C3: the plain facts (for example, that MFA is not enforced at
    # sign-in under the revised MFA-01 requirement), printed only when well formed.
    facts = report.get("facts")
    if isinstance(facts, dict):
        for name, value in facts.items():
            print(f"  fact: {name} = {value}")
    if report.get("note"):
        print(f"  {report['note']}")


def cmd_release_readiness(_args: argparse.Namespace) -> int:
    exit_code, report = run_release_readiness()
    print("release-readiness (read-only; never a legal, security or go-live approval):")
    _print_release_readiness(report)
    for entry in report.get("not_covered", []):
        print(f"  not covered: {entry}")
    return exit_code


# ---------------------------------------------------------------------------
# Aggregate entrypoint
# ---------------------------------------------------------------------------


def _run(description: str, command: list[str], *, env: dict[str, str] | None = None) -> bool:
    print(f"--- {description} ---")
    result = subprocess.run(command, cwd=BASE_DIR, check=False, env=env)
    ok = result.returncode == 0
    print(f"--- {description}: {'OK' if ok else 'FAILED'} ---\n")
    return ok


def _run_all(_args: argparse.Namespace, *, require_db: bool) -> int:
    """Shared implementation for `all` (require_db=True) and the explicitly
    named, DB-optional developer command `all-no-db` (require_db=False).

    `require_db=False` is a convenience for local iteration when the
    database is temporarily unreachable. It is NEVER the full Prompt 2
    acceptance check (Prompt 2 correction §4) -- `cmd_all_no_db` prints a
    prominent statement to that effect and the summary repeats it.
    """
    results: dict[str, bool] = {}

    results["ruff format --check"] = _run(
        "ruff format --check", [sys.executable, "-m", "ruff", "format", "--check", "."]
    )
    results["ruff check"] = _run("ruff check", [sys.executable, "-m", "ruff", "check", "."])
    results["local safety contract"] = not (
        check_gitignore_text_contract()
        + check_env_file_is_excluded()
        + check_env_example_placeholders_only()
        + check_deployment_env_example()
    )
    results["credential scan"] = not check_no_leaked_credentials()
    results["vendored asset checksums"] = not check_vendored_asset_checksums()

    if require_db:
        db_state_exit_code, db_state_report = run_db_state()
        db_state_label = "CLEAN/OK" if db_state_exit_code == 0 else "REPORTED (see detail)"
        print(f"--- db-state: {db_state_label} ---")
        for key, value in db_state_report.items():
            print(f"  {key:32s}: {value}")
        print()
        # Prompt 3+: a clean pre-migration state is no longer the expected
        # steady state once migrations have been applied in this environment
        # (e.g. after `manage.py migrate` has run once, `all` is re-run for
        # ordinary regression checking) -- so this is reported for visibility,
        # not gated into the aggregate pass/fail the way the Prompt 2 db-probe
        # is. A connection failure or missing configuration still counts.
        results["db-state (reachable)"] = "error" not in db_state_report

    if require_db:
        # The real local PostgreSQL 17 probe is REQUIRED -- missing
        # configuration, a failed connection, the wrong server major
        # version, and the wrong database name/runtime user/encoding are
        # all failures (Prompt 2 correction §4). This is the full Prompt 2
        # acceptance check; it must not report success while this could
        # not actually run.
        db_exit_code, db_report = run_db_probe()
        print(f"--- db-probe: {'OK' if db_exit_code == 0 else 'FAILED'} ---")
        for key, value in db_report.items():
            print(f"  {key:14s}: {value}")
        print()
        results["db-probe"] = db_exit_code == 0
    else:
        print(
            "--- db-probe: NOT RUN (all-no-db is a DB-optional developer "
            "convenience, never the Prompt 2 acceptance check) ---\n"
        )

    # P4-4-C1 (R-03): with the database, the database-tagged checks run too,
    # so the privacy.W001-W004 release warnings are shown. Warnings do not
    # fail this local gate; release readiness is reported separately below.
    django_check = [sys.executable, "manage.py", "check", "--settings=config.settings.local"]
    if require_db:
        django_check.insert(3, "--database=default")
    results["django check"] = _run(" ".join(["manage.py", *django_check[2:]]), django_check)
    results["missing-migration check"] = _run(
        "makemigrations --check --dry-run",
        [sys.executable, "manage.py", "makemigrations", "--check", "--dry-run"],
    )

    # Browser and performance suites run in separate processes so migration
    # round-trip tests cannot contaminate their setup. Prompt 3 expressly
    # forbids enabling DJANGO_ALLOW_ASYNC_UNSAFE, including in child processes.
    # Browser tests must keep synchronous database work outside Playwright's
    # event-loop thread; the acceptance command never bypasses Django's guard.
    results["full test suite"] = _run(
        "pytest",
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "--ignore=tests/browser",
            "--ignore=tests/performance",
        ],
    )
    # The provisional online-verification load harness (Phase 3 Prompt 5,
    # ADR-0022 §8) also runs in its own process: its timings must not share
    # a machine-busy test session, and `tests/foundation/
    # test_migration_reversibility.py` migrates apps to zero and back inside
    # the main session, which leaves later transactional tests in that same
    # session without some tables.
    results["performance harness (provisional)"] = _run(
        "pytest tests/performance",
        [sys.executable, "-m", "pytest", "-q", "tests/performance"],
    )
    results["browser test suite (Playwright)"] = _run(
        "pytest tests/browser",
        [sys.executable, "-m", "pytest", "-q", "tests/browser", "--browser", "chromium"],
    )

    print("--- static deploy checks (staging, production) ---")
    deploy_ok = cmd_deploy(_args) == 0
    print(f"--- static deploy checks: {'OK' if deploy_ok else 'FAILED'} ---\n")
    results["static deploy checks"] = deploy_ok

    results["pip-audit"] = _run("pip-audit", [sys.executable, "-m", "pip_audit"])

    if require_db:
        print("--- audit-isolation (informational; never counted in the aggregate) ---")
        audit_exit_code, audit_report = run_audit_isolation_check()
        if "error" in audit_report:
            print(f"  {audit_report['error']}")
        else:
            for key, value in audit_report.items():
                print(f"  {key:42s}: {value}")
        print(
            f"  audit-isolation exit code: {audit_exit_code} -- Fact B (deployment role "
            "isolation) is EXPECTED to be unsatisfied locally (ADR-0008); this is reported "
            "for visibility only and is deliberately excluded from the pass/fail summary below."
        )
        print()

    if require_db:
        print("--- release readiness (P4-4-C1; reported separately, never in the aggregate) ---")
        _release_exit_code, release_report = run_release_readiness()
        _print_release_readiness(release_report)
        print()
        release_status = str(release_report.get("overall", "NOT_ASSESSED"))
    else:
        release_status = "NOT_ASSESSED (all-no-db does not run the assessment)"

    print("=" * 70)
    print(
        "SUMMARY (Phase 2 through Prompt 5 scope)"
        if require_db
        else "SUMMARY (all-no-db -- NOT the acceptance check)"
    )
    print("=" * 70)
    for name, ok in results.items():
        print(f"  [{'OK' if ok else 'FAIL'}] {name}")
    print()
    local_ok = all(results.values())
    print(f"LOCAL REGRESSION GATE: {'PASSED' if local_ok else 'FAILED'}")
    print(
        f"RELEASE READINESS: {release_status} -- separate from the local regression gate; "
        "a passing local gate is never release readiness."
    )
    print()
    if not require_db:
        print(
            "all-no-db skipped the real PostgreSQL 17 probe on purpose. This "
            "run must NOT be reported or treated as the full acceptance "
            "check -- run `all` for that."
        )
    print(
        "NEVER reported as passing/proven by this aggregate: audit-isolation "
        "Fact B (real database-role privilege isolation), real Redis/Celery "
        "broker integration, real S3-compatible storage integration, real "
        "email-provider integration."
    )

    return 0 if local_ok else 1


def cmd_all(args: argparse.Namespace) -> int:
    return _run_all(args, require_db=True)


def cmd_all_no_db(args: argparse.Namespace) -> int:
    """DB-optional developer convenience. NEVER the Prompt 2 acceptance check."""
    return _run_all(args, require_db=False)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

_COMMANDS = {
    "local-safety": cmd_local_safety,
    "secret-scan": cmd_secrets,
    "manifest": cmd_manifest,
    "assets": cmd_assets,
    "db-probe": cmd_db_probe,
    "db-state": cmd_db_state,
    "audit-isolation": cmd_audit_isolation,
    "release-readiness": cmd_release_readiness,
    "deploy": cmd_deploy,
    "build-static": cmd_build_static,
    "artifact-manifest": cmd_artifact_manifest,
    "artifact-verify": cmd_artifact_verify,
    "all": cmd_all,
    "all-no-db": cmd_all_no_db,
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("command", choices=sorted(_COMMANDS))
    args, extra = parser.parse_known_args(argv)
    if extra and not args.command.startswith("artifact-"):
        parser.error(f"unrecognized arguments: {' '.join(extra)}")
    args.extra = extra
    return _COMMANDS[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
