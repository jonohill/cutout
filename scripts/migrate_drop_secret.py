#!/usr/bin/env python
"""One-shot migration: re-key episode audio from HMAC(secret) IDs to unkeyed IDs.

Episode IDs used to be ``HMAC-SHA256(secret, guid)`` and are used as the audio
object key ``{feed_id}/{episode_id}`` (see ``cutout.common.paths.audio_path``).
Removing the feed-ID secret switches that derivation to a plain SHA-256 of the
GUID (the current ``cutout.common.get_feed_id``), so every existing audio object
now hashes to a different key and the reconciler would re-download it. This
script renames each stored audio object to its new, secret-free key so nothing
has to be re-fetched.

It computes the OLD key itself from the secret you pass — the secret no longer
lives in the app config. Provide it with ``--secret`` or the ``FEED_ID_SECRET``
environment variable.

For every stored ``{feed_id}/feed.xml`` it parses the episodes (the stored feed
contains exactly the episodes whose audio is present), computes old and new IDs
from each GUID, moves the audio object, and rewrites the feed's enclosure URLs so
the feed is consistent immediately without waiting for a refresh.

The service should be offline while this runs — it moves objects in place.

Usage (from the ``cutout`` directory):

    uv run python scripts/migrate_drop_secret.py --secret OLD            # dry run
    uv run python scripts/migrate_drop_secret.py --secret OLD --apply    # execute

``--secret`` may be omitted if ``FEED_ID_SECRET`` is set in the environment.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import hmac
import os
import sys
from pathlib import Path

# Make the package importable when run straight from a checkout (mirrors the
# pythonpath pytest uses) without relying on an installed distribution.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from cutout.common import audio_path, feed_path, get_feed_id  # noqa: E402
from cutout.common.feed_id import _FEED_ID_BYTES  # noqa: E402
from cutout.common.storage import S3Storage  # noqa: E402
from cutout.config import Settings  # noqa: E402
from cutout.worker import feed_xml  # noqa: E402

_FEED_SUFFIX = "/feed.xml"


def _old_episode_id(channel: str, secret: str) -> str:
    """The pre-migration ID: HMAC-SHA256(secret, channel), as the app once derived it.

    Kept in lock-step with the old ``get_feed_id`` keyed branch (now deleted):
    first ``_FEED_ID_BYTES`` of the digest, URL-safe base64, padding stripped.
    """
    digest = hmac.new(
        secret.encode("utf-8"), channel.encode("utf-8"), hashlib.sha256
    ).digest()
    return (
        base64.urlsafe_b64encode(digest[:_FEED_ID_BYTES]).rstrip(b"=").decode("ascii")
    )


def _feed_ids(keys: set[str]) -> list[str]:
    """Feed IDs for every stored ``{feed_id}/feed.xml`` document."""
    return sorted(k[: -len(_FEED_SUFFIX)] for k in keys if k.endswith(_FEED_SUFFIX))


async def _move(storage: S3Storage, bucket: str, src: str, dst: str) -> None:
    """Server-side copy ``src`` -> ``dst`` then delete ``src``.

    Uses the underlying boto3 client directly: the ``Storage`` surface has no
    copy/delete, and a server-side copy avoids pulling each (large) audio object
    through this process. ``MetadataDirective`` defaults to COPY, so content type
    and user metadata carry over.
    """
    client = storage._client  # noqa: SLF001 — one-off script, reuse the configured client
    await asyncio.to_thread(
        client.copy_object,
        Bucket=bucket,
        Key=dst,
        CopySource={"Bucket": bucket, "Key": src},
    )
    await asyncio.to_thread(client.delete_object, Bucket=bucket, Key=src)


async def _migrate_feed(
    storage: S3Storage,
    bucket: str,
    audio_base: str,
    feed_id: str,
    old_secret: str,
    *,
    apply: bool,
) -> tuple[int, int]:
    """Migrate one feed. Returns (moved, skipped)."""
    raw = await storage.get_bytes(feed_path(feed_id))
    if raw is None:
        print(f"  ! {feed_id}: feed.xml vanished, skipping")
        return 0, 0

    feed = feed_xml.parse_feed(raw.decode("utf-8"))
    episodes = feed_xml.parse_episodes(feed)
    present = await storage.list_keys(f"{feed_id}/")

    moved = skipped = 0
    changed = False
    for ep in episodes:
        old_id = _old_episode_id(ep.guid, old_secret)
        new_id = get_feed_id(ep.guid)
        old_key = audio_path(feed_id, old_id)
        new_key = audio_path(feed_id, new_id)

        if old_id == new_id:  # secret was effectively a no-op for this id
            continue
        if new_key in present:
            print(f"    = {ep.guid}: already at {new_key}")
            skipped += 1
            feed_xml.set_audio_url(ep, f"{audio_base}/{new_key}")
            changed = True
            continue
        if old_key not in present:
            print(f"    ? {ep.guid}: no audio at {old_key} (not yet processed)")
            skipped += 1
            continue

        print(f"    {'->' if apply else '~ '} {old_key}  ->  {new_key}")
        if apply:
            await _move(storage, bucket, old_key, new_key)
        feed_xml.set_audio_url(ep, f"{audio_base}/{new_key}")
        moved += 1
        changed = True

    if changed and apply:
        metadata = await storage.head(feed_path(feed_id)) or {}
        await storage.put_bytes(
            feed_path(feed_id),
            feed.serialize().encode("utf-8"),
            content_type="application/xml",
            metadata=metadata or None,
        )

    return moved, skipped


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--secret",
        default=os.environ.get("FEED_ID_SECRET"),
        help="the OLD feed-ID secret (defaults to $FEED_ID_SECRET)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="perform the moves (default: dry run, only print the plan)",
    )
    args = parser.parse_args()

    if not args.secret:
        parser.error("no secret given — pass --secret or set FEED_ID_SECRET")

    settings = Settings()  # type: ignore[call-arg]
    storage = S3Storage(settings)
    bucket = settings.s3_bucket
    audio_base = settings.public_storage_url.rstrip("/")

    mode = "APPLY" if args.apply else "DRY RUN (pass --apply to execute)"
    print(f"== drop-secret migration [{mode}] bucket={bucket} ==")

    feed_ids = _feed_ids(await storage.list_keys(""))
    print(f"found {len(feed_ids)} feed(s)\n")

    total_moved = total_skipped = 0
    for feed_id in feed_ids:
        print(f"feed {feed_id}")
        moved, skipped = await _migrate_feed(
            storage, bucket, audio_base, feed_id, args.secret, apply=args.apply
        )
        total_moved += moved
        total_skipped += skipped

    verb = "moved" if args.apply else "to move"
    print(f"\n== done: {total_moved} {verb}, {total_skipped} skipped ==")
    if not args.apply and total_moved:
        print("re-run with --apply to perform the moves.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
