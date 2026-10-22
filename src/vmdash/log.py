"""Logging setup shared by the collector and the web process."""

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def setup_logging(level: str, file: Path | None) -> None:
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
        h.close()

    formatter = logging.Formatter(FORMAT)
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if file:
        file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(RotatingFileHandler(file, maxBytes=10_000_000, backupCount=5, encoding="utf-8"))
    for h in handlers:
        h.setFormatter(formatter)
        root.addHandler(h)
    root.setLevel(level.upper())

    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.handlers.clear()
        lg.propagate = True
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
