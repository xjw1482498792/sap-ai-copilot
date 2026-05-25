"""
LangGraph SQL 自修复 Agent（Day 6 核心亮点）。

业务动机：
  Day 4 跑完 15 题，唯一一道仍 FAIL 的 L5-03（"对比 2025 销售总额和采购总额"）
  原因是 LLM 把 EKKO.NETWR 列名幻觉了一次 —— RAG 召的表都对，schema_lookup
  也没去调，最后 SQLite 抛 `no such column: EKKO.NETWR`。这种"先写错→执行报错"
  的场景，单次 prompt 永远救不回来，必须靠"执行反馈 → 重新生成"的循环。

  Day 4 同时还出现过另一类病：SQL 跑通但只覆盖了一半语义（销售总额算了，采购
  没算），结果 row_count=1 但题目要求对比两边。这一类纯靠 SQL error 救不动，
  暂时不在 Day 6 自修复范围内（Day 9 扩刁难题集再单独处理）。

状态机设计：
  START → generate → execute → (ok 或耗尽次数) END
                       └── reflect → generate（循环）

  - generate: 首轮按 build_sql_messages；非首轮按 build_repair_messages，
              user message 拼上所有历次失败的 SQL + 错误信息。
              内部仍走 generate_sql_with_tools，保留 schema_lookup tool 调用能力
              —— Day 4 经验：列名幻觉的标准补救动作就是先 schema_lookup 核字段
  - execute: 跑 run_sql，把 (sql, ok, error, row_count) 追加到 history
  - reflect: 当前只 bump attempt 计数器。设计成独立节点是为了 Day 7-8 扩展
             （可以加 LLM 反思一步、加 plan 节点、加 RAG 重召等），现在保持极简
  - 路由：execute 之后用 route_after_execute 判断 END / reflect

复用：
  - generate_sql_with_tools()（src/main.py）—— 不重写 tool calling 循环
  - build_sql_messages / build_repair_messages（src/prompts.py）
  - run_sql / log_event 同其他层

只暴露 run_agent() 一个对外入口，返回字段尽量和 generate_sql_with_tools() 对齐
（sql / total_input_tokens / total_latency_ms / rounds / tool_calls），方便
main.py 和 eval/run_eval.py 用最小改动接入。
"""
from __future__ import annotations

import re
from typing import Optional, TypedDict

from langgraph.graph import StateGraph, START, END

from src.db import run_sql
from src.log import log_event
from src.prompts import build_sql_messages, build_repair_messages


# SQLite 错误归一化签名表：把"列名/表名不同但错误类别相同"的错误归到同一签名，
# 方便 reflect 节点判断"两次 attempt 是不是犯了同一类错"。提取出来用专门常量
# 是为了让单元测试可以直接拿来对照 —— Day 7-8 引入。
_ERROR_SIGNATURE_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"no such column[:\s]+(\S+)", re.I), "no_such_column"),
    (re.compile(r"no such table[:\s]+(\S+)", re.I), "no_such_table"),
    (re.compile(r"ambiguous column name", re.I), "ambiguous_column"),
    (re.compile(r"incomplete input", re.I), "incomplete_input"),
    (re.compile(r"syntax error", re.I), "syntax_error"),
    (re.compile(r"only execute one statement", re.I), "multiple_statements"),
    (re.compile(r"no such function", re.I), "no_such_function"),
]


def _normalize_error(error: str) -> str:
    """把 SQLite 错误归一到一个签名字符串，用于判定两次错误是否"同类"。

    例：'no such column: EKKO.NETWR' → 'no_such_column:EKKO.NETWR'
    例：'incomplete input' → 'incomplete_input'

    匹配不到时退化为错误文本的前 60 字符，保证可对比但保留区分度。
    """
    if not error:
        return ""
    text = error.strip()
    for pat, kind in _ERROR_SIGNATURE_PATTERNS:
        m = pat.search(text)
        if m:
            if m.groups():
                # 把"具体列名/表名"也带进签名，让 X.Y 错和 X.Z 错被识别成不同错
                detail = m.group(1).strip().rstrip(",.;:'`\"")
                return f"{kind}:{detail}"
            return kind
    return text[:60]


class AgentState(TypedDict, total=False):
    # 输入：调用方一次性传入，节点内只读
    question: str
    today: str
    schema_text: Optional[str]
    use_tools: bool
    max_tool_rounds: int
    max_repair_attempts: int   # 允许的最大"修复"次数，不含首次。0=不自修复

    # 累积统计（每个节点按需更新）
    total_input_tokens: int
    total_output_tokens: int
    total_latency_ms: int
    rounds_total: int          # 所有 attempt 的 LLM 调用次数之和（含 tool 轮）
    tool_calls_record: list[dict]

    # 当前状态
    sql: str                   # 当前 attempt 生成的 SQL
    exec_result: dict          # 当前 attempt 执行结果
    attempt: int               # 第几次尝试（0=首次，1=第一次修复，依此类推）
    history: list[dict]        # 每次尝试的 (attempt, sql, ok, error, row_count) 历史

    # Day 7-8 反思节点新增：连续相同 SQLite error 的检测状态
    repeat_count: int          # 连续相同错误的次数（0=未检测到/首次错误）
    last_error_signature: str  # 最近一次 error 的归一化签名


def _generate(state: AgentState) -> dict:
    """生成 SQL 节点。

    首轮用 build_sql_messages，非首轮用 build_repair_messages 把错误塞进 prompt。
    实际 LLM 调用走 generate_sql_with_tools，因此保留 Day 4 的 schema_lookup
    tool 能力 —— 修复时 LLM 可以主动查字段。
    """
    # 延迟导入：src/main.py 不要在 import-time 就被拉起，避免循环引用
    from src.main import generate_sql_with_tools

    attempt = state.get("attempt", 0)
    question = state["question"]
    today = state["today"]
    schema_text = state.get("schema_text")
    use_tools = state.get("use_tools", True)
    max_tool_rounds = state.get("max_tool_rounds", 3)
    history = state.get("history", [])

    if attempt == 0:
        messages = build_sql_messages(
            question, today,
            schema_text=schema_text,
            with_tool_catalog=use_tools,
        )
    else:
        # Day 7-8：把 reflect 节点检测到的"重复错误状态"透传给 build_repair_messages，
        # 让 prompt 顶部加 REPEAT_ERROR_WARNING 强迫 LLM 换思路
        messages = build_repair_messages(
            user_question=question,
            today=today,
            schema_text=schema_text,
            with_tool_catalog=use_tools,
            history=history,
            repeat_count=state.get("repeat_count", 0),
            error_signature=state.get("last_error_signature", ""),
        )

    gen = generate_sql_with_tools(
        messages,
        use_tools=use_tools,
        max_rounds=max_tool_rounds,
        tag=f"agent_a{attempt}",
    )

    log_event("agent_node", {
        "node": "generate",
        "attempt": attempt,
        "rounds": gen["rounds"],
        "tool_calls": len(gen["tool_calls"]),
        "sql_preview": (gen["sql"] or "")[:200],
    })

    return {
        "sql": gen["sql"],
        "total_input_tokens": state.get("total_input_tokens", 0) + gen["total_input_tokens"],
        "total_output_tokens": state.get("total_output_tokens", 0) + gen["total_output_tokens"],
        "total_latency_ms": state.get("total_latency_ms", 0) + gen["total_latency_ms"],
        "rounds_total": state.get("rounds_total", 0) + gen["rounds"],
        "tool_calls_record": state.get("tool_calls_record", []) + gen["tool_calls"],
    }


def _execute(state: AgentState) -> dict:
    """执行 SQL 节点：跑 run_sql，把结果追加到 history。"""
    sql = state["sql"]
    attempt = state.get("attempt", 0)
    result = run_sql(sql)

    history = state.get("history", []) + [{
        "attempt": attempt,
        "sql": sql,
        "ok": result["ok"],
        "error": result.get("error", ""),
        "row_count": result.get("row_count", 0),
    }]

    log_event("agent_node", {
        "node": "execute",
        "attempt": attempt,
        "ok": result["ok"],
        "row_count": result.get("row_count", 0),
        "error_preview": (result.get("error") or "")[:200],
    })

    return {"exec_result": result, "history": history}


def _route_after_execute(state: AgentState) -> str:
    """执行后路由：成功或耗尽 → end；执行报错且还能再修 → reflect。

    注意"SQL 跑通但 row_count=0"暂时不当作失败 —— 业务 SQL 0 行可能是真没数据，
    硬触发自修复反而会让 LLM 编造过滤条件。Day 9 扩刁难题集时单独处理这类。
    """
    result = state["exec_result"]
    attempt = state.get("attempt", 0)
    max_repair = state.get("max_repair_attempts", 3)
    if result["ok"]:
        return "end"
    if attempt >= max_repair:
        return "end"
    return "reflect"


def _reflect(state: AgentState) -> dict:
    """反思节点：bump attempt + 检测连续相同 SQLite 错误。

    Day 7-8 升级：除了 attempt += 1，额外比较最近一次 error 与上一次 error 的归一化
    签名，相同则 repeat_count += 1，下一轮 _generate 会让 prompt 顶部追加
    REPEAT_ERROR_WARNING，强迫 LLM 跳出"反复犯同样错"的循环。

    设计动机（来自 Day 9 baseline）：D9-11 在 _strip_code_fence bug 修复前
    曾经连续 3 次 attempt 都报 incomplete input，反思节点本应在第 2 次时识别
    出来并强化提示。即便 D9-11 这一题已经被 bug 修复彻底救回，重复错误检测
    对未来未见过的语义层失败仍是必要的工程稳健性增强。
    """
    history = state.get("history", [])
    new_attempt = state.get("attempt", 0) + 1

    repeat_count = 0
    last_sig = ""
    if history and not history[-1]["ok"]:
        last_sig = _normalize_error(history[-1].get("error", ""))
        if len(history) >= 2 and not history[-2]["ok"]:
            prev_sig = _normalize_error(history[-2].get("error", ""))
            if last_sig and last_sig == prev_sig:
                repeat_count = state.get("repeat_count", 0) + 1

    log_event("agent_node", {
        "node": "reflect",
        "next_attempt": new_attempt,
        "repeat_count": repeat_count,
        "last_error_signature": last_sig,
    })
    return {
        "attempt": new_attempt,
        "repeat_count": repeat_count,
        "last_error_signature": last_sig,
    }


def _build_graph():
    g = StateGraph(AgentState)
    g.add_node("generate", _generate)
    g.add_node("execute", _execute)
    g.add_node("reflect", _reflect)
    g.add_edge(START, "generate")
    g.add_edge("generate", "execute")
    g.add_conditional_edges("execute", _route_after_execute, {
        "end": END,
        "reflect": "reflect",
    })
    g.add_edge("reflect", "generate")
    return g.compile()


_GRAPH = None


def get_graph():
    """图编译有点重，懒加载。"""
    global _GRAPH
    if _GRAPH is None:
        _GRAPH = _build_graph()
    return _GRAPH


def run_agent(
    question: str,
    today: str,
    schema_text: Optional[str],
    use_tools: bool = True,
    max_tool_rounds: int = 3,
    max_repair_attempts: int = 3,
) -> dict:
    """对外入口。返回字段对齐 generate_sql_with_tools，多带 agent 元信息。

    返回：
      {
        "sql": str,                       # 最后一次生成的 SQL
        "exec_result": dict,              # 最后一次执行结果（ok / rows / error）
        "total_input_tokens": int,        # 累加所有 attempt
        "total_output_tokens": int,
        "total_latency_ms": int,
        "rounds": int,                    # LLM 调用总次数（所有 attempt 的 tool 轮加总）
        "attempts": int,                  # 实际尝试次数（含首次）
        "repair_used": bool,              # 是否真的进入过自修复
        "tool_calls": list[dict],         # 所有 attempt 的 tool_calls 合并
        "history": list[dict],            # 每次尝试的 sql + 结果，调试/评测用
      }
    """
    initial: AgentState = {
        "question": question,
        "today": today,
        "schema_text": schema_text,
        "use_tools": use_tools,
        "max_tool_rounds": max_tool_rounds,
        "max_repair_attempts": max_repair_attempts,
        "total_input_tokens": 0,
        "total_output_tokens": 0,
        "total_latency_ms": 0,
        "rounds_total": 0,
        "tool_calls_record": [],
        "history": [],
        "attempt": 0,
        "repeat_count": 0,
        "last_error_signature": "",
    }
    # LangGraph 默认 recursion_limit=25 对 max_repair=2 完全够（最多 5-6 步），
    # 但为了防止 reflect/generate 死循环造成爆栈，显式限到一个合理上界
    config = {"recursion_limit": 2 + 3 * (max_repair_attempts + 1)}
    final = get_graph().invoke(initial, config=config)

    history = final.get("history", [])
    attempts = (history[-1]["attempt"] + 1) if history else 0
    log_event("agent_end", {
        "attempts": attempts,
        "repair_used": attempts > 1,
        "final_ok": final.get("exec_result", {}).get("ok", False),
        "total_input_tokens": final["total_input_tokens"],
        "total_output_tokens": final["total_output_tokens"],
        "total_latency_ms": final["total_latency_ms"],
    })

    return {
        "sql": final["sql"],
        "exec_result": final["exec_result"],
        "total_input_tokens": final["total_input_tokens"],
        "total_output_tokens": final["total_output_tokens"],
        "total_latency_ms": final["total_latency_ms"],
        "rounds": final["rounds_total"],
        "attempts": attempts,
        "repair_used": attempts > 1,
        "tool_calls": final["tool_calls_record"],
        "history": history,
    }
