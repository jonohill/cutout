"""Feed/episode ID generation."""

from __future__ import annotations

import base64
import hashlib
import secrets

_FEED_ID_BYTES = 10


def get_feed_id(channel: str) -> str:
    """Return a deterministic, URL-safe ID derived from ``channel``.

    The ID is an unkeyed SHA-256 hash, so the same channel value always maps to
    the same identifier.
    """
    digest = hashlib.sha256(channel.encode("utf-8")).digest()
    return (
        base64.urlsafe_b64encode(digest[:_FEED_ID_BYTES]).rstrip(b"=").decode("ascii")
    )


def new_feed_id() -> str:
    """Return a fresh random feed ID in the same format as ``get_feed_id``."""
    return secrets.token_urlsafe(_FEED_ID_BYTES)
