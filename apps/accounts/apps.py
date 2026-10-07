from django.apps import AppConfig
from django.db.models.signals import post_migrate

REGISTRATION_INTAKE_GROUP_NAME = "Registration Intake"

# Codename-only (app_label, codename) pairs: the exact, minimal permission
# set for the approved Phase 1 operational group (§11 "required
# Django permissions only"). Auto-created `view_registration` permissions
# are looked up, never re-declared with a different codename.
REGISTRATION_INTAKE_PERMISSIONS: tuple[tuple[str, str], ...] = (
    ("registrations", "view_registration"),
    ("registrations", "view_registrationprofile"),
)

# Phase 2 Prompt 4 "temporary external-security accounts": a stable,
# named classification group carrying DELIBERATELY ZERO permissions by
# default. `OperationalUserAccountType.EXTERNAL_SECURITY` accounts are
# created against this group so the account exists with a scoped
# `ScopedGroupMembership` (event/organization, `active_until` expiry) but
# gains no functional capability whatsoever until an administrator
# explicitly grants a specific permission to it later -- deny-by-default,
# no implicit inheritance of any unrelated operational permission. No
# entry/gate/scan permission is ever granted to this group in this prompt.
EXTERNAL_SECURITY_TEMPORARY_GROUP_NAME = "External Security (Temporary)"

# Staff account administration (`apps.accounts.administration`): create staff
# accounts, change their status and validity, send credential setup links and
# grant or revoke scoped roles within the administrator's own scope. No other
# group receives it; the first administrator is set up with
# `manage.py bootstrap_account_administrator`.
ACCOUNT_ADMINISTRATORS_GROUP_NAME = "Account Administrators"
ACCOUNT_ADMINISTRATORS_PERMISSIONS: tuple[tuple[str, str], ...] = (
    ("accounts", "manage_operational_accounts"),
)

# Ministry NIN service diagnostics (`apps.people.nin_diagnostics`): a technical
# role, granted only for every event and organization (`roles.py`,
# `global_only`); the page refuses any narrower membership.
INTEGRATION_DIAGNOSTICS_GROUP_NAME = "Integration Diagnostics Operators"
INTEGRATION_DIAGNOSTICS_PERMISSIONS: tuple[tuple[str, str], ...] = (
    ("people", "run_ministry_nin_diagnostics"),
)


def _ensure_registration_intake_group(sender, **kwargs):
    """Idempotently ensure the "Registration Intake" group and its permissions exist.

    Connected to `post_migrate`, the standard Django idiom for authorization
    bootstrap data (not domain business logic) -- it runs after every
    `migrate`, once Django's own default per-model permissions have been
    created, and is safe to run repeatedly (`get_or_create`/`add` are both
    idempotent).
    """
    from django.contrib.auth.models import Group, Permission

    group, _ = Group.objects.get_or_create(name=REGISTRATION_INTAKE_GROUP_NAME)
    for app_label, codename in REGISTRATION_INTAKE_PERMISSIONS:
        try:
            permission = Permission.objects.get(
                content_type__app_label=app_label, codename=codename
            )
        except Permission.DoesNotExist:
            # The referenced app's migrations have not created this
            # permission yet in this `migrate` run (e.g. a partial/targeted
            # migrate). Never fails the migration for this -- the group
            # still exists and gains the permission on the next full run.
            continue
        group.permissions.add(permission)
    # Deliberately created with NO permissions ever added here (Phase 2
    # Prompt 4) -- existence only, so the group is available to assign a
    # temporary external-security account to without granting anything.
    Group.objects.get_or_create(name=EXTERNAL_SECURITY_TEMPORARY_GROUP_NAME)
    for group_name, permissions in (
        (ACCOUNT_ADMINISTRATORS_GROUP_NAME, ACCOUNT_ADMINISTRATORS_PERMISSIONS),
        (INTEGRATION_DIAGNOSTICS_GROUP_NAME, INTEGRATION_DIAGNOSTICS_PERMISSIONS),
    ):
        group, _ = Group.objects.get_or_create(name=group_name)
        for app_label, codename in permissions:
            permission = Permission.objects.filter(
                content_type__app_label=app_label, codename=codename
            ).first()
            if permission is not None:
                group.permissions.add(permission)


class AccountsConfig(AppConfig):
    """Operational users, authentication, groups, permissions, sessions and scoped membership."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.accounts"
    label = "accounts"

    def ready(self) -> None:
        from apps.accounts import checks  # noqa: F401 - registers the CAPTCHA checks

        # Connected for EVERY app's post_migrate, not only this app's: the
        # intake permissions belong to `registrations`, which migrates after
        # `accounts`, so a handler limited to this sender ran before they
        # existed and left the group empty after a fresh `migrate`. The
        # handler is idempotent and only adds permissions.
        post_migrate.connect(
            _ensure_registration_intake_group,
            dispatch_uid="accounts-ensure-registration-intake-group",
        )
