"""全链路 Trace：记录 query -> plan -> LLM -> 工具调用 -> HITL -> 结果，可落盘可回放."""

from __future__ import annotations

import json
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="milliseconds")


def _short_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


@dataclass
class TraceSpan:
    span_id: str
    parent_id: str | None
    kind: str
    name: str
    start_ms: float
    duration_ms: float = 0.0
    status: str = "ok"
    attrs: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "span_id": self.span_id,
            "parent_id": self.parent_id,
            "kind": self.kind,
            "name": self.name,
            "start_ms": round(self.start_ms, 3),
            "duration_ms": round(self.duration_ms, 3),
            "status": self.status,
            "attrs": self.attrs,
        }


class TraceRecorder:
    """一次分析任务的 trace 收集器。所有 span 都挂在同一个 trace_id 下."""

    def __init__(self, log_dir: Path, query: str = "", trace_id: str | None = None):
        self.log_dir = Path(log_dir)
        self.trace_id = trace_id or _short_id("trace")
        self.query = query
        self.started_at = _now_iso()
        self._started_perf = time.perf_counter()
        self.spans: list[TraceSpan] = []
        self.events: list[dict] = []
        self.result: dict = {}
        self.status = "running"
        self._stack: list[str] = []
        self._finished = False

    # ---------- span / event ----------

    @contextmanager
    def span(self, kind: str, name: str, **attrs) -> Iterator[dict]:
        """上下文管理器；yield 的 dict 会作为 span.attrs 落盘，可中途补充."""
        span = TraceSpan(
            span_id=_short_id("span"),
            parent_id=self._stack[-1] if self._stack else None,
            kind=kind,
            name=name,
            start_ms=(time.perf_counter() - self._started_perf) * 1000,
            attrs=dict(attrs),
        )
        self.spans.append(span)
        self._stack.append(span.span_id)
        started = time.perf_counter()
        try:
            yield span.attrs
        except BaseException:
            span.status = "error"
            raise
        finally:
            span.duration_ms = (time.perf_counter() - started) * 1000
            if self._stack and self._stack[-1] == span.span_id:
                self._stack.pop()

    def event(self, kind: str, **attrs) -> None:
        self.events.append(
            {
                "ts": _now_iso(),
                "kind": kind,
                "offset_ms": round((time.perf_counter() - self._started_perf) * 1000, 3),
                "attrs": attrs,
            }
        )

    # ---------- finish / serialize ----------

    def finish(self, status: str = "ok", **result) -> dict:
        self.status = status
        self.result = result
        if not self._finished:
            self.events.append(
                {
                    "ts": _now_iso(),
                    "kind": "trace_finish",
                    "offset_ms": round((time.perf_counter() - self._started_perf) * 1000, 3),
                    "attrs": {"status": status},
                }
            )
            self._finished = True
        return self.to_dict()

    def to_dict(self) -> dict:
        return {
            "trace_id": self.trace_id,
            "query": self.query,
            "status": self.status,
            "started_at": self.started_at,
            "ended_at": _now_iso(),
            "duration_ms": round((time.perf_counter() - self._started_perf) * 1000, 3),
            "spans": [span.to_dict() for span in self.spans],
            "events": list(self.events),
            "result": self.result,
        }

    def save(self) -> Path:
        trace_dir = self.log_dir / "traces"
        trace_dir.mkdir(parents=True, exist_ok=True)
        path = trace_dir / f"{self.trace_id}.json"
        path.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        return path


# ---------- 读取 / 回放 ----------


def load_trace(trace_id: str, log_dir: Path) -> dict:
    path = Path(log_dir) / "traces" / f"{trace_id}.json"
    if not path.exists():
        raise FileNotFoundError(f"trace 不存在: {trace_id}")
    return json.loads(path.read_text(encoding="utf-8"))


def list_traces(log_dir: Path, limit: int = 20) -> list[dict]:
    trace_dir = Path(log_dir) / "traces"
    if not trace_dir.exists():
        return []
    files = sorted(trace_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    items: list[dict] = []
    for path in files[:limit]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        items.append(
            {
                "trace_id": data.get("trace_id", path.stem),
                "query": data.get("query", ""),
                "status": data.get("status", "unknown"),
                "duration_ms": data.get("duration_ms", 0),
                "started_at": data.get("started_at", ""),
                "path": str(path),
            }
        )
    return items


def replay_trace(trace_id: str, log_dir: Path) -> str:
    """把 trace 渲染成可读的时间线文本，用于复盘/面试演示."""
    data = load_trace(trace_id, log_dir)
    lines: list[str] = []
    lines.append(f"trace_id : {data.get('trace_id')}")
    lines.append(f"query    : {data.get('query')}")
    lines.append(f"status   : {data.get('status')}  duration: {data.get('duration_ms', 0):.1f} ms")
    lines.append("-" * 72)

    for event in data.get("events", []):
        offset = event.get("offset_ms", 0)
        attrs = json.dumps(event.get("attrs", {}), ensure_ascii=False)
        lines.append(f"[{offset:8.1f} ms] event  {event.get('kind')}  {attrs}")

    lines.append("-" * 72)
    for span in sorted(data.get("spans", []), key=lambda item: item.get("start_ms", 0)):
        indent = "  " if span.get("parent_id") else ""
        detail = ""
        if span.get("kind") == "tool":
            detail = json.dumps(span.get("attrs", {}), ensure_ascii=False)
            if len(detail) > 180:
                detail = detail[:180] + "..."
        lines.append(
            f"[{span.get('start_ms', 0):8.1f} ms] {indent}{span.get('kind'):5s} "
            f"{span.get('name')}  {span.get('duration_ms', 0):.1f}ms  {span.get('status')}  {detail}"
        )

    result = data.get("result", {})
    if result:
        lines.append("-" * 72)
        lines.append("answer:")
        lines.append(str(result.get("answer", "")))
        artifacts = result.get("artifacts") or []
        if artifacts:
            lines.append("artifacts: " + ", ".join(str(item) for item in artifacts))
    return "\n".join(lines)
