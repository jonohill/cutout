from __future__ import annotations

import logging

_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"


def setup_logging(level: str | int = "INFO") -> None:
    logging.basicConfig(level=level, format=_FORMAT, datefmt=_DATEFMT, force=True)
