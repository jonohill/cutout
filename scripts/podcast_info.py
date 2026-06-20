#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["boto3"]
# ///
"""List podcast info from the cutout R2 bucket.

Required env vars (R2 S3-compatible API):
  R2_ACCOUNT_ID
  R2_ACCESS_KEY_ID
  R2_SECRET_ACCESS_KEY

Optional:
  R2_BUCKET    (default: cutout)
  WORKER_URL   (default: https://example.com)
"""

from __future__ import annotations

import os
import sys
import xml.etree.ElementTree as ET
from collections import defaultdict
from email.header import decode_header

import boto3

BUCKET = os.environ.get("R2_BUCKET", "cutout")
WORKER_URL = os.environ.get("WORKER_URL", "https://example.com").rstrip("/")


def decode_meta(value: str) -> str:
    """R2 returns customMetadata as RFC 2047 encoded-words; decode back to text."""
    if "=?" not in value:
        return value
    try:
        parts = decode_header(value)
    except Exception:
        return value
    out: list[str] = []
    for chunk, charset in parts:
        if isinstance(chunk, bytes):
            out.append(chunk.decode(charset or "utf-8", errors="replace"))
        else:
            out.append(chunk)
    return "".join(out)


def extract_podcast_name(body: bytes) -> str:
    """Pull the channel/title from an RSS or Atom feed."""
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return "?"
    # RSS: <rss><channel><title>...
    title = root.findtext("./channel/title")
    if title:
        return title.strip()
    # Atom: <feed xmlns="..."><title>...
    for child in root:
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "title" and child.text:
            return child.text.strip()
    return "?"


def human_bytes(n: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PiB"


def main() -> int:
    account_id = os.environ.get("R2_ACCOUNT_ID")
    access_key = os.environ.get("R2_ACCESS_KEY_ID")
    secret_key = os.environ.get("R2_SECRET_ACCESS_KEY")
    if not (account_id and access_key and secret_key):
        print(
            "missing R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, or R2_SECRET_ACCESS_KEY",
            file=sys.stderr,
        )
        return 1

    s3 = boto3.client(
        "s3",
        endpoint_url=f"https://{account_id}.r2.cloudflarestorage.com",
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name="auto",
    )

    sizes: dict[str, int] = defaultdict(int)
    episodes: dict[str, int] = defaultdict(int)
    feed_keys: dict[str, str] = {}

    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=BUCKET):
        for obj in page.get("Contents", []):
            key: str = obj["Key"]
            if "/" not in key:
                continue
            feed_id, _, name = key.partition("/")
            sizes[feed_id] += obj["Size"]
            if name == "feed.xml":
                feed_keys[feed_id] = key
            else:
                episodes[feed_id] += 1

    rows: list[tuple[str, str, str, str, int, int]] = []
    for feed_id, feed_key in sorted(feed_keys.items()):
        obj = s3.get_object(Bucket=BUCKET, Key=feed_key)
        metadata = {k.lower(): v for k, v in obj.get("Metadata", {}).items()}
        original = decode_meta(metadata.get("feedurl", "?"))
        name = extract_podcast_name(obj["Body"].read())
        ours = f"{WORKER_URL}/podcast/{feed_id}"
        rows.append((feed_id, name, ours, original, episodes[feed_id], sizes[feed_id]))

    for feed_id, name, ours, original, n_eps, size in rows:
        print(f"feed_id:   {feed_id}")
        print(f"name:      {name}")
        print(f"our feed:  {ours}")
        print(f"original:  {original}")
        print(f"episodes:  {n_eps}")
        print(f"storage:   {human_bytes(size)}")
        print()

    orphans = sorted(set(sizes) - set(feed_keys))
    if orphans:
        print(
            f"warning: {len(orphans)} prefix(es) without feed.xml: {orphans}",
            file=sys.stderr,
        )

    print(
        f"total: {len(rows)} feeds, "
        f"{sum(episodes.values())} episodes, "
        f"{human_bytes(sum(sizes.values()))}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
