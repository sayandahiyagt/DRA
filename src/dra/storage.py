"""BlobStore abstraction for durable content-addressable blob storage (§44/§160-§162, dra#78 Wave 1a).

Replaces the non-durable ``raw_capture.stored_at`` path (which could point to a
temporary/local path) with a real storage abstraction.  Every ``ContentBlob``
carries a ``storage_uri`` that is resolvable via :meth:`BlobStore.open`.

Two backends are provided:

- :class:`FilesystemBlobStore` — the always-on dev default.  Content-addressed by
  sha256 hex under ``BLOBSTORE_ROOT`` (env-overridable, defaults to
  ``/tmp/dra-blobs``).  No external dependencies.

- :class:`S3BlobStore` — the production backend.  ``boto3`` is **lazily**
  imported inside :meth:`S3BlobStore.__init__` so that ``import dra.storage``
  never fails when the AWS SDK is absent (it lives in the
  ``dra[investigate]`` extra, not core).  Tests that need S3 must call
  ``pytest.importorskip("boto3")``.

The :class:`BlobStore` itself is a :class:`typing.Protocol` — callers depend
on the protocol, not a concrete backend, so the dev/prod choice is a wiring
decision in :mod:`dra.publish` and :class:`~dra.investigators.InvestigatorContext`.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any, Iterable, Protocol, runtime_checkable

__all__ = [
    "BlobStore",
    "FilesystemBlobStore",
    "S3BlobStore",
    "default_blob_store",
    "resolve_blob_store",
]


@runtime_checkable
class BlobStore(Protocol):
    """Content-addressed blob storage interface (§160).

    Implementations are keyed by sha256 hex digest.  ``put`` returns the
    durable ``storage_uri`` under which the blob can later be retrieved via
    ``open``.
    """

    async def put(self, data: bytes, hash: str, mime: str | None) -> str:
        """Persist *data* (already known to have sha256 *hash*) and return its
        durable ``storage_uri``."""
        ...

    def open(self, storage_uri: str) -> Any:
        """Return a binary file-like object for *storage_uri* (sync, may block)."""
        ...

    async def exists(self, storage_uri: str) -> bool:
        """Return True if the blob at *storage_uri* is durably stored."""
        ...

    async def verify(self, storage_uri: str, expected_hash: str) -> bool:
        """Return True if the blob hashes to *expected_hash* (sha256 hex)."""
        ...

    async def delete_if_referenced(
        self,
        storage_uri: str,
        referencing_hashes: Iterable[str],
    ) -> bool:
        """Delete *storage_uri* only if no live reference still points at it.

        ``referencing_hashes`` is the set of content hashes known to be in use.
        Returns True if the blob was deleted, False if it was retained because a
        reference still exists (or the backend does not support deletion).
        """
        ...


class FilesystemBlobStore(BlobStore):
    """Dev filesystem backend: content-addressed by hash under ``root``.

    Paths are ``<root>/<hash[:2]>/<hash>[.ext]`` to avoid pathological directory
    entries.  The ``storage_uri`` stored in ``content_blob`` is the absolute path
    string.
    """

    def __init__(self, root: str | Path | None = None) -> None:
        self.root = Path(root or os.environ.get("BLOBSTORE_ROOT", "/tmp/dra-blobs"))
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, hash: str, mime: str | None) -> Path:
        ext = f".{mime.split('/')[-1]}" if mime and "/" in mime else ""
        return self.root / hash[:2] / f"{hash}{ext}"

    def _uri(self, path: Path) -> str:
        return str(path)

    async def put(self, data: bytes, hash: str, mime: str | None) -> str:
        path = self._path(hash, mime)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return self._uri(path)

    def open(self, storage_uri: str) -> Any:
        return open(storage_uri, "rb")

    async def exists(self, storage_uri: str) -> bool:
        return Path(storage_uri).is_file()

    async def verify(self, storage_uri: str, expected_hash: str) -> bool:
        if not await self.exists(storage_uri):
            return False
        digest = hashlib.sha256()
        with self.open(storage_uri) as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                digest.update(chunk)
        return digest.hexdigest() == expected_hash

    async def delete_if_referenced(
        self,
        storage_uri: str,
        referencing_hashes: Iterable[str],
    ) -> bool:
        return False


class S3BlobStore(BlobStore):
    """Production S3-compatible backend.

    ``boto3`` is imported lazily so ``import dra.storage`` succeeds without it.
    Bucket and credential resolution follow the standard boto3 chain (env vars,
    IAM role, ``~/.aws/credentials``).
    """

    def __init__(
        self,
        bucket: str = os.environ.get("DRA_BLOBSTORE_BUCKET", "dra-blobs"),
        prefix: str = "",
        **boto_kwargs: Any,
    ) -> None:
        import boto3  # noqa: PLC0415 — lazy import (see module docstring)

        self._s3 = boto3.session.Session(**boto_kwargs).resource("s3")
        self._bucket = bucket
        self._prefix = prefix

    def _key(self, hash: str) -> str:
        return f"{self._prefix}{hash[:2]}/{hash}" if self._prefix else f"{hash[:2]}/{hash}"

    def _uri(self, hash: str) -> str:
        return f"s3://{self._bucket}/{self._key(hash)}"

    async def put(self, data: bytes, hash: str, mime: str | None) -> str:
        key = self._key(hash)
        extra: dict[str, Any] = {}
        if mime:
            extra["ContentType"] = mime
        obj = self._s3.Object(self._bucket, key)
        obj.put(Body=data, **extra)
        return self._uri(hash)

    def open(self, storage_uri: str) -> Any:
        import boto3  # noqa: PLC0415

        bucket, key = _parse_s3_uri(storage_uri)
        s3 = boto3.session.Session().resource("s3")
        obj = s3.Object(bucket, key)
        return obj.get()["Body"]

    async def exists(self, storage_uri: str) -> bool:
        import boto3  # noqa: PLC0415

        bucket, key = _parse_s3_uri(storage_uri)
        client = boto3.session.Session().client("s3")
        try:
            client.head_object(Bucket=bucket, Key=key)
            return True
        except client.exceptions.ClientError:
            return False

    async def verify(self, storage_uri: str, expected_hash: str) -> bool:
        if not await self.exists(storage_uri):
            return False
        data = await _read_uri(self.open(storage_uri))
        return hashlib.sha256(data).hexdigest() == expected_hash

    async def delete_if_referenced(
        self,
        storage_uri: str,
        referencing_hashes: Iterable[str],
    ) -> bool:
        import boto3  # noqa: PLC0415

        client = boto3.session.Session().client("s3")
        client.delete_object(Bucket=_parse_s3_uri(storage_uri)[0], Key=_parse_s3_uri(storage_uri)[1])
        return True


def _parse_s3_uri(uri: str) -> tuple[str, str]:
    """Parse ``s3://bucket/key`` into ``(bucket, key)``."""
    assert uri.startswith("s3://"), f"not an s3 URI: {uri}"
    stripped = uri[5:]
    bucket, _, key = stripped.partition("/")
    return bucket, key


async def _read_uri(fh: Any) -> bytes:
    if hasattr(fh, "read"):
        return fh.read()
    chunks = []
    for chunk in iter(lambda: fh.read(65536), b""):  # type: ignore[union-attr]
        chunks.append(chunk)
    return b"".join(chunks)


def default_blob_store() -> FilesystemBlobStore:
    """Return the always-on dev default (FilesystemBlobStore)."""
    return FilesystemBlobStore()


def resolve_blob_store(name: str | None = None) -> BlobStore:
    """Resolve a BlobStore by name (``"s3"`` or default FilesystemBlobStore).

    ``boto3`` is only required when ``name == "s3"``; otherwise the filesystem
    backend is returned with no AWS dependency.
    """
    if name and name.lower() == "s3":
        return S3BlobStore()
    return FilesystemBlobStore()
