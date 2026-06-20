"""The queue worker: feed reconciliation and the media pipeline.

Separated from the HTTP server (``cutout.app``) by the queue — the HTTP side
produces feed messages, this side consumes them. ``FeedProcessor`` reconciles a
feed and hands episodes needing audio to the ``Pipeline``, which runs them
through the download -> transcribe -> chapters -> encode -> upload stages.
"""

from .media import MediaWorker
from .pipeline import Pipeline, Stage, build_media_pipeline
from .processor import FeedProcessor

__all__ = [
    "FeedProcessor",
    "MediaWorker",
    "Pipeline",
    "Stage",
    "build_media_pipeline",
]
