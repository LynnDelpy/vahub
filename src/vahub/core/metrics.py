"""Prometheus metrics.

Latency is recorded per stage rather than per turn. A single "turn took 4
seconds" number cannot tell you whether the time went into speech recognition,
the model, or a module that is waiting on a slow backend, and those three have
nothing in common as fixes.

The other three exist to answer questions that come up during an incident: what
did the gate decide, which module is in which state, and is the event bus losing
messages because a consumer cannot keep up.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager

from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest

# --- pipeline stages -------------------------------------------------------
# Named stages, so a dashboard does not depend on whatever string a caller
# happened to pass.
STAGE_STT = "stt"
STAGE_LLM = "llm"
STAGE_TOOL = "tool"
STAGE_TTS = "tts"
STAGE_TURN = "turn"

# Buckets go down to 50ms (a local module call) and up to a minute (a model
# request that is about to hit its timeout).
_LATENCY_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 4.0, 8.0, 15.0, 30.0, 60.0)

STAGE_LATENCY = Histogram(
    "vahub_stage_latency_seconds",
    "Latency of one pipeline stage",
    ["stage"],
    buckets=_LATENCY_BUCKETS,
)


def observe_stage(stage: str, seconds: float) -> None:
    STAGE_LATENCY.labels(stage=stage).observe(seconds)


@contextmanager
def stage_timer(stage: str) -> Iterator[None]:
    t0 = time.monotonic()
    try:
        yield
    finally:
        STAGE_LATENCY.labels(stage=stage).observe(time.monotonic() - t0)


# --- policy ----------------------------------------------------------------
POLICY_DECISIONS = Counter(
    "vahub_policy_decisions_total",
    "Gate decisions by principal and tool",
    ["principal", "module", "tool", "outcome"],
)

# --- modules ---------------------------------------------------------------
MODULE_STATES = ("unconfigured", "starting", "ready", "degraded", "failed", "stopped")

MODULE_STATE = Gauge(
    "vahub_module_state",
    "1 for the module's current state, 0 otherwise",
    ["module", "state"],
)
MODULE_RESTARTS = Counter(
    "vahub_module_restarts_total",
    "Module restart attempts",
    ["module"],
)


def set_module_state(module: str, state: str) -> None:
    # Every state is written, not just the current one: a gauge left at 1 for an
    # old state would make a stale series look live.
    for candidate in MODULE_STATES:
        MODULE_STATE.labels(module=module, state=candidate).set(1.0 if candidate == state else 0.0)


# --- tool calls ------------------------------------------------------------
TOOL_CALLS = Counter(
    "vahub_tool_calls_total",
    "Tool calls by outcome",
    ["module", "tool", "result"],
)
TOOL_LATENCY = Histogram(
    "vahub_tool_latency_seconds",
    "Tool call latency, measured around the dispatch",
    ["module", "tool"],
    buckets=_LATENCY_BUCKETS,
)

# --- bus -------------------------------------------------------------------
BUS_DROPPED = Counter(
    "vahub_bus_dropped_total",
    "Bus messages dropped, or slow subscribers disconnected, per topic",
    ["topic"],
)


def render() -> tuple[bytes, str]:
    """Body and content type for the /metrics endpoint."""
    return generate_latest(), CONTENT_TYPE_LATEST
