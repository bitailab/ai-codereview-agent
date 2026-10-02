"""记录每次模型调用的完整请求与回复，供状态页查看。

挂在 ChatOpenAI 的 callbacks 上，所以文本审查、结构化输出、工具调用、复核等所有调用都会被记录。
任务归属取自 LangGraph 注入的 thread_id（job-N），可读的步骤说明由节点通过 step() 设置。
单独存放在 data/llm_trace.db（压缩），按天数清理，不影响 state.db。
"""
from __future__ import annotations

import contextvars
import json
import logging
import sqlite3
import threading
import time
import zlib
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import BaseMessage

log = logging.getLogger(__name__)

_step: contextvars.ContextVar[str] = contextvars.ContextVar("huatuo_trace_step", default="")

SCHEMA = """
CREATE TABLE IF NOT EXISTS calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_id TEXT,
    node TEXT,
    step TEXT,
    model TEXT,
    started_at TEXT NOT NULL,
    secs REAL,
    prompt_tokens INTEGER,
    completion_tokens INTEGER,
    request BLOB,
    response BLOB,
    error TEXT
);
CREATE INDEX IF NOT EXISTS calls_thread ON calls(thread_id, id);
"""


def set_step(label: str) -> None:
    """同 step()，但不自动恢复：LangGraph 每个节点任务在独立复制的 context 中运行，不会串到别的节点。"""
    _step.set(label)


@contextmanager
def step(label: str):
    """标记当前正在做什么（例如“审查 a.go [correctness]”），期间的模型调用都带上这个说明。"""
    token = _step.set(label)
    try:
        yield
    finally:
        _step.reset(token)


def _pack(obj: Any) -> bytes:
    return zlib.compress(json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8"))


def _unpack(blob: bytes | None) -> Any:
    return json.loads(zlib.decompress(blob).decode("utf-8")) if blob else None


def _message(m: BaseMessage) -> dict:
    d: dict[str, Any] = {"role": m.type, "content": m.content}
    if getattr(m, "tool_calls", None):
        d["tool_calls"] = m.tool_calls
    if getattr(m, "tool_call_id", None):
        d["tool_call_id"] = m.tool_call_id
    return d


class TraceStore:
    def __init__(self, path: Path, keep_days: int = 7):
        self.path = path
        self.keep_days = keep_days
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.execute("PRAGMA journal_mode=WAL")  # 状态页同时在读
        self._conn.executescript(SCHEMA)
        self._last_prune = 0.0

    def add(self, row: dict) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO calls (thread_id, node, step, model, started_at, secs, prompt_tokens, completion_tokens,"
                " request, response, error) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (row["thread_id"], row["node"], row["step"], row["model"], row["started_at"], row["secs"],
                 row.get("prompt_tokens"), row.get("completion_tokens"), _pack(row["request"]),
                 _pack(row["response"]) if row.get("response") is not None else None, row.get("error")),
            )
            if time.time() - self._last_prune > 3600:
                self._last_prune = time.time()
                self._conn.execute("DELETE FROM calls WHERE started_at < datetime('now', ?)", (f"-{self.keep_days} days",))


class TraceHandler(BaseCallbackHandler):
    """同步调用时回调在调用方线程内执行，所以能读到节点设置的 step。"""

    def __init__(self, store: TraceStore):
        self.store = store
        self._pending: dict[UUID, dict] = {}

    def on_chat_model_start(self, serialized: dict, messages: list[list[BaseMessage]], *, run_id: UUID,
                            metadata: dict | None = None, **kwargs: Any) -> None:
        md = metadata or {}
        params = kwargs.get("invocation_params") or {}
        request = {
            "params": {k: params[k] for k in ("model", "temperature", "max_tokens", "max_completion_tokens") if k in params},
            "tools": [t.get("function", {}).get("name") for t in params.get("tools") or []] or None,
            "response_format": bool(params.get("response_format")),
            "messages": [_message(m) for m in (messages[0] if messages else [])],
        }
        self._pending[run_id] = {
            "thread_id": md.get("thread_id"), "node": md.get("langgraph_node"), "step": _step.get(),
            "model": params.get("model") or params.get("model_name"), "request": request,
            "started_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"), "t0": time.monotonic(),
        }

    def _finish(self, run_id: UUID, **extra: Any) -> None:
        row = self._pending.pop(run_id, None)
        if row is None:
            return
        row["secs"] = round(time.monotonic() - row.pop("t0"), 1)
        try:
            self.store.add(row | extra)
        except Exception as e:  # noqa: BLE001 记录失败不能影响审查
            log.warning("记录模型调用失败: %s", e)

    def on_llm_end(self, response: Any, *, run_id: UUID, **kwargs: Any) -> None:
        gen = response.generations[0][0] if response.generations and response.generations[0] else None
        msg = getattr(gen, "message", None)
        usage = (response.llm_output or {}).get("token_usage") or {}
        out: dict[str, Any] = {"content": msg.content if msg is not None else getattr(gen, "text", "")}
        if msg is not None:
            if getattr(msg, "tool_calls", None):
                out["tool_calls"] = msg.tool_calls
            if reasoning := (msg.additional_kwargs or {}).get("reasoning_content"):
                out["reasoning"] = reasoning
        self._finish(run_id, response=out, prompt_tokens=usage.get("prompt_tokens"),
                     completion_tokens=usage.get("completion_tokens"))

    def on_llm_error(self, error: BaseException, *, run_id: UUID, **kwargs: Any) -> None:
        self._finish(run_id, error=str(error)[:2000])


_handler: TraceHandler | None = None
_handler_lock = threading.Lock()


def handler(data_path: Path, keep_days: int) -> TraceHandler:
    global _handler
    with _handler_lock:
        if _handler is None:
            data_path.mkdir(parents=True, exist_ok=True)
            _handler = TraceHandler(TraceStore(data_path / "llm_trace.db", keep_days))
        return _handler


class TraceReader:
    """状态页用的只读访问。库文件在第一次模型调用时才创建，不存在时返回空。"""

    def __init__(self, path: Path):
        self.path = path

    def _conn(self) -> sqlite3.Connection | None:
        if not self.path.exists():
            return None
        conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        return conn

    def calls(self, thread_id: str, limit: int = 400) -> list[dict]:
        conn = self._conn()
        if conn is None:
            return []
        with conn:
            rows = conn.execute(
                "SELECT id, node, step, model, started_at, secs, prompt_tokens, completion_tokens, error"
                " FROM calls WHERE thread_id=? ORDER BY id DESC LIMIT ?", (thread_id, limit)).fetchall()
        return [dict(r) for r in reversed(rows)]

    def call(self, call_id: int) -> dict | None:
        conn = self._conn()
        if conn is None:
            return None
        with conn:
            r = conn.execute("SELECT * FROM calls WHERE id=?", (call_id,)).fetchone()
        if r is None:
            return None
        return dict(r) | {"request": _unpack(r["request"]), "response": _unpack(r["response"])}
