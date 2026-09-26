"""Rotating audit/diagnostic log for the review workflow (never logs tokens)."""

import logging
from logging.handlers import RotatingFileHandler
from .config import DATA_DIR


def configure_review_logging():
    logger = logging.getLogger("review")
    if any(getattr(h, "_review_handler", False) for h in logger.handlers):
        return
    directory = DATA_DIR / "logs"
    directory.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        directory / "review.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8"
    )
    handler._review_handler = True
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    )
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
