"""Encrypted text field and versioned HMAC blind-index field types (ADR-0006).

Indexes on encrypted plaintext are prohibited (Schema §16.2); only
`BlindIndexField` columns are ever indexed for exact matching, and those
indexes are declared as partial unique indexes in each app's migrations,
never as `unique=True` on the field itself (the uniqueness rule applies
only to a subset of rows, e.g. "verified active").
"""

from __future__ import annotations

from django.db import models

from apps.core.crypto import decrypt, encrypt


class EncryptedTextField(models.TextField):
    """Application-level AES-GCM authenticated encryption (ADR-0006).

    Encryption/decryption happens transparently on write/read through
    Django's `get_prep_value`/`from_db_value` hooks -- this is field-type
    plumbing, not domain business logic. The stored value always carries
    its own key-version tag (`apps.core.crypto.encrypt`), so rotation does
    not require a column migration.
    """

    def get_prep_value(self, value):
        value = super().get_prep_value(value)
        if value is None or value == "":
            return value
        return encrypt(value)

    def from_db_value(self, value, expression, connection):
        if value is None or value == "":
            return value
        return decrypt(value)


class BlindIndexField(models.CharField):
    """Fixed-length hex digest column for a versioned HMAC blind index (ADR-0006).

    Carries no encryption/decryption behavior of its own -- computing the
    digest from a normalized plaintext value is a service-layer
    responsibility (`apps.core.crypto.compute_blind_index`). This subclass
    exists to name and constrain the column shape distinctly from an
    ordinary `CharField`.
    """

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("max_length", 64)  # SHA-256 hex digest length
        super().__init__(*args, **kwargs)
