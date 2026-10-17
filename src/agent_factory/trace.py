"""Run tracer: an append-only, thread-safe event stream kept outside graph state.

Because events are pushed to the sink as they happen, a failed run still leaves
a complete record of what ran, what it produced and why it stopped.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

from .contracts import TraceEvent

PREVIEW_CHARS = 2000


def preview(value: Any, limit: int = PREVIEW_CHARS) -> Any:
    """Bound the size of values stored in the trace."""
    if isinstance(value, str):
        return value if len(value) <= limit else value[:limit] + "…"
    if isinstance(value, dict):
        return {k: preview(v, limit) for k, v in value.items()}
    if isinstance(value, list):
        return [preview(v, limit) for v in value[:50]]
    return value


class Tracer:
    def __init__(self, run_id: str, sink: Callable[[TraceEvent], None] | None = None) -> None:
        self.run_id = run_id
        self.events: list[TraceEvent] = []
        self._sink = sink
        self._lock = threading.Lock()

    def emit(self, type: str, node: str | None = None, **data: Any) -> TraceEvent:
        with self._lock:
            event = TraceEvent(run_id=self.run_id, seq=len(self.events), type=type, node=node,
                               data=preview(data))
            self.events.append(event)
        if self._sink:
            self._sink(event)
        return event

    def of_type(self, type: str) -> list[TraceEvent]:
        return [e for e in self.events if e.type == type]

    def executed_nodes(self) -> list[str]:
        return [e.node for e in self.events if e.type == "node_started" and e.node]
