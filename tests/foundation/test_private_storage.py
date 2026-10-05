"""`PrivateStorage` interface and local filesystem implementation (§5.11)."""

from __future__ import annotations

import io
import uuid as uuid_module

import pytest

from apps.documents.storage import (
    ObjectNotFound,
    PrivateFileSystemStorage,
    PrivateStorage,
    PrivateStorageError,
)


def test_interface_has_no_public_url_method() -> None:
    method_names = {name for name in dir(PrivateStorage) if not name.startswith("_")}
    assert method_names == {"save", "open", "exists", "delete", "size", "content_type"}
    assert "url" not in method_names


def test_save_open_round_trip(private_storage_root) -> None:
    storage = PrivateFileSystemStorage(private_storage_root)
    key = storage.save(io.BytesIO(b"synthetic bytes"), content_type="image/jpeg")

    assert storage.exists(key)
    assert storage.size(key) == len(b"synthetic bytes")
    assert storage.content_type(key) == "image/jpeg"
    with storage.open(key) as handle:
        assert handle.read() == b"synthetic bytes"


def test_delete_removes_object_and_metadata(private_storage_root) -> None:
    storage = PrivateFileSystemStorage(private_storage_root)
    key = storage.save(io.BytesIO(b"x"), content_type="text/plain")

    storage.delete(key)

    assert not storage.exists(key)
    with pytest.raises(ObjectNotFound):
        storage.content_type(key)


def test_unknown_key_raises_object_not_found(private_storage_root) -> None:
    storage = PrivateFileSystemStorage(private_storage_root)
    with pytest.raises(ObjectNotFound):
        storage.open("does-not-exist")


@pytest.mark.parametrize("bad_key", ["../escape", "a/b", "a\\b", ".", ".."])
def test_path_traversal_is_rejected(private_storage_root, bad_key: str) -> None:
    storage = PrivateFileSystemStorage(private_storage_root)
    with pytest.raises(PrivateStorageError):
        storage.open(bad_key)


def test_object_not_found_error_never_includes_the_requested_key(private_storage_root) -> None:
    storage = PrivateFileSystemStorage(private_storage_root)
    distinctive_key = "unknown-key-should-never-appear-in-the-error-1234"

    with pytest.raises(ObjectNotFound) as excinfo:
        storage.open(distinctive_key)

    assert distinctive_key not in str(excinfo.value)


def test_path_traversal_error_never_includes_the_local_filesystem_path(
    private_storage_root,
) -> None:
    storage = PrivateFileSystemStorage(private_storage_root)

    with pytest.raises(PrivateStorageError) as excinfo:
        storage.open("../escape")

    assert str(private_storage_root) not in str(excinfo.value)


def test_save_cleans_up_the_primary_object_when_metadata_write_fails(
    private_storage_root, monkeypatch
) -> None:
    fixed_uuid = uuid_module.UUID(int=0)
    monkeypatch.setattr("apps.documents.storage.uuid.uuid4", lambda: fixed_uuid)
    expected_key = fixed_uuid.hex
    # Force the metadata sidecar write to fail: pre-create a DIRECTORY at
    # the exact path the sidecar file would need, so `Path.write_text()`
    # raises `IsADirectoryError` (an `OSError`).
    meta_path = private_storage_root / (expected_key + PrivateFileSystemStorage._META_SUFFIX)
    meta_path.mkdir(parents=True)
    storage = PrivateFileSystemStorage(private_storage_root)

    with pytest.raises(PrivateStorageError):
        storage.save(io.BytesIO(b"synthetic bytes"), content_type="image/jpeg")

    primary_path = private_storage_root / expected_key
    assert not primary_path.exists(), "the primary object must not be left orphaned"


def test_save_cleans_up_a_partial_primary_file_when_the_primary_write_fails(
    private_storage_root, monkeypatch
) -> None:
    """Prompt 2 final closure pass §7.

    Opening a file in "wb" mode creates/truncates it immediately, before
    any bytes are written -- so a controlled failing writer whose own
    `read()` raises partway through still leaves a (here: empty) partial
    primary file on disk unless `save()` cleans it up.
    """
    fixed_uuid = uuid_module.UUID(int=2)
    monkeypatch.setattr("apps.documents.storage.uuid.uuid4", lambda: fixed_uuid)
    expected_key = fixed_uuid.hex
    storage = PrivateFileSystemStorage(private_storage_root)

    class FailingContent:
        def read(self) -> bytes:
            # secret-scan: allow (synthetic test fixture)
            raise RuntimeError("simulated failing writer: disk full at byte 42")

    with pytest.raises(PrivateStorageError) as excinfo:
        storage.save(FailingContent(), content_type="image/jpeg")

    primary_path = private_storage_root / expected_key
    assert not primary_path.exists(), "the partial primary file must not be left behind"
    message = str(excinfo.value)
    assert str(private_storage_root) not in message
    assert expected_key not in message
    assert "simulated failing writer" not in message
    assert "disk full" not in message


@pytest.mark.parametrize(
    "malformed_metadata_text",
    [
        pytest.param('{"content_type": "application/pdf"', id="malformed-json"),
        pytest.param('{"unexpected_field": "no content type here"}', id="missing-field"),
        pytest.param('["application/pdf"]', id="not-an-object"),
        pytest.param('{"content_type": 12345}', id="non-string-content-type"),
    ],
)
def test_content_type_wraps_malformed_metadata_generically(
    private_storage_root, malformed_metadata_text: str
) -> None:
    """Prompt 2 final closure pass §3, mirrored for the local filesystem
    backend: malformed JSON, JSON that is not an object, a missing
    `content_type` field, and a non-string `content_type` value must all
    become the same generic `PrivateStorageError` -- never a raw
    `KeyError`/`TypeError`/`json.JSONDecodeError`, and never leak the
    storage key or the local filesystem path."""
    storage = PrivateFileSystemStorage(private_storage_root)
    key = "distinctive0000000000000000000000"
    meta_path = private_storage_root / (key + PrivateFileSystemStorage._META_SUFFIX)
    meta_path.write_text(malformed_metadata_text, encoding="utf-8")

    with pytest.raises(PrivateStorageError) as excinfo:
        storage.content_type(key)

    message = str(excinfo.value)
    assert key not in message
    assert str(private_storage_root) not in message
    assert excinfo.value.__cause__ is None


def test_content_type_wraps_invalid_utf8_metadata_generically(private_storage_root) -> None:
    storage = PrivateFileSystemStorage(private_storage_root)
    key = "distinctive0000000000000000000001"
    meta_path = private_storage_root / (key + PrivateFileSystemStorage._META_SUFFIX)
    meta_path.write_bytes(b"\xff\xfe\x00\x01not valid utf-8")

    with pytest.raises(PrivateStorageError) as excinfo:
        storage.content_type(key)

    assert key not in str(excinfo.value)


def test_storage_root_stays_outside_the_given_directory_boundary(tmp_path) -> None:
    served_dir = tmp_path / "static"
    served_dir.mkdir()
    private_root = tmp_path / "var" / "private"
    storage = PrivateFileSystemStorage(private_root)

    key = storage.save(io.BytesIO(b"secret"), content_type="application/octet-stream")

    assert not (served_dir / key).exists()
    assert (private_root / key).exists()
