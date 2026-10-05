"""S3-compatible `PrivateStorage` adapter (Prompt 2 correction §1).

Every test here uses a mock/fake in place of `storages.backends.s3boto3
.S3Boto3Storage` -- NONE of them perform a real network call or contact any
S3-compatible service, per the correction's explicit requirement.
"""

from __future__ import annotations

import io
from unittest.mock import MagicMock, patch

import pytest

from apps.documents.storage import (
    ObjectNotFound,
    PrivateStorage,
    PrivateStorageError,
    S3PrivateStorage,
    get_private_storage,
)


def _make_storage_with_fake_backend() -> tuple[S3PrivateStorage, MagicMock]:
    """Construct an `S3PrivateStorage` whose underlying backend is a `MagicMock`.

    `storages.backends.s3boto3.S3Boto3Storage` is patched at the point
    `S3PrivateStorage.__init__` imports it, so real class is never
    instantiated and no network call is possible.
    """
    fake_backend = MagicMock(name="S3Boto3Storage-instance")
    with patch("storages.backends.s3boto3.S3Boto3Storage", return_value=fake_backend) as fake_cls:
        storage = S3PrivateStorage(
            bucket_name="test-bucket",
            endpoint_url="https://s3.example.invalid",
            access_key="test-access-key",  # secret-scan: allow
            secret_key="test-secret-key",  # secret-scan: allow
            region_name="us-east-1",
        )
    return storage, fake_backend, fake_cls


def test_interface_has_no_public_url_method() -> None:
    method_names = {name for name in dir(S3PrivateStorage) if not name.startswith("_")}
    assert method_names == {"save", "open", "exists", "delete", "size", "content_type"}
    assert "url" not in method_names


def test_backend_is_constructed_with_explicit_private_configuration() -> None:
    fake_backend = MagicMock()
    with patch("storages.backends.s3boto3.S3Boto3Storage", return_value=fake_backend) as fake_cls:
        S3PrivateStorage(
            bucket_name="my-bucket",
            endpoint_url="https://s3.example.invalid",
            access_key="explicit-access-key",  # secret-scan: allow
            secret_key="explicit-secret-key",  # secret-scan: allow
            region_name="eu-west-1",
        )

    assert fake_cls.call_count == 1
    _, kwargs = fake_cls.call_args
    assert kwargs["bucket_name"] == "my-bucket"
    assert kwargs["endpoint_url"] == "https://s3.example.invalid"
    assert kwargs["access_key"] == "explicit-access-key"
    assert kwargs["secret_key"] == "explicit-secret-key"
    assert kwargs["region_name"] == "eu-west-1"
    # Private-by-default, non-negotiable configuration:
    assert kwargs["default_acl"] == "private"
    assert kwargs["querystring_auth"] is False


def test_explicit_credentials_are_not_influenced_by_ambient_aws_env_vars(monkeypatch) -> None:
    # Simulate an unrelated ambient AWS_* environment (e.g. a developer's
    # own AWS CLI profile). The adapter must still use the EXPLICIT
    # S3_STORAGE_* values, never fall back to these.
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "ambient-key-must-not-be-used")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "ambient-secret-must-not-be-used")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "ap-southeast-1")

    fake_backend = MagicMock()
    with patch("storages.backends.s3boto3.S3Boto3Storage", return_value=fake_backend) as fake_cls:
        S3PrivateStorage(
            bucket_name="my-bucket",
            endpoint_url="https://s3.example.invalid",
            access_key="project-owned-access-key",  # secret-scan: allow
            secret_key="project-owned-secret-key",  # secret-scan: allow
            region_name="us-east-1",
        )

    _, kwargs = fake_cls.call_args
    assert kwargs["access_key"] == "project-owned-access-key"
    assert kwargs["secret_key"] == "project-owned-secret-key"
    assert kwargs["region_name"] == "us-east-1"
    assert "ambient-key-must-not-be-used" not in str(fake_cls.call_args)
    assert "ambient-secret-must-not-be-used" not in str(fake_cls.call_args)


def test_save_writes_object_and_metadata_sidecar() -> None:
    storage, fake_backend, _ = _make_storage_with_fake_backend()
    fake_backend.save.return_value = None

    key = storage.save(io.BytesIO(b"synthetic bytes"), content_type="image/jpeg")

    assert fake_backend.save.call_count == 2
    saved_names = [call.args[0] for call in fake_backend.save.call_args_list]
    assert saved_names[0] == key
    assert saved_names[1] == key + S3PrivateStorage._META_SUFFIX


def test_open_returns_backend_stream_when_object_exists() -> None:
    storage, fake_backend, _ = _make_storage_with_fake_backend()
    fake_backend.exists.return_value = True
    fake_backend.open.return_value = io.BytesIO(b"content")

    handle = storage.open("some-key")

    assert handle.read() == b"content"
    fake_backend.open.assert_called_once_with("some-key", "rb")


def test_open_raises_object_not_found_when_backend_reports_absent() -> None:
    storage, fake_backend, _ = _make_storage_with_fake_backend()
    fake_backend.exists.return_value = False

    with pytest.raises(ObjectNotFound):
        storage.open("missing-key")

    fake_backend.open.assert_not_called()


def test_delete_removes_object_and_metadata_sidecar() -> None:
    storage, fake_backend, _ = _make_storage_with_fake_backend()

    storage.delete("some-key")

    deleted_names = [call.args[0] for call in fake_backend.delete.call_args_list]
    assert deleted_names == ["some-key", "some-key" + S3PrivateStorage._META_SUFFIX]


def test_content_type_round_trips_through_the_metadata_sidecar() -> None:
    storage, fake_backend, _ = _make_storage_with_fake_backend()
    fake_backend.exists.return_value = True
    fake_backend.open.return_value = io.BytesIO(b'{"content_type": "application/pdf"}')

    assert storage.content_type("some-key") == "application/pdf"


def test_content_type_wraps_malformed_json_metadata_generically() -> None:
    storage, fake_backend, _ = _make_storage_with_fake_backend()
    fake_backend.exists.return_value = True
    # Deliberately truncated/invalid JSON below.
    fake_backend.open.return_value = io.BytesIO(b'{"content_type": "application/pdf"')

    with pytest.raises(PrivateStorageError) as excinfo:
        storage.content_type("some-key")

    message = str(excinfo.value)
    assert "some-key" not in message
    assert "content_type" not in message
    assert "application/pdf" not in message


def test_content_type_wraps_a_missing_content_type_field_generically() -> None:
    storage, fake_backend, _ = _make_storage_with_fake_backend()
    fake_backend.exists.return_value = True
    fake_backend.open.return_value = io.BytesIO(b'{"unexpected_field": "no content type here"}')

    with pytest.raises(PrivateStorageError) as excinfo:
        storage.content_type("some-key")

    message = str(excinfo.value)
    assert "some-key" not in message
    assert "unexpected_field" not in message
    assert excinfo.value.__cause__ is None


def test_content_type_wraps_an_unexpected_top_level_metadata_type_generically() -> None:
    storage, fake_backend, _ = _make_storage_with_fake_backend()
    fake_backend.exists.return_value = True
    # Valid JSON, but a list instead of an object -- `data["content_type"]`
    # would otherwise raise a raw TypeError.
    fake_backend.open.return_value = io.BytesIO(b'["application/pdf"]')

    with pytest.raises(PrivateStorageError):
        storage.content_type("some-key")


def test_content_type_wraps_invalid_encoding_in_metadata_generically() -> None:
    storage, fake_backend, _ = _make_storage_with_fake_backend()
    fake_backend.exists.return_value = True
    fake_backend.open.return_value = io.BytesIO(b"\xff\xfe\x00\x01not valid utf-8")

    with pytest.raises(PrivateStorageError):
        storage.content_type("some-key")


def test_content_type_wraps_a_non_string_content_type_field_generically() -> None:
    storage, fake_backend, _ = _make_storage_with_fake_backend()
    fake_backend.exists.return_value = True
    # Valid JSON object, valid "content_type" key, but the wrong TYPE.
    fake_backend.open.return_value = io.BytesIO(b'{"content_type": 12345}')

    with pytest.raises(PrivateStorageError) as excinfo:
        storage.content_type("some-key")

    message = str(excinfo.value)
    assert "some-key" not in message
    assert "12345" not in message
    assert excinfo.value.__cause__ is None


def test_content_type_wraps_a_provider_failure_generically() -> None:
    storage, fake_backend, _ = _make_storage_with_fake_backend()
    fake_backend.exists.return_value = True
    # secret-scan: allow (synthetic test fixture) -- provider error text below
    fake_backend.open.side_effect = RuntimeError(
        "botocore error: aws_access_key_id=REAL-LOOKING-SECRET-KEY endpoint=..."
    )

    with pytest.raises(PrivateStorageError) as excinfo:
        storage.content_type("some-key")

    message = str(excinfo.value)
    assert "some-key" not in message
    assert "test-bucket" not in message
    assert "REAL-LOOKING-SECRET-KEY" not in message
    assert "botocore error" not in message
    assert excinfo.value.__cause__ is None


@pytest.mark.parametrize(
    "malformed_metadata_body",
    [
        pytest.param(b'{"content_type": "application/pdf"', id="malformed-json"),
        pytest.param(b'{"unexpected_field": "no content type here"}', id="missing-field"),
        pytest.param(b'["application/pdf"]', id="not-an-object"),
        pytest.param(b'{"content_type": 12345}', id="non-string-content-type"),
        pytest.param(b"\xff\xfe\x00\x01not valid utf-8", id="invalid-encoding"),
    ],
)
def test_content_type_failures_never_leak_key_bucket_or_credentials(
    malformed_metadata_body: bytes,
) -> None:
    """Prompt 2 final closure pass §3: every malformed-metadata condition's
    exception message is checked against the storage key, the bucket
    name, and both S3 credentials configured on this fixture -- none of
    them may ever appear."""
    storage, fake_backend, _ = _make_storage_with_fake_backend()
    fake_backend.exists.return_value = True
    fake_backend.open.return_value = io.BytesIO(malformed_metadata_body)

    with pytest.raises(PrivateStorageError) as excinfo:
        storage.content_type("distinctive-test-key")

    message = str(excinfo.value)
    assert "distinctive-test-key" not in message
    assert "test-bucket" not in message
    assert "test-access-key" not in message
    assert "test-secret-key" not in message
    assert "s3.example.invalid" not in message


@pytest.mark.parametrize("bad_key", ["../escape", "a/b", "a\\b", ".", ".."])
def test_path_traversal_is_rejected_before_touching_the_backend(bad_key: str) -> None:
    storage, fake_backend, _ = _make_storage_with_fake_backend()

    with pytest.raises(PrivateStorageError):
        storage.open(bad_key)

    fake_backend.exists.assert_not_called()


def test_backend_failure_is_wrapped_and_never_leaks_the_original_message() -> None:
    storage, fake_backend, _ = _make_storage_with_fake_backend()
    fake_backend.exists.side_effect = RuntimeError(
        "botocore error: aws_access_key_id=REAL-LOOKING-SECRET-KEY endpoint=..."
    )

    with pytest.raises(PrivateStorageError) as excinfo:
        storage.exists("some-key")

    message = str(excinfo.value)
    assert "REAL-LOOKING-SECRET-KEY" not in message
    assert "botocore error" not in message
    # The chain is deliberately broken (`from None`), not merely hidden
    # from str() -- so the original message can never resurface later via
    # traceback.format_exc() or a structured-logging exception dump.
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__suppress_context__ is True


def test_object_not_found_error_never_includes_the_requested_key() -> None:
    storage, fake_backend, _ = _make_storage_with_fake_backend()
    fake_backend.exists.return_value = False
    distinctive_key = "unknown-key-should-never-appear-in-the-error-5678"

    with pytest.raises(ObjectNotFound) as excinfo:
        storage.open(distinctive_key)

    assert distinctive_key not in str(excinfo.value)


def test_save_cleans_up_the_primary_and_metadata_objects_when_metadata_save_fails() -> None:
    """Prompt 2 correction §8, extended in the final closure pass: on a
    metadata-save failure, both the primary object AND the metadata
    sidecar (in case the provider partially created it despite raising)
    are deleted, best-effort."""
    storage, fake_backend, _ = _make_storage_with_fake_backend()
    fake_backend.save.side_effect = [None, RuntimeError("metadata backend failure")]

    with pytest.raises(PrivateStorageError) as excinfo:
        storage.save(io.BytesIO(b"synthetic bytes"), content_type="image/jpeg")

    assert "metadata backend failure" not in str(excinfo.value)
    assert fake_backend.delete.call_count == 2
    deleted_keys = {call.args[0] for call in fake_backend.delete.call_args_list}
    saved_primary_key = fake_backend.save.call_args_list[0].args[0]
    assert deleted_keys == {saved_primary_key, saved_primary_key + S3PrivateStorage._META_SUFFIX}


def test_save_cleanup_failure_does_not_leak_through_the_reported_error() -> None:
    storage, fake_backend, _ = _make_storage_with_fake_backend()
    fake_backend.save.side_effect = [None, RuntimeError("metadata backend failure")]
    fake_backend.delete.side_effect = RuntimeError("cleanup delete also failed, details: ...")

    with pytest.raises(PrivateStorageError) as excinfo:
        storage.save(io.BytesIO(b"synthetic bytes"), content_type="image/jpeg")

    message = str(excinfo.value)
    assert "metadata backend failure" not in message
    assert "cleanup delete also failed" not in message


def test_get_private_storage_selects_s3_and_wires_project_owned_settings(settings) -> None:
    settings.PRIVATE_STORAGE_BACKEND = "s3"
    settings.S3_STORAGE_BUCKET_NAME = "settings-bucket"
    settings.S3_STORAGE_ENDPOINT_URL = "https://s3.settings.invalid"
    settings.S3_STORAGE_ACCESS_KEY_ID = "settings-access-key"  # secret-scan: allow
    settings.S3_STORAGE_SECRET_ACCESS_KEY = "settings-secret-key"  # secret-scan: allow
    settings.S3_STORAGE_REGION_NAME = "us-west-2"

    fake_backend = MagicMock()
    with patch("storages.backends.s3boto3.S3Boto3Storage", return_value=fake_backend) as fake_cls:
        storage = get_private_storage()

    assert isinstance(storage, S3PrivateStorage)
    assert isinstance(storage, PrivateStorage)
    _, kwargs = fake_cls.call_args
    assert kwargs["bucket_name"] == "settings-bucket"
    assert kwargs["access_key"] == "settings-access-key"
    assert kwargs["region_name"] == "us-west-2"


def test_get_private_storage_selects_filesystem_by_default(settings) -> None:
    assert settings.PRIVATE_STORAGE_BACKEND == "filesystem"
    storage = get_private_storage()
    assert type(storage).__name__ == "PrivateFileSystemStorage"
