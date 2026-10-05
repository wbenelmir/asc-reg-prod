"""P4-4-C1 (review finding R-02): the private-storage readiness probe.

Before this correction the probe asked `exists()` for a key that is never
created and ignored the answer. `PrivateFileSystemStorage.exists()` returns
False when its root is missing, so a missing root was reported "reachable".
An S3 HEAD on an absent key has the same blind spot: it answers 404 whether
the object or the whole bucket is absent.

These tests use synthetic temporary roots and botocore's `Stubber`, which
answers each S3 call in-process: no request leaves the test, no provider is
contacted, and no real project document is read or written.
"""

from __future__ import annotations

import io
import json
from unittest.mock import patch

import pytest
from botocore.stub import Stubber
from django.test import Client

from apps.documents.storage import (
    READINESS_PROBE_KEY,
    PrivateFileSystemStorage,
    PrivateStorage,
    PrivateStorageError,
    S3PrivateStorage,
    check_private_storage_ready,
)
from scripts.check import SYNTHETIC_DEPLOY_ENV

BUCKET = "p44c1-synthetic-bucket"
ENDPOINT = "https://s3.p44c1.example.invalid"
#: The scanner's own documented, never-real sentinel values (scripts/check.py).
SYNTHETIC_S3 = {
    "access_key": SYNTHETIC_DEPLOY_ENV["S3_STORAGE_ACCESS_KEY_ID"],
    "secret_key": SYNTHETIC_DEPLOY_ENV["S3_STORAGE_SECRET_ACCESS_KEY"],
}


def _listing(root) -> dict[str, tuple[int, int]]:
    return {p.name: (p.stat().st_size, p.stat().st_mtime_ns) for p in root.iterdir()}


def _assert_generic(exc: BaseException, *fragments: str) -> None:
    text = str(exc)
    for fragment in fragments:
        assert fragment not in text
    assert exc.__cause__ is None


# ---------------------------------------------------------------------------
# Filesystem backend
# ---------------------------------------------------------------------------


def test_the_reported_false_positive_is_closed(tmp_path) -> None:
    """The review's reproduction: `exists()` answers False for a missing root
    without raising. That answer is unchanged (it is the healthy answer for an
    absent object), but readiness no longer accepts it."""
    storage = PrivateFileSystemStorage(tmp_path / "missing-root")
    assert storage.exists(READINESS_PROBE_KEY) is False
    with pytest.raises(PrivateStorageError) as caught:
        check_private_storage_ready(storage)
    _assert_generic(caught.value, "missing-root", str(tmp_path))
    assert not (tmp_path / "missing-root").exists(), "the probe must not create the root"


def test_a_healthy_empty_root_with_the_object_absent_is_ready(tmp_path) -> None:
    root = tmp_path / "private"
    root.mkdir()
    check_private_storage_ready(PrivateFileSystemStorage(root))
    assert list(root.iterdir()) == []


def test_the_probe_reads_and_writes_no_object(tmp_path) -> None:
    root = tmp_path / "private"
    root.mkdir()
    storage = PrivateFileSystemStorage(root)
    storage.save(io.BytesIO(b"synthetic bytes"), "application/octet-stream")
    before = _listing(root)
    with patch("builtins.open", side_effect=AssertionError("the probe opened a file")):
        check_private_storage_ready(storage)
    assert _listing(root) == before


def test_a_root_that_is_a_file_is_not_ready(tmp_path) -> None:
    root = tmp_path / "not-a-directory"
    root.write_bytes(b"")
    with pytest.raises(PrivateStorageError) as caught:
        check_private_storage_ready(PrivateFileSystemStorage(root))
    _assert_generic(caught.value, "not-a-directory")


def test_a_root_without_access_rights_is_not_ready(tmp_path) -> None:
    root = tmp_path / "private"
    root.mkdir()
    with (
        patch("apps.documents.storage.os.access", return_value=False),
        pytest.raises(PrivateStorageError),
    ):
        check_private_storage_ready(PrivateFileSystemStorage(root))


def test_a_root_that_cannot_be_listed_is_not_ready_and_hides_the_os_error(tmp_path) -> None:
    root = tmp_path / "private"
    root.mkdir()
    denied = PermissionError(13, "Permission denied", str(root))
    with (
        patch("apps.documents.storage.os.scandir", side_effect=denied),
        pytest.raises(PrivateStorageError) as caught,
    ):
        check_private_storage_ready(PrivateFileSystemStorage(root))
    _assert_generic(caught.value, str(root), "Permission denied")


def test_an_adapter_failure_is_not_ready_and_generic(tmp_path) -> None:
    root = tmp_path / "private"
    root.mkdir()
    with (
        patch.object(
            PrivateFileSystemStorage, "exists", side_effect=RuntimeError("host=fs.internal")
        ),
        pytest.raises(PrivateStorageError) as caught,
    ):
        check_private_storage_ready(PrivateFileSystemStorage(root))
    _assert_generic(caught.value, "fs.internal", "RuntimeError")


def test_a_backend_without_a_readiness_check_fails_closed() -> None:
    class Unknown(PrivateStorage):
        def save(self, content, content_type, *, suggested_name=""):
            raise NotImplementedError

        def open(self, key):
            raise NotImplementedError

        def exists(self, key):
            return False

        def delete(self, key):
            raise NotImplementedError

        def size(self, key):
            raise NotImplementedError

        def content_type(self, key):
            raise NotImplementedError

    with pytest.raises(PrivateStorageError):
        check_private_storage_ready(Unknown())


# ---------------------------------------------------------------------------
# S3 backend (real django-storages adapter, botocore Stubber, no network)
# ---------------------------------------------------------------------------


def _stubbed_s3() -> tuple[S3PrivateStorage, Stubber]:
    storage = S3PrivateStorage(
        bucket_name=BUCKET,
        endpoint_url=ENDPOINT,
        region_name="us-east-1",
        **SYNTHETIC_S3,
    )
    stubber = Stubber(storage._backend.connection.meta.client)
    stubber.activate()
    return storage, stubber


def test_s3_a_reachable_bucket_with_the_object_absent_is_ready() -> None:
    storage, stubber = _stubbed_s3()
    stubber.add_response("head_bucket", {}, {"Bucket": BUCKET})
    stubber.add_client_error("head_object", "404", http_status_code=404)
    check_private_storage_ready(storage)
    stubber.assert_no_pending_responses()


def test_s3_an_absent_bucket_is_not_ready() -> None:
    """The S3 form of the false positive: the HEAD on the absent probe key
    alone would also have answered 404."""
    storage, stubber = _stubbed_s3()
    stubber.add_client_error("head_bucket", "404", http_status_code=404)
    with pytest.raises(PrivateStorageError) as caught:
        check_private_storage_ready(storage)
    _assert_generic(caught.value, BUCKET, ENDPOINT, "404", "ClientError")


@pytest.mark.parametrize(
    ("bucket_error", "object_error"),
    [
        (("AccessDenied", 403), None),
        (None, ("AccessDenied", 403)),
        (None, ("InternalError", 500)),
    ],
)
def test_s3_refused_or_failing_calls_are_not_ready(bucket_error, object_error) -> None:
    storage, stubber = _stubbed_s3()
    if bucket_error:
        stubber.add_client_error("head_bucket", bucket_error[0], http_status_code=bucket_error[1])
    else:
        stubber.add_response("head_bucket", {}, {"Bucket": BUCKET})
        stubber.add_client_error("head_object", object_error[0], http_status_code=object_error[1])
    with pytest.raises(PrivateStorageError) as caught:
        check_private_storage_ready(storage)
    _assert_generic(caught.value, BUCKET, ENDPOINT, "AccessDenied", "InternalError")


# ---------------------------------------------------------------------------
# The /readyz view
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_readyz_reports_a_missing_local_root_as_unreachable(settings, tmp_path) -> None:
    settings.PRIVATE_STORAGE_BACKEND = "filesystem"
    settings.PRIVATE_STORAGE_ROOT = tmp_path / "p44c1-missing"
    response = Client().get("/readyz")
    assert response.status_code == 503
    assert json.loads(response.content) == {
        "status": "unavailable",
        "database": "reachable",
        "storage": "unreachable",
        "challenge_counter": "local",
    }
    assert "no-store" in response["Cache-Control"]
    assert "p44c1-missing" not in response.content.decode()


@pytest.mark.django_db
def test_readyz_reports_a_healthy_root_as_reachable(settings, tmp_path) -> None:
    root = tmp_path / "private"
    root.mkdir()
    settings.PRIVATE_STORAGE_BACKEND = "filesystem"
    settings.PRIVATE_STORAGE_ROOT = root
    response = Client().get("/readyz")
    assert response.status_code == 200
    assert json.loads(response.content)["storage"] == "reachable"


@pytest.mark.django_db
def test_readyz_reports_an_absent_s3_bucket_as_unreachable_without_detail() -> None:
    storage, stubber = _stubbed_s3()
    stubber.add_client_error("head_bucket", "NoSuchBucket", http_status_code=404)
    with patch("apps.documents.storage.get_private_storage", return_value=storage):
        response = Client().get("/readyz")
    assert response.status_code == 503
    body = response.content.decode()
    assert json.loads(body)["storage"] == "unreachable"
    for fragment in (BUCKET, "example.invalid", "NoSuchBucket"):
        assert fragment not in body
