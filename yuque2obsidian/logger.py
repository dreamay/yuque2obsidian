"""Logging utilities."""

import logging
import sys
from typing import Optional


class GradioLogHandler(logging.Handler):
    """A log handler that stores the latest N records for Gradio UI."""

    def __init__(self, capacity: int = 200) -> None:
        super().__init__()
        self.capacity = capacity
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)
        if len(self.records) > self.capacity:
            self.records.pop(0)

    def get_text(self) -> str:
        return "\n".join(self.format(r) for r in self.records)


def setup_logging(level: int = logging.INFO, gradio_handler: Optional[GradioLogHandler] = None) -> logging.Logger:
    """Configure root logger for the package."""
    logger = logging.getLogger("yuque2obsidian")
    logger.setLevel(level)
    logger.handlers.clear()

    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(level)
    console.setFormatter(formatter)
    logger.addHandler(console)

    if gradio_handler is not None:
        gradio_handler.setLevel(level)
        gradio_handler.setFormatter(formatter)
        logger.addHandler(gradio_handler)

    return logger
