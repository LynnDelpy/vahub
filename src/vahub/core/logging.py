"""Logging setup.

Structured logging by default (`log_format: json`), because the interesting
questions about a hub are asked after the fact: which principal called which
tool, and what did the gate decide. A console renderer is available for
development, where a human is reading the stream live.

Everything goes to stdout: the hub is expected to run under systemd or a
container runtime, and both capture stdout.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

import structlog

# Libraries that log per request at INFO and would drown the hub's own events.
_NOISY = ("httpx", "httpcore", "apscheduler.executors.default", "asyncio")


def configure(level: str = "INFO", log_format: str = "json") -> None:
    lvl = getattr(logging, str(level).upper(), logging.INFO)
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=lvl, force=True)
    for name in _NOISY:
        logging.getLogger(name).setLevel(max(lvl, logging.WARNING))

    renderer: Any
    if log_format == "console":
        renderer = structlog.dev.ConsoleRenderer(colors=sys.stdout.isatty())
    else:
        renderer = structlog.processors.JSONRenderer()

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(lvl),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> Any:
    """A bound logger. Typed loosely on purpose: structlog's concrete bound
    logger class depends on the configured wrapper_class."""
    return structlog.get_logger(name)
