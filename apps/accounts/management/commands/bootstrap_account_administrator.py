"""`manage.py bootstrap_account_administrator --email ... --display-name ...`

The protected setup of the FIRST staff account administrator, run on the
server by the deployment team (the established operational mechanism; the
production deployment never depends on the Django admin). Refused as soon as
an unrestricted account administrator exists: every later account is created
in the operations area `/ops/staff-accounts/`.

The account receives the Account Administrators role for every event and
organization and a single-use, expiring setup link
(`apps.accounts.administration.bootstrap_administrator`). Delivery:

* `--deliver email` (default): the link is emailed to the account's own
  address through the configured mail backend;
* `--deliver terminal`: the link is printed ONCE on this terminal, for an
  operator who cannot receive the email; it is not written anywhere else.

`--existing` promotes an existing, non-disabled account (its current
password stays valid; the link is a reset link).

When the email cannot be sent, the account stays created, the unsent link is
revoked, the failure is audited and the command exits with an error that says
so. `--resend` then issues a new link for that same pending account only
(`apps.accounts.administration.resend_bootstrap_setup`: refused for any other
account, after the setup was completed, or when another administrator can
send links); combine it with `--deliver terminal` if email keeps failing.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from apps.accounts.administration import (
    AccountAdministrationError,
    bootstrap_administrator,
    deliver_setup_link,
    record_delivery_failure,
    resend_bootstrap_setup,
    setup_url,
)
from apps.accounts.models import CredentialSetupPurpose


class Command(BaseCommand):
    help = "Set up the first staff account administrator (refused once one exists)."

    def add_arguments(self, parser):
        parser.add_argument("--email", required=True)
        parser.add_argument("--display-name", default="")
        mode = parser.add_mutually_exclusive_group()
        mode.add_argument("--existing", action="store_true")
        mode.add_argument(
            "--resend",
            action="store_true",
            help="New setup link for the pending first administrator whose email failed.",
        )
        parser.add_argument("--deliver", choices=("email", "terminal"), default="email")

    def handle(self, *args, **options):
        try:
            if options["resend"]:
                user, raw_link = resend_bootstrap_setup(email=options["email"])
                purpose = user.credential_setup_tokens.order_by("-created_at").first().purpose
            else:
                user, raw_link = bootstrap_administrator(
                    email=options["email"],
                    display_name=options["display_name"],
                    existing=options["existing"],
                )
                purpose = (
                    CredentialSetupPurpose.RESET
                    if options["existing"]
                    else CredentialSetupPurpose.INVITATION
                )
        except AccountAdministrationError as exc:
            raise CommandError(f"Refused: {exc.code}") from None
        if options["deliver"] == "terminal":
            self.stdout.write(
                "Single-use setup link (shown once; do not store, share or paste it into a ticket):"
            )
            self.stdout.write(setup_url(raw_link))
            return
        try:
            deliver_setup_link(email=user.email_normalized, raw_link=raw_link, purpose=purpose)
        except Exception as exc:  # report every transport failure accurately
            record_delivery_failure(user_id=user.pk, raw_link=raw_link, error=exc)
            raise CommandError(
                f"The account is set up, but the setup link could not be emailed "
                f"({type(exc).__name__}); that link was revoked. Run this command again with "
                f"--resend (add --deliver terminal if email keeps failing)."
            ) from None
        self.stdout.write("A single-use setup link was sent to the account's email address.")
