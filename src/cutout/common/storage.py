from __future__ import annotations

import os
import shutil
from collections.abc import AsyncIterator, Iterator
from email.header import decode_header, make_header
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import BinaryIO, Protocol, runtime_checkable

import boto3
from botocore.client import Config
from botocore.exceptions import ClientError
from starlette.concurrency import run_in_threadpool

from ..config import Settings

# Error codes S3 implementations use for a missing object.
_MISSING_CODES = {"NoSuchKey", "NoSuchBucket", "404", "NotFound"}


def _decode_metadata_value(value: str) -> str:
    """Decode an RFC 2047 ``encoded-word`` metadata value back to plain text.

    R2 stores any user-metadata value containing non-US-ASCII bytes as an
    ``encoded-word`` (``=?utf-8?Q?...?=``) and returns it still encoded on
    ``head_object``. Plain-ASCII values are unaffected, so this is a no-op for
    them and only unwraps the ones R2 mangled.
    """
    if "=?" not in value:
        return value
    try:
        return str(make_header(decode_header(value)))
    except (ValueError, LookupError):
        return value


@runtime_checkable
class Storage(Protocol):
    """Object-storage surface used by the HTTP server and the worker."""

    async def get_bytes(self, key: str) -> bytes | None:
        """Return the object's bytes, or ``None`` if it does not exist."""
        ...

    async def head(self, key: str) -> dict[str, str] | None:
        """Return the object's user metadata (lowercased keys), or ``None``."""
        ...

    async def list_keys(self, prefix: str) -> set[str]:
        """Return every object key under ``prefix``."""
        ...

    async def delete(self, key: str) -> None:
        """Delete the object at ``key``; a no-op if it does not exist."""
        ...

    async def put(
        self,
        key: str,
        body: BinaryIO,
        *,
        cache_control: str | None = None,
        content_type: str | None = None,
    ) -> None:
        """Store ``body`` (a file-like object, read from its current position)."""
        ...

    async def put_bytes(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> None:
        """Store in-memory ``data`` (used for the small feed XML document)."""
        ...


class S3Storage:
    """Storage backed by an S3-compatible service."""

    def __init__(self, settings: Settings) -> None:
        self._bucket = settings.s3_bucket
        # Path-style addressing works for R2, MinIO and AWS S3 alike.
        self._client = boto3.client(
            "s3",
            endpoint_url=settings.s3_endpoint_url,
            aws_access_key_id=settings.s3_access_key_id,
            aws_secret_access_key=settings.s3_secret_access_key,
            region_name=settings.s3_region,
            config=Config(
                signature_version="s3v4",
                s3={"addressing_style": "path"},
            ),
        )

    async def get_bytes(self, key: str) -> bytes | None:
        return await run_in_threadpool(self._get_bytes, key)

    def _get_bytes(self, key: str) -> bytes | None:
        try:
            resp = self._client.get_object(Bucket=self._bucket, Key=key)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in _MISSING_CODES:
                return None
            raise
        return resp["Body"].read()

    async def head(self, key: str) -> dict[str, str] | None:
        return await run_in_threadpool(self._head, key)

    def _head(self, key: str) -> dict[str, str] | None:
        try:
            resp = self._client.head_object(Bucket=self._bucket, Key=key)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in _MISSING_CODES:
                return None
            raise
        # S3 already lowercases user-metadata keys; normalise to be safe, and
        # undo any RFC 2047 encoding R2 applied to non-ASCII values.
        return {
            k.lower(): _decode_metadata_value(v)
            for k, v in (resp.get("Metadata") or {}).items()
        }

    async def list_keys(self, prefix: str) -> set[str]:
        return await run_in_threadpool(self._list_keys, prefix)

    def _list_keys(self, prefix: str) -> set[str]:
        keys: set[str] = set()
        token: str | None = None
        while True:
            kwargs: dict = {"Bucket": self._bucket, "Prefix": prefix}
            if token:
                kwargs["ContinuationToken"] = token
            resp = self._client.list_objects_v2(**kwargs)
            for obj in resp.get("Contents", []):
                keys.add(obj["Key"])
            if not resp.get("IsTruncated"):
                break
            token = resp.get("NextContinuationToken")
        return keys

    async def delete(self, key: str) -> None:
        await run_in_threadpool(self._delete, key)

    def _delete(self, key: str) -> None:
        # delete_object is idempotent — deleting a missing key is not an error.
        self._client.delete_object(Bucket=self._bucket, Key=key)

    async def put(
        self,
        key: str,
        body: BinaryIO,
        *,
        cache_control: str | None = None,
        content_type: str | None = None,
    ) -> None:
        await run_in_threadpool(self._put, key, body, cache_control, content_type)

    def _put(
        self,
        key: str,
        body: BinaryIO,
        cache_control: str | None,
        content_type: str | None,
    ) -> None:
        extra: dict[str, str] = {}
        if cache_control is not None:
            extra["CacheControl"] = cache_control
        if content_type is not None:
            extra["ContentType"] = content_type
        body.seek(0)
        # upload_fileobj streams in parts, so large episodes never sit fully
        # in memory.
        self._client.upload_fileobj(body, self._bucket, key, ExtraArgs=extra or None)

    async def put_bytes(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> None:
        await run_in_threadpool(self._put_bytes, key, data, content_type, metadata)

    def _put_bytes(
        self,
        key: str,
        data: bytes,
        content_type: str | None,
        metadata: dict[str, str] | None,
    ) -> None:
        kwargs: dict = {"Bucket": self._bucket, "Key": key, "Body": data}
        if content_type is not None:
            kwargs["ContentType"] = content_type
        if metadata:
            kwargs["Metadata"] = metadata
        self._client.put_object(**kwargs)


class LocalStorage:
    """Working storage on the local filesystem, used for a job's in-flight media.

    The pipeline's intermediate artifacts (the downloaded source, the
    transcript, the cut audio) are processed by real tools — ffmpeg, the
    transcriber — that read and write files on disk, not objects in a bucket. So
    this implements the slice of the ``Storage`` surface the pipeline relies on
    for resume-tracking (``head`` reports whether a stage already produced its
    output) and adds three filesystem-specific affordances:

      * ``path`` — the real location to hand a subprocess;
      * ``open_write`` — a streaming, atomic write for large downloads;
      * ``cleanup`` — drop one episode's files once its audio is uploaded.

    Writes land atomically: data is written to a ``.part`` sibling and renamed
    into place only once complete, so an interrupted write (a dropped download,
    a crash) never leaves a truncated file that ``head`` would mistake for a
    finished stage. Keys are interpreted as paths beneath ``root``.
    """

    def __init__(self, root: Path | str) -> None:
        self._root = Path(root)

    def path(self, key: str) -> Path:
        """The real filesystem path for ``key`` (whether or not it exists yet)."""
        return self._root / key

    async def head(self, key: str) -> dict[str, str] | None:
        # No metadata to carry locally; an empty dict just signals "present".
        return {} if self.path(key).is_file() else None

    async def get_bytes(self, key: str) -> bytes | None:
        return await run_in_threadpool(self._get_bytes, key)

    def _get_bytes(self, key: str) -> bytes | None:
        path = self.path(key)
        return path.read_bytes() if path.is_file() else None

    async def put(
        self,
        key: str,
        body: BinaryIO,
        *,
        cache_control: str | None = None,
        content_type: str | None = None,
    ) -> None:
        await run_in_threadpool(self._put, key, body)

    def _put(self, key: str, body: BinaryIO) -> None:
        body.seek(0)
        with self._atomic(key) as out:
            shutil.copyfileobj(body, out)

    async def put_bytes(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> None:
        await run_in_threadpool(self._put_bytes, key, data)

    def _put_bytes(self, key: str, data: bytes) -> None:
        with self._atomic(key) as out:
            out.write(data)

    @asynccontextmanager
    async def open_write(self, key: str) -> AsyncIterator[BinaryIO]:
        """Yield a writable file for ``key``, renamed into place atomically on a
        clean exit and discarded if the caller raises.

        Lets a stage stream a large body to disk chunk by chunk (the download)
        without buffering it whole in memory, while preserving the all-or-nothing
        guarantee: a failure part-way through leaves no ``key``, so the resume
        check re-runs the stage rather than reading a truncated file.
        """
        path = self.path(key)
        await run_in_threadpool(path.parent.mkdir, parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".part")
        out = tmp.open("wb")
        try:
            yield out
        except BaseException:
            out.close()
            tmp.unlink(missing_ok=True)
            raise
        else:
            out.close()
            await run_in_threadpool(os.replace, tmp, path)

    async def delete(self, key: str) -> None:
        await run_in_threadpool(self._delete, key)

    def _delete(self, key: str) -> None:
        self.path(key).unlink(missing_ok=True)

    async def list_keys(self, prefix: str) -> set[str]:
        return await run_in_threadpool(self._list_keys, prefix)

    def _list_keys(self, prefix: str) -> set[str]:
        keys: set[str] = set()
        for path in self._root.rglob("*"):
            if not path.is_file():
                continue
            key = path.relative_to(self._root).as_posix()
            if key.startswith(prefix):
                keys.add(key)
        return keys

    async def cleanup(self, prefix: str) -> None:
        """Remove every artifact whose key starts with ``prefix`` — i.e. all of
        one episode's working files once it is done."""
        await run_in_threadpool(self._cleanup, prefix)

    def _cleanup(self, prefix: str) -> None:
        target = self._root / prefix
        parent = target.parent
        if not parent.is_dir():
            return
        for child in parent.iterdir():
            if child.is_file() and child.name.startswith(target.name):
                child.unlink(missing_ok=True)
        # If that was the feed's last episode, drop the now-empty feed dir too.
        try:
            parent.rmdir()
        except OSError:
            pass

    @contextmanager
    def _atomic(self, key: str) -> Iterator[BinaryIO]:
        """Sync helper: write to a ``.part`` sibling, rename into place on success."""
        path = self.path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".part")
        out = tmp.open("wb")
        try:
            yield out
            out.close()
            os.replace(tmp, path)
        except BaseException:
            out.close()
            tmp.unlink(missing_ok=True)
            raise
