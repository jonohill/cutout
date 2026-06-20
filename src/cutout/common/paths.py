from __future__ import annotations


def feed_path(feed_id: str) -> str:
    """Storage key for a feed's XML document."""
    return f"{feed_id}/feed.xml"


def audio_path(feed_id: str, episode_id: str) -> str:
    """Storage key for an episode's audio."""
    return f"{feed_id}/{episode_id}"


def work_path(feed_id: str, episode_id: str, name: str) -> str:
    """Storage key for an intermediate media-pipeline artifact.

    Kept as a sibling of the final audio (``{feed_id}/{episode_id}``) so a single
    ``list_keys(f"{feed_id}/")`` surfaces an episode's whole pipeline progress.
    """
    return f"{feed_id}/{episode_id}.{name}"
