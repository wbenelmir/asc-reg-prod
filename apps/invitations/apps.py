from django.apps import AppConfig
from django.db.models.signals import post_migrate

# Phase 2 Prompt 2 correction pass ("review default operational
# permissions" / least privilege): FOUR distinct groups, not two. The
# original two-group design bundled every sensitive write capability
# (delegation upload, on-behalf registration) into the same groups an
# ordinary organization user or a general invitation manager received by
# DEFAULT. Each sensitive write capability below now requires its OWN
# explicitly-granted group -- basic view access never implies it.
INVITATION_MANAGER_GROUP_NAME = "Invitation Manager"
ORGANIZATION_USER_GROUP_NAME = "Organization User"
DELEGATION_COORDINATOR_GROUP_NAME = "Delegation Coordinator"
ON_BEHALF_REGISTRAR_GROUP_NAME = "On-Behalf Registrar"

# Codename-only (app_label, codename) pairs -- the exact, minimal permission
# set for each Phase 2 Prompt 2 operational group ("required Django
# permissions only"), mirroring apps.accounts.apps's Registration Intake
# bootstrap pattern.

# Campaign and link LIFECYCLE control (create/change a campaign, issue/
# rotate/revoke a link) -- a genuinely elevated, explicitly-granted role.
# Deliberately does NOT include delegation-upload or on-behalf-registration:
# those are separate sensitive capabilities with their own groups below.
INVITATION_MANAGER_PERMISSIONS: tuple[tuple[str, str], ...] = (
    ("invitations", "add_invitationcampaign"),
    ("invitations", "change_invitationcampaign"),
    ("invitations", "view_invitationcampaign"),
    ("invitations", "add_invitationlink"),
    ("invitations", "change_invitationlink"),
    ("invitations", "view_invitationlink"),
    ("registrations", "view_registration"),
)

# The BASIC, default-granted organization role -- VIEW ONLY. An ordinary
# organization user never automatically gains any write capability
# (delegation upload, on-behalf registration, campaign/link changes)
# merely by being a member of their organization's workspace (Phase 2
# Prompt 2 correction pass "do not automatically grant sensitive
# capabilities merely because a user is an ordinary organization user").
ORGANIZATION_USER_PERMISSIONS: tuple[tuple[str, str], ...] = (
    ("invitations", "view_invitationcampaign"),
    ("invitations", "view_invitationlink"),
    ("invitations", "view_delegationbatch"),
    ("registrations", "view_registration"),
)

# Delegation CSV upload/apply -- a distinct sensitive write capability,
# granted only through this explicit group, never bundled into the basic
# Organization User role or implied by Invitation Manager.
DELEGATION_COORDINATOR_PERMISSIONS: tuple[tuple[str, str], ...] = (
    ("invitations", "view_invitationcampaign"),
    ("invitations", "add_delegationbatch"),
    ("invitations", "view_delegationbatch"),
    ("invitations", "add_onbehalfclaim"),
    ("invitations", "view_onbehalfclaim"),
    ("registrations", "view_registration"),
)

# Authorized on-behalf draft creation -- a distinct sensitive write
# capability (AF-ORG-03), granted only through this explicit group.
ON_BEHALF_REGISTRAR_PERMISSIONS: tuple[tuple[str, str], ...] = (
    ("invitations", "view_invitationcampaign"),
    ("invitations", "add_onbehalfclaim"),
    ("invitations", "view_onbehalfclaim"),
    ("registrations", "view_registration"),
    ("registrations", "register_on_behalf"),
)

_ALL_GROUPS: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = (
    (INVITATION_MANAGER_GROUP_NAME, INVITATION_MANAGER_PERMISSIONS),
    (ORGANIZATION_USER_GROUP_NAME, ORGANIZATION_USER_PERMISSIONS),
    (DELEGATION_COORDINATOR_GROUP_NAME, DELEGATION_COORDINATOR_PERMISSIONS),
    (ON_BEHALF_REGISTRAR_GROUP_NAME, ON_BEHALF_REGISTRAR_PERMISSIONS),
)


def _ensure_invitation_groups(sender, **kwargs):
    """Idempotently ensure Phase 2 Prompt 2's operational groups exist.

    Connected to `post_migrate`, the same established idiom as
    `apps.accounts.apps._ensure_registration_intake_group` -- runs after
    every `migrate`, once Django's own default per-model permissions have
    been created, and is safe to run repeatedly. Only ADDS permissions
    listed above; never removes a permission an administrator may have
    separately granted through the Django admin.
    """
    from django.contrib.auth.models import Group, Permission

    for group_name, permission_pairs in _ALL_GROUPS:
        group, _ = Group.objects.get_or_create(name=group_name)
        for app_label, codename in permission_pairs:
            try:
                permission = Permission.objects.get(
                    content_type__app_label=app_label, codename=codename
                )
            except Permission.DoesNotExist:
                # The referenced app's migrations have not created this
                # permission yet in this `migrate` run (e.g. a partial/
                # targeted migrate) -- never fails the migration for this;
                # the group still exists and gains the permission on the
                # next full run.
                continue
            group.permissions.add(permission)


class InvitationsConfig(AppConfig):
    """Organization invitation campaigns, reusable links, delegation import and
    authorized on-behalf registration with participant claim (Phase 2 Prompt 2;
    TRD §13.1 names `invitations` as a distinct later-phase application)."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.invitations"
    label = "invitations"

    def ready(self) -> None:
        post_migrate.connect(_ensure_invitation_groups, sender=self)
