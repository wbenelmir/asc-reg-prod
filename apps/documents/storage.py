"""Private, purpose-bound object storage abstraction.

Schema §7.2/§17.2 and TRD `SEC-006`: protected file bytes live in encrypted
private object storage, never in PostgreSQL, and access uses short-lived
authorization plus audit evidence.

`PrivateStorage` intentionally has **no public-URL method of any kind**.
This is a structural guarantee, not a convention: nothing in this interface
can produce a link. Bytes can only be retrieved through application code
that deliberately opens a stored object after an authorization decision has
been made -- the authenticated, policy-checked, audited streaming view
built in Prompt 4, with the exact denial behavior specified in the accepted
plan §5.12.

Local and test use `PrivateFileSystemStorage`, rooted at `var/private/`,
outside `static/`, `staticfiles/`, any `MEDIA_ROOT`, and every served path.
Staging and production select `S3PrivateStorage`, an S3-compatible backend
behind the identical interface (see `config/settings/staging.py` /
`production.py` and ADR-0013). It is implemented below, configured
explicitly from this project's own `S3_STORAGE_*` settings, but its
integration is **never exercised against a real S3-compatible service by
this project's own local test suite** -- Prompt 2 correction §1 requires
isolated tests using mocks/fakes, never a real network call, and that
constraint is honored throughout `tests/foundation/test_s3_storage.py`.

No `Document` or `StoredObject` model exists yet (Prompt 3) -- this module
provides only the storage abstraction and its two implementations.
"""

from __future__ import annotations

import abc
import json
import os
import stat
import uuid
from pathlib import Path
from typing import BinaryIO


class PrivateStorageError(Exception):
    """Raised for storage-layer failures (never includes file bytes or paths in logs)."""


class ObjectNotFound(PrivateStorageError):
    """Raised when `open`/`size`/`content_type`/`delete` is given an unknown key."""


class PrivateStorage(abc.ABC):
    """Storage abstraction for Restricted-classification document bytes.

    Implementations MUST NOT expose a public URL method of any kind, MUST
    NOT be reachable through a web server's static/media routing, and MUST
    NOT place object bytes in PostgreSQL.
    """

    @abc.abstractmethod
    def save(self, content: BinaryIO, content_type: str, *, suggested_name: str = "") -> str:
        """Persist `content`; return the storage key the caller must retain.

        The returned key is an opaque handle. It is not a public identifier
        and it is never sent to a participant or embedded in a URL.
        """

    @abc.abstractmethod
    def open(self, key: str) -> BinaryIO:
        """Return a readable binary stream for the object at `key`."""

    @abc.abstractmethod
    def exists(self, key: str) -> bool: ...

    @abc.abstractmethod
    def delete(self, key: str) -> None: ...

    @abc.abstractmethod
    def size(self, key: str) -> int: ...

    @abc.abstractmethod
    def content_type(self, key: str) -> str: ...

    def _check_ready(self, probe_key: str) -> None:
        """Readiness hook for `check_private_storage_ready()` (P4-4-C1, R-02).

        Raises `PrivateStorageError` when the backend cannot serve requests.
        An implementation that does not define its own check is never
        reported ready: an unknown backend fails closed."""
        raise PrivateStorageError("Private storage readiness is not defined for this backend.")


class PrivateFileSystemStorage(PrivateStorage):
    """Local development / test implementation.

    Rooted at an application-chosen directory (`var/private/` locally,
    a `tmp_path`-derived directory in tests) that is never a Django
    STATICFILES_DIRS entry, never STATIC_ROOT, and never MEDIA_ROOT.

    A small JSON sidecar (`<key>.meta.json`) records the declared content
    type next to each object, since plain filesystem storage has no other
    place to keep it. The sidecar contains no personal data -- only the
    content type string.
    """

    _META_SUFFIX = ".meta.json"

    def __init__(self, root: str | Path) -> None:
        self._root = Path(root)

    def _ensure_root(self) -> None:
        try:
            self._root.mkdir(parents=True, exist_ok=True)
        except OSError:
            raise PrivateStorageError("Local private storage root could not be prepared.") from None
        if os.name != "nt":
            # Best-effort restrictive permissions; Windows ACLs are a
            # separate, later concern (accepted plan §5.7 applies this
            # standard specifically to var/mail/, not to this class).
            try:
                os.chmod(self._root, stat.S_IRWXU)
            except OSError:
                pass

    def _path_for(self, key: str) -> Path:
        # Keys are opaque UUID-derived names; this rejects path traversal
        # by construction rather than by pattern-matching a "bad" input.
        # The message deliberately never echoes the rejected key back.
        if "/" in key or "\\" in key or key in {".", ".."}:
            raise PrivateStorageError("Storage key must not contain path separators.")
        return self._root / key

    def save(self, content: BinaryIO, content_type: str, *, suggested_name: str = "") -> str:
        self._ensure_root()
        key = uuid.uuid4().hex
        path = self._path_for(key)
        try:
            with open(path, "wb") as fh:
                fh.write(content.read())
        except Exception:
            # Cleanup-safe: opening a file in "wb" mode creates/truncates
            # it immediately, before any bytes are written -- a failure
            # anywhere in this block (a real disk error, or `content`
            # itself raising while being read) can still leave an empty
            # or partially written primary file behind. Never leave that
            # partial artifact (Prompt 2 final closure pass §7).
            # Best-effort only -- a failure here must not replace or leak
            # through the save-failure message below.
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
            raise PrivateStorageError("Local private storage save operation failed.") from None
        meta_path = self._path_for(key + self._META_SUFFIX)
        try:
            meta_path.write_text(json.dumps({"content_type": content_type}), encoding="utf-8")
        except OSError:
            # Cleanup-safe: never leave an orphaned primary object behind
            # when its metadata sidecar could not be written (Prompt 2
            # correction §8). Best-effort only -- a failure here must not
            # replace or leak through the save-failure message below.
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
            raise PrivateStorageError("Local private storage save operation failed.") from None
        return key

    def open(self, key: str) -> BinaryIO:
        path = self._path_for(key)
        if not path.is_file():
            raise ObjectNotFound("Requested object was not found.")
        try:
            return open(path, "rb")
        except OSError:
            raise PrivateStorageError("Local private storage open operation failed.") from None

    def exists(self, key: str) -> bool:
        return self._path_for(key).is_file()

    def delete(self, key: str) -> None:
        path = self._path_for(key)
        try:
            path.unlink(missing_ok=True)
            self._path_for(key + self._META_SUFFIX).unlink(missing_ok=True)
        except OSError:
            raise PrivateStorageError("Local private storage delete operation failed.") from None

    def size(self, key: str) -> int:
        path = self._path_for(key)
        if not path.is_file():
            raise ObjectNotFound("Requested object was not found.")
        try:
            return path.stat().st_size
        except OSError:
            raise PrivateStorageError("Local private storage size lookup failed.") from None

    def content_type(self, key: str) -> str:
        meta_path = self._path_for(key + self._META_SUFFIX)
        if not meta_path.is_file():
            raise ObjectNotFound("Requested object was not found.")
        try:
            data = json.loads(meta_path.read_text(encoding="utf-8"))
            value = data["content_type"]
            # A non-string "content_type" (wrong metadata TYPE, not just
            # wrong shape) must fail closed the same as every other
            # malformed-metadata condition here, never be returned as if
            # it satisfied this method's `-> str` contract (Prompt 2
            # final closure pass §3).
            if not isinstance(value, str):
                raise TypeError("content_type metadata field is not a string")
            return value
        except (OSError, ValueError, KeyError, TypeError):  # fmt: skip
            raise PrivateStorageError("Local private storage content-type lookup failed.") from None

    def _check_ready(self, probe_key: str) -> None:
        # `exists()` answers False both for a healthy absent object and for a
        # missing root, so the root itself is checked first (R-02). Nothing
        # is created, written, listed beyond one entry, or read.
        try:
            if not self._root.is_dir():
                raise PrivateStorageError("Local private storage root is unavailable.")
            if not os.access(self._root, os.R_OK | os.W_OK | os.X_OK):
                raise PrivateStorageError("Local private storage root is not usable.")
            with os.scandir(self._root) as entries:
                next(entries, None)
        except PrivateStorageError:
            raise
        except OSError:
            raise PrivateStorageError("Local private storage root is not usable.") from None
        # The same lookup path as a real request; the probe key never exists,
        # so False is the healthy answer.
        self.exists(probe_key)


class S3PrivateStorage(PrivateStorage):
    """S3-compatible private object storage (staging/production only).

    Wraps django-storages' `S3Boto3Storage`, constructed with EVERY
    credential and endpoint value passed explicitly from this project's own
    `S3_STORAGE_*` settings. Explicit constructor arguments take precedence
    over boto3's own environment-variable credential resolution (`AWS_
    ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_DEFAULT_REGION`, etc.), so
    this class cannot accidentally pick up an unrelated ambient `AWS_*`
    value -- it is passed nothing that would let it fall back to one.

    Objects are private by construction: `default_acl="private"` is fixed,
    not configurable, and `querystring_auth=False` means this class never
    generates a signed (or any other) URL -- consistent with `PrivateStorage`
    exposing no public-URL method at all.

    A small JSON sidecar object (`<key>.meta.json`) records the declared
    content type next to each object, mirroring `PrivateFileSystemStorage`,
    so both implementations satisfy `content_type()` identically without
    requiring a HEAD request or S3-specific object metadata API.

    Every method wraps the underlying S3 client call and re-raises a
    generic `PrivateStorageError` naming only the operation, using
    `raise ... from None` to break the exception chain deliberately: the
    original exception's own message (from botocore or the provider) is
    replaced, not merely hidden from `str()`, so it cannot resurface later
    through `traceback.format_exc()` or a structured-logging exception
    dump either. Neither an access key, a secret key, object contents, nor
    an unnecessary storage key ever appears in any exception this class
    raises.
    """

    _META_SUFFIX = ".meta.json"

    def __init__(
        self,
        *,
        bucket_name: str,
        endpoint_url: str,
        access_key: str,
        secret_key: str,
        region_name: str,
    ) -> None:
        from storages.backends.s3boto3 import S3Boto3Storage

        self._backend = S3Boto3Storage(
            bucket_name=bucket_name,
            endpoint_url=endpoint_url,
            access_key=access_key,
            secret_key=secret_key,
            region_name=region_name,
            default_acl="private",
            querystring_auth=False,
            file_overwrite=False,
        )

    @staticmethod
    def _key_for(key: str) -> str:
        # Same structural rejection of path traversal as PrivateFileSystemStorage.
        if "/" in key or "\\" in key or key in {".", ".."}:
            raise PrivateStorageError("Storage key must not contain path separators.")
        return key

    def save(self, content: BinaryIO, content_type: str, *, suggested_name: str = "") -> str:
        from django.core.files.base import ContentFile

        key = uuid.uuid4().hex
        try:
            self._backend.save(key, ContentFile(content.read()))
        except Exception:
            raise PrivateStorageError("S3 storage save operation failed.") from None
        try:
            self._backend.save(
                key + self._META_SUFFIX,
                ContentFile(json.dumps({"content_type": content_type}).encode("utf-8")),
            )
        except Exception:
            # Cleanup-safe: never leave an orphaned primary object OR an
            # orphaned metadata sidecar behind when the metadata write
            # fails (Prompt 2 correction §8, extended in the final
            # closure pass to also attempt the sidecar: some provider
            # failure modes -- a timeout after the object was already
            # accepted, a retried request that partially succeeded --
            # can leave the sidecar partially created even though the
            # call that "failed" raised). Each cleanup attempt is
            # independent and best-effort -- a failure in either one (or
            # both) must not replace or leak through the save-failure
            # message below.
            try:
                self._backend.delete(key)
            except Exception:  # noqa: S110 - best-effort cleanup; the outer
                # raise below always reports the original save failure
                # generically regardless of whether this cleanup succeeds.
                pass
            try:
                self._backend.delete(key + self._META_SUFFIX)
            except Exception:  # noqa: S110 - best-effort cleanup; see above.
                pass
            raise PrivateStorageError("S3 storage save operation failed.") from None
        return key

    def open(self, key: str) -> BinaryIO:
        object_key = self._key_for(key)
        try:
            exists = self._backend.exists(object_key)
        except Exception:
            raise PrivateStorageError("S3 storage existence check failed.") from None
        if not exists:
            raise ObjectNotFound("Requested object was not found.")
        try:
            return self._backend.open(object_key, "rb")
        except Exception:
            raise PrivateStorageError("S3 storage open operation failed.") from None

    def exists(self, key: str) -> bool:
        try:
            return self._backend.exists(self._key_for(key))
        except Exception:
            raise PrivateStorageError("S3 storage existence check failed.") from None

    def delete(self, key: str) -> None:
        object_key = self._key_for(key)
        try:
            self._backend.delete(object_key)
            self._backend.delete(object_key + self._META_SUFFIX)
        except Exception:
            raise PrivateStorageError("S3 storage delete operation failed.") from None

    def size(self, key: str) -> int:
        object_key = self._key_for(key)
        try:
            exists = self._backend.exists(object_key)
            if not exists:
                raise ObjectNotFound("Requested object was not found.")
            return self._backend.size(object_key)
        except ObjectNotFound:
            raise
        except Exception:
            raise PrivateStorageError("S3 storage size lookup failed.") from None

    def content_type(self, key: str) -> str:
        object_key = self._key_for(key)
        meta_key = object_key + self._META_SUFFIX
        try:
            exists = self._backend.exists(meta_key)
            if not exists:
                raise ObjectNotFound("Requested object was not found.")
            with self._backend.open(meta_key, "rb") as handle:
                data = json.loads(handle.read().decode("utf-8"))
            # Reading the field is deliberately INSIDE this try block, not
            # after it: malformed JSON, an unexpected top-level metadata
            # type (list, number, ...), or a missing "content_type" key
            # must all become the same generic `PrivateStorageError` --
            # never a raw `KeyError`/`TypeError`/decoding exception, the
            # underlying provider exception, the object key, or the
            # metadata content itself (Prompt 2 correction §2, v3). A
            # non-string "content_type" (wrong metadata TYPE, not just
            # wrong shape) is checked explicitly and fails the same way,
            # rather than being returned as if it satisfied this method's
            # `-> str` contract (Prompt 2 final closure pass §3).
            value = data["content_type"]
            if not isinstance(value, str):
                raise TypeError("content_type metadata field is not a string")
            return value
        except ObjectNotFound:
            raise
        except Exception:
            raise PrivateStorageError("S3 storage content-type lookup failed.") from None

    def _check_ready(self, probe_key: str) -> None:
        # A HEAD on an absent key answers 404 both when the object is absent
        # and when the bucket itself does not exist, so the bucket is checked
        # first (R-02). `head_bucket` needs the `s3:ListBucket` permission on
        # this bucket. Neither call reads, writes or deletes an object.
        try:
            self._backend.connection.meta.client.head_bucket(Bucket=self._backend.bucket_name)
            self._backend.exists(self._key_for(probe_key))
        except Exception:
            raise PrivateStorageError("S3 storage readiness check failed.") from None


#: A key that is never created. Asking for it exercises the lookup path; the
#: healthy answer is "absent" (R-02).
READINESS_PROBE_KEY = "asc-readiness-probe"


def check_private_storage_ready(storage: PrivateStorage | None = None) -> None:
    """Raise `PrivateStorageError` unless the configured private storage can
    serve requests (P4-4-C1, R-02). Used by `/readyz`.

    What it proves, per backend:

    * filesystem: the configured root exists, is a directory, the process has
      read, write and search permission on it (as the operating system
      reports), it can be listed, and a key lookup completes;
    * S3: the endpoint answers, the credentials are accepted, the bucket exists
      and `HeadBucket` is permitted, and a HEAD on an absent key completes.

    What it does not prove: that an upload, a read of an existing object or a
    delete succeeds (object-level permissions, free space, quotas, encryption
    settings). Nothing is written, so a probe can run on every health check.
    """
    try:
        storage = get_private_storage() if storage is None else storage
        storage._check_ready(READINESS_PROBE_KEY)
    except PrivateStorageError:
        raise
    except Exception:
        raise PrivateStorageError("Private storage readiness check failed.") from None


def get_private_storage() -> PrivateStorage:
    """Return the configured `PrivateStorage` implementation.

    Reads `settings.PRIVATE_STORAGE_BACKEND` ("filesystem" | "s3"). The "s3"
    branch is fully wired and configured explicitly from this project's own
    `S3_STORAGE_*` settings (never from ambient `AWS_*` environment
    variables), but is not exercised against a real S3-compatible service
    by Phase 1's own local test suite (accepted plan §5.11) -- only isolated
    mock/fake-backed tests (`tests/foundation/test_s3_storage.py`).
    """
    from django.conf import settings

    backend = settings.PRIVATE_STORAGE_BACKEND
    if backend == "filesystem":
        return PrivateFileSystemStorage(settings.PRIVATE_STORAGE_ROOT)
    if backend == "s3":
        return S3PrivateStorage(
            bucket_name=settings.S3_STORAGE_BUCKET_NAME,
            endpoint_url=settings.S3_STORAGE_ENDPOINT_URL,
            access_key=settings.S3_STORAGE_ACCESS_KEY_ID,
            secret_key=settings.S3_STORAGE_SECRET_ACCESS_KEY,
            region_name=settings.S3_STORAGE_REGION_NAME,
        )
    raise PrivateStorageError(f"Unknown PRIVATE_STORAGE_BACKEND: {backend!r}")
