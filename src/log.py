"""
结构化运行日志（JSON Lines）。

每次 LLM 调用 / SQL 执行 / 端到端问答都追加一行到 logs/runs.jsonl，
后续 Day 3 起做 RAG / Agent 时新事件类型直接复用 log_event(event, payload)。

设计要点：
  - 一个进程一个 session_id，方便把同一次 demo 跑出来的事件串起来
  - 字段统一：ts / session_id / event / payload，便于后面 pandas / DuckDB 二次分析
  - LLM messages 只截前 200 字符，避免日志文件爆掉（system prompt 上千 token）
  - 写入用 with open(... "a")，POSIX/NTFS 对小 append 都是原子的，无需自己加锁
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = ROOT / "logs"
LOG_FILE = LOG_DIR / "runs.jsonl"

# 进程级 session_id，import 一次定一次
SESSION_ID = os.environ.get("SAP_SESSION_ID") or uuid.uuid4().hex[:12]


def _preview(text: str | None, limit: int = 200) -> str:
    if not text:
        return ""
    text = text.replace("\n", " ").strip()
    return text if len(text) <= limit else text[:limit] + "..."


def _messages_preview(messages: list[dict]) -> list[dict]:
    """LLM messages 缩略：保留 role，content 截断。"""
    return [
        {"role": m.get("role", ""), "content": _preview(m.get("content", ""))}
        for m in messages
    ]


def log_event(event: str, payload: dict[str, Any]) -> None:
    """通用事件写入。其他业务事件（rag_recall / agent_step 等）后续直接调这个。"""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    record = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "session_id": SESSION_ID,
        "event": event,
        "payload": payload,
    }
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def log_llm_call(
    *,
    model: str,
    messages: list[dict],
    content: str,
    usage: dict,
    latency_ms: int,
    stream: bool = False,
    tag: str = "",
) -> None:
    """LLM 调用专用包装。tag 用来区分 sql_gen / explain / eval 等用途。"""
    log_event(
        "llm_call",
        {
            "tag": tag,
            "model": model,
            "stream": stream,
            "messages": _messages_preview(messages),
            "content_preview": _preview(content, 400),
            "usage": usage,
            "latency_ms": latency_ms,
        },
    )


def log_sql_exec(*, sql: str, ok: bool, row_count: int = 0, error: str = "") -> None:
    log_event(
        "sql_exec",
        {
            "sql": _preview(sql, 500),
            "ok": ok,
            "row_count": row_count,
            "error": _preview(error, 300),
        },
    )


def log_ask(*, question: str, success: bool, total_latency_ms: int, tag: str = "") -> None:
    """端到端一次问答的总结事件。"""
    log_event(
        "ask",
        {
            "tag": tag,
            "question": _preview(question, 300),
            "success": success,
            "total_latency_ms": total_latency_ms,
        },
    )
