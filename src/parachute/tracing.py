"""Trace sinks. neatlogs gets the full span tree; a local JSONL copy feeds the
UI and the replay report. Attributes carry hashes, counts and error classes,
never row values or secrets."""

from __future__ import annotations

import contextvars
import json
import os
import threading
import time
import uuid
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any, Iterator, Protocol

_parent: contextvars.ContextVar[str | None] = contextvars.ContextVar("parachute_span", default=None)


def _clean(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


class SpanHandle(Protocol):
    def set(self, **attrs: Any) -> None: ...


class TraceSink(Protocol):
    def span(self, kind: str, name: str, **attrs: Any): ...
    def flush(self) -> None: ...


class _NullHandle:
    def set(self, **attrs: Any) -> None:
        pass


class NullTrace:
    @contextmanager
    def span(self, kind: str, name: str, **attrs: Any) -> Iterator[_NullHandle]:
        yield _NullHandle()

    def flush(self) -> None:
        pass


class _JsonlHandle:
    def __init__(self, record: dict[str, Any]):
        self.record = record

    def set(self, **attrs: Any) -> None:
        self.record["attrs"].update({k: _clean(v) for k, v in attrs.items()})


class JsonlTrace:
    """One JSON line per finished span, under var/traces/<run_id>.jsonl."""

    def __init__(self, directory: str | Path, run_key: str = "run_id"):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.run_key = run_key
        self._lock = threading.Lock()
        self._run_id: str | None = None

    @contextmanager
    def span(self, kind: str, name: str, **attrs: Any) -> Iterator[_JsonlHandle]:
        if attrs.get(self.run_key):
            self._run_id = str(attrs[self.run_key])
        span_id = uuid.uuid4().hex[:16]
        record = {
            "span_id": span_id,
            "parent_id": _parent.get(),
            "kind": kind,
            "name": name,
            "start": time.time(),
            "attrs": {k: _clean(v) for k, v in attrs.items()},
            "status": "ok",
        }
        token = _parent.set(span_id)
        handle = _JsonlHandle(record)
        try:
            yield handle
        except BaseException as exc:
            record["status"] = "error"
            record["attrs"]["exception"] = type(exc).__name__
            raise
        finally:
            _parent.reset(token)
            record["end"] = time.time()
            record["duration_ms"] = int((record["end"] - record["start"]) * 1000)
            self._write(record)

    def _write(self, record: dict[str, Any]) -> None:
        name = self._run_id or "unscoped"
        with self._lock, open(self.directory / f"{name}.jsonl", "a") as fh:
            fh.write(json.dumps(record, default=str) + "\n")

    def flush(self) -> None:
        pass


class _NeatlogsHandle:
    def __init__(self, span: Any, kind: str):
        self.span = span
        self.kind = kind

    def set(self, **attrs: Any) -> None:
        for key, value in attrs.items():
            self.span.set_attribute(f"parachute.{key}", _clean(value))
        if self.kind == "GUARDRAIL" and "passed" in attrs:
            self.span.set_attribute("neatlogs.guardrail.passed", bool(attrs["passed"]))
            if "reason" in attrs:
                self.span.set_attribute("neatlogs.guardrail.output", str(attrs["reason"]))


WORKFLOW_NAME = "postgres-schema-migration"
# Each separately triggered entry point gets its own workflow in neatlogs.
ROOT_WORKFLOWS = {
    "migration_run": WORKFLOW_NAME,
    "migration_resume": WORKFLOW_NAME,
    "contract_approval": "contract-approval",
    "migration_abort": "migration-abort",
}
INGEST_ENDPOINT = "https://ingest.neatlogs.com"
_neatlogs_state = {"ready": False}


def init_neatlogs(workflow_name: str = WORKFLOW_NAME) -> bool:
    """Initialise neatlogs once per process, at the entry point. No key, no export."""
    if _neatlogs_state["ready"]:
        return True
    if not os.environ.get("NEATLOGS_API_KEY"):
        return False
    import neatlogs

    neatlogs.init(
        api_key=os.environ["NEATLOGS_API_KEY"],
        endpoint=INGEST_ENDPOINT,
        workflow_name=workflow_name,
        tags=["neatHack", "fictional-fixture"],
        flush_interval=1.0,
    )
    _neatlogs_state["ready"] = True
    return True


def shutdown_neatlogs() -> None:
    """Flush and stop the exporter. Call once when the process ends."""
    if not _neatlogs_state["ready"]:
        return
    import neatlogs

    neatlogs.flush(timeout_millis=10_000)
    neatlogs.shutdown(timeout_millis=10_000)
    _neatlogs_state["ready"] = False


class NeatlogsTrace:
    """Sends spans to neatlogs. The entry point calls init_neatlogs() first."""

    def __init__(self):
        import neatlogs

        if not _neatlogs_state["ready"] and not init_neatlogs():
            raise RuntimeError("NEATLOGS_API_KEY is not set")
        self.neatlogs = neatlogs
        self.trace_ids: dict[str, list[str]] = {}

    @contextmanager
    def span(self, kind: str, name: str, **attrs: Any) -> Iterator[_NeatlogsHandle]:
        extra: dict[str, Any] = {}
        if kind == "TOOL":
            extra["tool.name"] = name
        if kind == "WORKFLOW" and name in ROOT_WORKFLOWS:
            extra["neatlogs.workflow_name"] = ROOT_WORKFLOWS[name]
        # Every trace of one run (start, crash resume, approval) shares a session.
        session = attrs.get("run_id") if kind == "WORKFLOW" else None
        with self.neatlogs.trace(name, kind=kind, session_id=session, **extra) as sp:
            handle = _NeatlogsHandle(sp, kind)
            handle.set(**attrs)
            if kind == "WORKFLOW" and attrs.get("run_id"):
                tid = format(sp.get_span_context().trace_id, "032x")
                self.trace_ids.setdefault(str(attrs["run_id"]), []).append(tid)
            yield handle

    def flush(self) -> None:
        self.neatlogs.flush()


class _MultiHandle:
    def __init__(self, handles: list[Any]):
        self.handles = handles

    def set(self, **attrs: Any) -> None:
        for h in self.handles:
            h.set(**attrs)


class MultiTrace:
    def __init__(self, *sinks: Any):
        self.sinks = sinks

    @contextmanager
    def span(self, kind: str, name: str, **attrs: Any) -> Iterator[_MultiHandle]:
        with ExitStack() as stack:
            handles = [stack.enter_context(s.span(kind, name, **attrs)) for s in self.sinks]
            yield _MultiHandle(handles)

    def flush(self) -> None:
        for s in self.sinks:
            s.flush()

    def trace_ids(self, run_id: str) -> list[str]:
        for s in self.sinks:
            ids = getattr(s, "trace_ids", None)
            if ids and run_id in ids:
                return list(ids[run_id])
        return []


def default_trace(var_dir: str | Path) -> MultiTrace:
    sinks: list[Any] = [JsonlTrace(Path(var_dir) / "traces")]
    if init_neatlogs():
        sinks.append(NeatlogsTrace())
    return MultiTrace(*sinks)
