"""
Function Calling 工具层（Day 4）。

定义 LLM 可调用的工具集和分发执行器。当前只有一个 tool：

  schema_lookup(table_names: list[str]) -> str
      当 RAG 召回的 top-K 表里缺关键表（典型场景：MAKT 中文描述表被同模块的
      MARA/VBAP 在向量空间盖住），LLM 可以主动调用本工具拉那几张表的完整字段，
      作为对 RAG 召回不足的兜底。

设计要点：
  1. tool schema 用 OpenAI function calling 标准格式（DeepSeek 完全兼容）
  2. 表名白名单校验，避免 LLM 编造不存在的表
  3. execute_tool 是统一入口，按 tool name 分发；后续 Day 6 Agent 加新工具
     时只需要在这里注册即可
  4. 每次 tool 调用都写一行 tool_call 事件到 logs/runs.jsonl，方便复盘
"""
from __future__ import annotations

import json
from typing import Any

from src.log import log_event
from src.schemas import SCHEMAS, get_table, schema_to_prompt_text


ALLOWED_TABLES: set[str] = {t["name"] for t in SCHEMAS}

MAX_TABLES_PER_CALL = 5


SCHEMA_LOOKUP_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "schema_lookup",
        "description": (
            "查看一张或多张 SAP 表的完整字段定义。"
            "当你发现已给出的 schema 不够（例如：查询需要 JOIN 一张未在召回列表里的表，"
            "或某个字段的含义/类型不明），调用本工具补充。"
            "一次最多查 5 张表，传入大写表名。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "table_names": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "想查看的表名列表，大写，例如 [\"MAKT\", \"MARA\"]",
                    "minItems": 1,
                    "maxItems": MAX_TABLES_PER_CALL,
                },
            },
            "required": ["table_names"],
        },
    },
}


ALL_TOOLS: list[dict[str, Any]] = [SCHEMA_LOOKUP_TOOL]


def execute_schema_lookup(table_names: Any) -> str:
    """按表名拉完整 schema 文本。

    无效输入/未知表都返回带说明的字符串而不是抛异常 —— LLM 看到错误描述能自我纠正
    （例如把不存在的表名换成存在的）。
    """
    if not isinstance(table_names, list) or not table_names:
        return "[schema_lookup 错误] 参数 table_names 必须是非空数组"
    if len(table_names) > MAX_TABLES_PER_CALL:
        return f"[schema_lookup 错误] 一次最多查 {MAX_TABLES_PER_CALL} 张表"

    parts: list[str] = []
    unknown: list[str] = []
    for name in table_names:
        if not isinstance(name, str):
            continue
        upper = name.upper()
        if upper not in ALLOWED_TABLES:
            unknown.append(name)
            continue
        t = get_table(upper)
        if t is not None:
            parts.append(schema_to_prompt_text(t))

    if unknown:
        parts.append(
            f"[以下表名不存在，请检查拼写或换其他表：{', '.join(unknown)}]"
        )
    return "\n\n".join(parts) if parts else "[未返回任何 schema]"


def execute_tool(name: str, arguments_json: str) -> str:
    """统一工具分发入口。

    LLM 返回的 tool_calls[i].function.arguments 是 JSON 字符串，先解析再分发。
    所有调用都写日志（成功 + 失败 + 未知工具）。
    """
    try:
        args = json.loads(arguments_json) if arguments_json else {}
    except json.JSONDecodeError as e:
        result = f"[工具参数 JSON 解析失败] {e}"
        log_event("tool_call", {
            "name": name,
            "arguments_raw": arguments_json,
            "ok": False,
            "result_preview": result,
        })
        return result

    if name == "schema_lookup":
        table_names = args.get("table_names", [])
        result = execute_schema_lookup(table_names)
        log_event("tool_call", {
            "name": name,
            "arguments": args,
            "ok": True,
            "result_preview": result[:200],
        })
        return result

    result = f"[未知工具：{name}]"
    log_event("tool_call", {
        "name": name,
        "arguments": args,
        "ok": False,
        "result_preview": result,
    })
    return result
