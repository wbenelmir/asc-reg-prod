"""Organizations and aliases (Schema §6.2).

`ProfessionalAffiliation` is added later by `organizations.0002`, once
`registrations.Registration` exists to attach to (Schema §5.6).
"""

from __future__ import annotations

from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.core.models import TimestampedModel, UUIDPrimaryKeyModel


class OrganizationType(models.TextChoices):
    """Labels are translated (Prompt 5 correction pass §5) -- the stored
    enum VALUE (e.g. "COMPANY") is never translated or changed, only the
    human-facing label Django renders via `get_organization_type_display()`
    or a `ChoiceField`'s option text."""

    MINISTRY = "MINISTRY", _("Ministry")
    PUBLIC_BODY = "PUBLIC_BODY", _("Public body")
    COMPANY = "COMPANY", _("Company")
    STARTUP = "STARTUP", _("Startup")
    # Legacy (C-04): kept readable for existing rows, never offered for new
    # selection (`NEW_SELECTION_ORGANIZATION_TYPES`).
    INSTITUTION = "INSTITUTION", _("Institution")
    OTHER = "OTHER", _("Other")
    # UX-2 (M14, C-04): the approved global list. Codes stay within 16 characters.
    LOCAL_GOV = "LOCAL_GOV", _("Local or regional government")
    INTL_ORG = "INTL_ORG", _("International or intergovernmental organization")
    DIPLOMATIC = "DIPLOMATIC", _("Embassy or diplomatic mission")
    SME = "SME", _("Small or medium enterprise")
    INCUBATOR = "INCUBATOR", _("Incubator, accelerator or technology park")
    INVESTOR = "INVESTOR", _("Investor (venture capital, angel or fund)")
    FINANCIAL = "FINANCIAL", _("Bank or financial institution")
    UNIVERSITY = "UNIVERSITY", _("University or higher education")
    RESEARCH = "RESEARCH", _("Research centre")
    NGO = "NGO", _("Non-profit, NGO or association")
    BUSINESS_ASSOC = "BUSINESS_ASSOC", _("Chamber, federation or professional association")
    MEDIA = "MEDIA", _("Media or press")
    FREELANCE = "FREELANCE", _("Independent or self-employed")
    STUDENT = "STUDENT", _("Student (no organization)")


#: The organization types a participant may choose for new input, in display
#: order; `OTHER` last. `INSTITUTION` is legacy and not offered.
NEW_SELECTION_ORGANIZATION_TYPES: tuple[str, ...] = (
    "MINISTRY",
    "PUBLIC_BODY",
    "LOCAL_GOV",
    "INTL_ORG",
    "DIPLOMATIC",
    "COMPANY",
    "SME",
    "STARTUP",
    "INCUBATOR",
    "INVESTOR",
    "FINANCIAL",
    "UNIVERSITY",
    "RESEARCH",
    "NGO",
    "BUSINESS_ASSOC",
    "MEDIA",
    "FREELANCE",
    "STUDENT",
    "OTHER",
)


class OperatingScope(models.TextChoices):
    """UX-2 (M16, D-07): where the organization operates, separate from its
    headquarters country. "Multinational" is never a fake country."""

    NATIONAL = "NATIONAL", _("National")
    REGIONAL = "REGIONAL", _("Regional (several countries)")
    MULTINATIONAL = "MULTINATIONAL", _("Multinational or global")


class OrganizationStatus(models.TextChoices):
    ACTIVE = "ACTIVE", "Active"
    INACTIVE = "INACTIVE", "Inactive"
    MERGED = "MERGED", "Merged"


class Organization(UUIDPrimaryKeyModel, TimestampedModel):
    """Master organization record (Schema §6.2). Merge relinks references but
    never rewrites a Registration's submitted professional snapshot."""

    official_name = models.CharField(max_length=300)
    normalized_name = models.CharField(max_length=300, blank=True, default="")
    organization_type = models.CharField(max_length=16, choices=OrganizationType.choices)
    country_code = models.ForeignKey(
        "core.Country", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    status = models.CharField(
        max_length=16, choices=OrganizationStatus.choices, default=OrganizationStatus.ACTIVE
    )
    merged_into = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.PROTECT, related_name="merged_organizations"
    )

    class Meta:
        db_table = "organizations_organization"
        constraints = [
            models.CheckConstraint(
                condition=~models.Q(merged_into=models.F("id")),
                name="org_organization_no_self_merge",
            ),
        ]
        indexes = [models.Index(fields=["normalized_name"], name="org_organization_norm_idx")]

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.official_name


class OrganizationAlias(UUIDPrimaryKeyModel, TimestampedModel):
    """Alternative name, language, or historical spelling (Schema §6.2)."""

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="aliases")
    alias_name = models.CharField(max_length=300)
    language = models.CharField(max_length=8, blank=True, default="")

    class Meta:
        db_table = "organizations_organization_alias"
        indexes = [models.Index(fields=["organization"], name="org_alias_organization_idx")]


class ProfessionalAffiliation(UUIDPrimaryKeyModel, TimestampedModel):
    """Context-specific affiliation for one Registration (Schema §5.6).

    The submitted snapshot (`submitted_organization_name`, `job_title`,
    ...) remains unchanged when the matched master `organization` is later
    renamed or merged -- only `organization` is repointed, never the
    submitted text.
    """

    registration = models.OneToOneField(
        "registrations.Registration",
        on_delete=models.CASCADE,
        related_name="professional_affiliation",
    )
    organization = models.ForeignKey(
        Organization, null=True, blank=True, on_delete=models.SET_NULL, related_name="affiliations"
    )
    submitted_organization_name = models.CharField(max_length=300)
    organization_type = models.CharField(
        max_length=16, choices=OrganizationType.choices, blank=True, default=""
    )
    job_title = models.CharField(max_length=200, blank=True, default="")
    department = models.CharField(max_length=200, blank=True, default="")
    sector = models.ForeignKey(
        "core.Sector", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    country_code = models.ForeignKey(
        "core.Country", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    organization_website = models.URLField(max_length=300, blank=True, default="")
    professional_profile_url = models.URLField(max_length=300, blank=True, default="")
    biography = models.CharField(max_length=2000, blank=True, default="")
    # UX-2 (D-07): required for new input; existing rows stay empty and are
    # shown as "Not provided".
    operating_scope = models.CharField(
        max_length=16, choices=OperatingScope.choices, blank=True, default=""
    )

    class Meta:
        db_table = "organizations_professional_affiliation"
        indexes = [models.Index(fields=["organization"], name="org_affiliation_org_idx")]
