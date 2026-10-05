"""`manage.py provision_staging_uat`: the guarded, repeatable staging UAT setup.

    python manage.py provision_staging_uat --confirm-host <staging host> \\
        --email-pattern 'uat-{key}@<team-controlled domain>'          # preview
    python manage.py provision_staging_uat ... --apply                # write
    python manage.py provision_staging_uat ... --retire [--apply]     # clean up

Staging only (`config.settings.staging`). Preview is the default. See
`apps.core.services.staging_provisioning` for what is created and why, and
`docs/deployment/README.md` for the procedure. No password is set or shown:
set each created account's password with `manage.py changepassword <email>`.
Only records the provisioning set created are reused or retired.

Exit code 0 on success, 1 when a conflict (including an ownership conflict)
stopped the run before it wrote anything, 2 when a safeguard refused it.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Preview, apply or retire the synthetic staging UAT accounts and test data."

    def add_arguments(self, parser):
        parser.add_argument(
            "--confirm-host",
            required=True,
            help="The host name of DJANGO_PUBLIC_BASE_URL, typed by the operator.",
        )
        parser.add_argument(
            "--email-pattern",
            required=True,
            help="Address pattern with {key}, for team-controlled test mailboxes.",
        )
        parser.add_argument("--apply", action="store_true", help="Write (default: preview).")
        parser.add_argument("--retire", action="store_true", help="Disable and close instead.")
        parser.add_argument(
            "--temporary-days",
            type=int,
            default=14,
            help="Validity of the temporary and external-security accounts (1-60, default 14).",
        )

    def handle(self, *args, **options):
        from apps.core.services.staging_provisioning import (
            ProvisioningRefused,
            apply_plan,
            build_plan,
            retire,
        )

        common = {
            "email_pattern": options["email_pattern"],
            "confirm_host": options["confirm_host"],
        }
        try:
            if options["retire"]:
                report = retire(**common, apply=options["apply"])
            elif options["apply"]:
                report = apply_plan(**common, temporary_days=options["temporary_days"])
            else:
                report = build_plan(**common)
        except ProvisioningRefused as exc:
            raise CommandError(f"Refused: {exc}", returncode=2) from None

        mode = "RETIRE" if options["retire"] else "APPLY" if options["apply"] else "PREVIEW"
        self.stdout.write(f"Staging UAT provisioning -- {mode}")
        for line in report.lines:
            self.stdout.write(f"  [{line.action:9}] {line.kind:12} {line.key:22} {line.detail}")
        if report.conflicts:
            raise CommandError(
                f"{len(report.conflicts)} conflict(s): nothing was written.", returncode=1
            )
        if not report.applied:
            self.stdout.write("Preview only: nothing was written. Add --apply to write.")
            return
        if report.invitation_urls:
            self.stdout.write("")
            self.stdout.write(
                "Invitation links (shown ONCE; give them to the UAT coordinator only; a "
                "lost link is replaced by rotating it in the organization workspace):"
            )
            for reference, url in report.invitation_urls.items():
                self.stdout.write(f"  {reference}: {url}")
        if report.created_account_emails:
            self.stdout.write("")
            self.stdout.write(
                "Accounts were created WITHOUT a usable password. Set each one interactively:"
            )
            for email in report.created_account_emails:
                self.stdout.write(f"  python manage.py changepassword {email}")
        self.stdout.write("Done.")
