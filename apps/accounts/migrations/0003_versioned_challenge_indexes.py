from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("accounts", "0002_authenticationchallenge_scopedgroupmembership")]

    operations = [
        migrations.RemoveIndex(
            model_name="authenticationchallenge",
            name="acc_challenge_recipient_idx",
        ),
        migrations.RemoveIndex(
            model_name="authenticationchallenge",
            name="acc_challenge_network_idx",
        ),
        migrations.AddIndex(
            model_name="authenticationchallenge",
            index=models.Index(
                fields=[
                    "recipient_fingerprint",
                    "recipient_fingerprint_key_version",
                    "issued_at",
                ],
                name="acc_challenge_recipient_v_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="authenticationchallenge",
            index=models.Index(
                fields=[
                    "network_fingerprint",
                    "network_fingerprint_key_version",
                    "issued_at",
                ],
                name="acc_challenge_network_v_idx",
            ),
        ),
    ]
