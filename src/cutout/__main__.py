"""Console entrypoint: ``cutout`` (or ``python -m cutout``)."""

from __future__ import annotations

import uvicorn

from .config import get_settings
from .log import setup_logging


def run() -> None:
    settings = get_settings()
    setup_logging(settings.log_level)
    uvicorn.run(
        "cutout.runtime:create_full_app",
        factory=True,
        host=settings.host,
        port=settings.port,
    )


if __name__ == "__main__":
    run()
