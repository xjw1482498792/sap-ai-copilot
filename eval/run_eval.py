"""
批量评测脚本。

判分逻辑（粗判，宁松勿严）：
  - 表分        ：expected_tables 必须全部出现在 SQL 里（子串大小写不敏感）
  - 子句必须分  ：must_have 列表全中
  - 子句备选分  ：must_have_any 每组 OR 关系，每组至少命中 1 个
  - 可执行分    ：executable_required=true 时，SQL 必须能跑通且 row_count>0
  - 综合 pass   ：以上四项全过才算 pass

为什么不直接对比 SQL 字符串：LLM 的 SQL 风格千变万化（别名、列顺序、JOIN 方向、
是否用子查询），死匹配会让评测变成 LLM 风格测试，掩盖业务正确性。
表 + 关键字段 + 可执行的组合判分能稳定衡量"业务意图是否抓对"。

Day 4 改动：默认开 Function Calling（schema_lookup tool）。每题额外记录
  tool_rounds、tool_call_count、looked_up_tables，用于诊断 LLM 是否在
  MAKT 这类漏召题上正确调了 tool。

Day 6 改动：加 --use-agent 开关，走 LangGraph SQL 自修复 Agent。每题额外
  记录 attempts / repair_used / repair_succeeded，summary 加"被自修复救回
  的题数"和"修复后仍失败的题数"。

用法：
    python -m eval.run_eval                  # 默认 RAG + Tools（Day 4）
    python -m eval.run_eval --use-agent      # 上 LangGraph 自修复（Day 6）
    python -m eval.run_eval --no-tools       # 关 Tools，跑 Day 3 纯 RAG
    python -m eval.run_eval --no-rag         # 关 RAG，跑 Day 2 全 schema 朴素（自动连带关 tool）
    python -m eval.run_eval --top-k 3        # RAG 召回 top-3
    python -m eval.run_eval --limit 5        # 只跑前 5 题
    python -m eval.run_eval --case L4-01     # 只跑指定 case
    python -m eval.run_eval --tag day6       # 输出文件名 baseline_day6.json
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from statistics import mean

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

from tabulate import tabulate

from src.config import require_api_key
from src.db import run_sql, table_row_counts
from src.log import log_event
from src.main import generate_sql_with_tools
from src.prompts import build_sql_messages

ROOT = Path(__file__).resolve().parent.parent
TEST_FILE = ROOT / "eval" / "test_cases.json"
EVAL_DIR = ROOT / "eval"


def _contains_ci(haystack: str, needle: str) -> bool:
    """大小写不敏感子串包含。"""
    return needle.lower() in haystack.lower()


def _table_in_sql(sql: str, table: str) -> bool:
    """检查表名是否作为独立 token 出现在 SQL 里（避免 BSEG 命中 BSE）。"""
    return re.search(rf"\b{re.escape(table)}\b", sql, flags=re.IGNORECASE) is not None


def grade_case(case: dict, sql: str, exec_result: dict) -> dict:
    """对单题判分，返回详细评分明细。"""
    expected_tables = case.get("expected_tables", [])
    must_have = case.get("must_have", [])
    must_have_any = case.get("must_have_any", [])
    executable_required = case.get("executable_required", True)

    missing_tables = [t for t in expected_tables if not _table_in_sql(sql, t)]
    missing_must = [s for s in must_have if not _contains_ci(sql, s)]
    missing_any = []
    for group in must_have_any:
        if not any(_contains_ci(sql, s) for s in group):
            missing_any.append(group)

    executable_ok = True
    if executable_required:
        executable_ok = exec_result["ok"] and exec_result.get("row_count", 0) > 0

    tables_ok = not missing_tables
    must_ok = not missing_must
    any_ok = not missing_any
    passed = tables_ok and must_ok and any_ok and executable_ok

    return {
        "pass": passed,
        "tables_ok": tables_ok,
        "must_have_ok": must_ok,
        "must_have_any_ok": any_ok,
        "executable_ok": executable_ok,
        "missing_tables": missing_tables,
        "missing_must_have": missing_must,
        "missing_must_have_any": missing_any,
    }


def _extract_looked_up_tables(tool_calls: list[dict]) -> list[str]:
    """从 tool_calls 记录里抽出曾经被 schema_lookup 查过的表名（去重保序）。"""
    seen: list[str] = []
    for tc in tool_calls:
        if tc.get("name") != "schema_lookup":
            continue
        try:
            args = json.loads(tc.get("arguments") or "{}")
        except json.JSONDecodeError:
            continue
        for name in args.get("table_names", []) or []:
            if isinstance(name, str):
                upper = name.upper()
                if upper not in seen:
                    seen.append(upper)
    return seen


def run_one(case: dict, use_rag: bool, use_tools: bool, top_k: int,
            max_tool_rounds: int, use_agent: bool = False,
            max_repair_attempts: int = 3) -> dict:
    """跑单题：（可选 RAG 召回）→ 调 LLM 生 SQL → 跑 SQL → 判分。

    use_agent=True 时走 LangGraph 自修复 Agent（Day 6），否则走 Day 4 单次 tool calling。
    """
    question = case["question"]
    today = "2026-05-20"  # 评测用固定日期，保证可复现

    schema_text = None
    rag_hits: list[tuple[str, float]] = []
    rag_latency_ms = 0
    if use_rag:
        from src.retriever import retrieve_schema_text
        t_rag = time.time()
        schema_text, rag_hits = retrieve_schema_text(question, top_k=top_k)
        rag_latency_ms = int((time.time() - t_rag) * 1000)

    tool_enabled = use_tools and use_rag

    agent_history: list[dict] = []
    attempts = 1
    repair_used = False

    if use_agent and use_rag:
        from src.agent import run_agent
        agent_out = run_agent(
            question=question,
            today=today,
            schema_text=schema_text,
            use_tools=tool_enabled,
            max_tool_rounds=max_tool_rounds,
            max_repair_attempts=max_repair_attempts,
        )
        sql = agent_out["sql"]
        exec_result = agent_out["exec_result"]
        latency_ms = agent_out["total_latency_ms"]
        in_tok = agent_out["total_input_tokens"]
        out_tok = agent_out["total_output_tokens"]
        rounds = agent_out["rounds"]
        tool_calls = agent_out["tool_calls"]
        agent_history = agent_out["history"]
        attempts = agent_out["attempts"]
        repair_used = agent_out["repair_used"]
    else:
        messages = build_sql_messages(
            question, today,
            schema_text=schema_text,
            with_tool_catalog=tool_enabled,
        )
        gen = generate_sql_with_tools(
            messages,
            use_tools=tool_enabled,
            max_rounds=max_tool_rounds,
            tag=f"eval_{case['id']}",
        )
        sql = gen["sql"]
        exec_result = run_sql(sql)
        latency_ms = gen["total_latency_ms"]
        in_tok = gen["total_input_tokens"]
        out_tok = gen["total_output_tokens"]
        rounds = gen["rounds"]
        tool_calls = gen["tool_calls"]

    grade = grade_case(case, sql, exec_result)
    looked_up = _extract_looked_up_tables(tool_calls)

    return {
        "id": case["id"],
        "difficulty": case["difficulty"],
        "question": question,
        "sql": sql,
        **grade,
        "row_count": exec_result.get("row_count", 0),
        "sql_error": exec_result.get("error", "") if not exec_result["ok"] else "",
        "latency_ms": latency_ms,
        "usage": {
            "prompt_tokens": in_tok,
            "completion_tokens": out_tok,
            "total_tokens": in_tok + out_tok,
        },
        "model": "",  # 由 chat() 内部日志记录，这里不再单独取
        "rag": {
            "enabled": use_rag,
            "top_k": top_k if use_rag else 0,
            "latency_ms": rag_latency_ms,
            "hits": [{"table": n, "score": s} for n, s in rag_hits],
        },
        "tools": {
            "enabled": tool_enabled,
            "rounds": rounds,
            "call_count": len(tool_calls),
            "looked_up_tables": looked_up,
        },
        "agent": {
            "enabled": use_agent and use_rag,
            "attempts": attempts,
            "repair_used": repair_used,
            # 修复是否成功：触发了修复且最终结果 ok
            "repair_succeeded": repair_used and exec_result["ok"],
            "history": [
                {"attempt": h["attempt"], "ok": h["ok"],
                 "error": (h.get("error") or "")[:200],
                 "row_count": h.get("row_count", 0)}
                for h in agent_history
            ],
        },
    }


def summarize(results: list[dict]) -> dict:
    """聚合统计。"""
    n = len(results)
    passed = sum(1 for r in results if r["pass"])
    by_diff: dict[str, list[dict]] = {}
    for r in results:
        by_diff.setdefault(r["difficulty"], []).append(r)

    by_difficulty_stats = {}
    for diff in sorted(by_diff.keys()):
        items = by_diff[diff]
        by_difficulty_stats[diff] = {
            "total": len(items),
            "passed": sum(1 for x in items if x["pass"]),
            "pass_rate": round(sum(1 for x in items if x["pass"]) / len(items), 3),
        }

    tool_calls_total = sum(r.get("tools", {}).get("call_count", 0) for r in results)
    cases_with_tool_call = sum(
        1 for r in results if r.get("tools", {}).get("call_count", 0) > 0
    )

    repair_triggered = sum(1 for r in results if r.get("agent", {}).get("repair_used"))
    repair_succeeded = sum(1 for r in results if r.get("agent", {}).get("repair_succeeded"))
    avg_attempts = (
        round(mean(r.get("agent", {}).get("attempts", 1) for r in results), 2)
        if n else 0
    )

    return {
        "total_cases": n,
        "passed": passed,
        "pass_rate": round(passed / n, 3) if n else 0,
        "by_difficulty": by_difficulty_stats,
        "total_input_tokens": sum(r["usage"]["prompt_tokens"] for r in results),
        "total_output_tokens": sum(r["usage"]["completion_tokens"] for r in results),
        "avg_input_tokens": round(mean(r["usage"]["prompt_tokens"] for r in results)),
        "avg_output_tokens": round(mean(r["usage"]["completion_tokens"] for r in results)),
        "avg_latency_ms": round(mean(r["latency_ms"] for r in results)),
        "tables_pass_rate": round(sum(1 for r in results if r["tables_ok"]) / n, 3) if n else 0,
        "must_have_pass_rate": round(sum(1 for r in results if r["must_have_ok"]) / n, 3) if n else 0,
        "executable_pass_rate": round(sum(1 for r in results if r["executable_ok"]) / n, 3) if n else 0,
        "tool_calls_total": tool_calls_total,
        "cases_with_tool_call": cases_with_tool_call,
        "tool_usage_rate": round(cases_with_tool_call / n, 3) if n else 0,
        "agent_repair_triggered": repair_triggered,
        "agent_repair_succeeded": repair_succeeded,
        "agent_avg_attempts": avg_attempts,
    }


def print_table(results: list[dict]) -> None:
    """终端漂亮打印每题结果。"""
    rows = []
    for r in results:
        status = "PASS" if r["pass"] else "FAIL"
        flags = "".join([
            "T" if r["tables_ok"] else ".",
            "M" if r["must_have_ok"] else ".",
            "A" if r["must_have_any_ok"] else ".",
            "E" if r["executable_ok"] else ".",
        ])
        tools_info = r.get("tools", {})
        tc = tools_info.get("call_count", 0)
        lookup_str = ",".join(tools_info.get("looked_up_tables", [])) if tc else ""
        agent_info = r.get("agent", {})
        attempts = agent_info.get("attempts", 1)
        # 仅在触发了自修复时高亮 attempt 数
        attempts_str = str(attempts) if agent_info.get("repair_used") else ""
        rows.append([
            r["id"],
            r["difficulty"],
            status,
            flags,
            r["row_count"],
            r["latency_ms"],
            r["usage"]["prompt_tokens"],
            r["usage"]["completion_tokens"],
            tc,
            lookup_str,
            attempts_str,
        ])
    print(tabulate(
        rows,
        headers=["ID", "难度", "状态", "TMAE", "行数", "延迟ms", "in_tok",
                 "out_tok", "tool", "lookup", "尝试"],
        tablefmt="simple",
    ))
    print("\n  TMAE 含义：T=表全中 M=必含子句全中 A=备选子句全中 E=可执行且非空")
    print("  tool=本题 schema_lookup 调用次数  lookup=补充查询过的表")
    print("  尝试=Agent 触发自修复时的总 attempt 次数（空=首次即过）")


def print_summary(summary: dict) -> None:
    print("\n" + "=" * 60)
    print("评测汇总")
    print("=" * 60)
    print(f"总题数        : {summary['total_cases']}")
    print(f"通过          : {summary['passed']} / {summary['total_cases']} "
          f"({summary['pass_rate'] * 100:.1f}%)")
    print(f"表分通过率    : {summary['tables_pass_rate'] * 100:.1f}%")
    print(f"子句通过率    : {summary['must_have_pass_rate'] * 100:.1f}%")
    print(f"可执行通过率  : {summary['executable_pass_rate'] * 100:.1f}%")
    print("\n按难度分布：")
    for diff, s in summary["by_difficulty"].items():
        print(f"  {diff}: {s['passed']}/{s['total']}  ({s['pass_rate'] * 100:.1f}%)")
    print(f"\n平均 input  tokens : {summary['avg_input_tokens']}")
    print(f"平均 output tokens : {summary['avg_output_tokens']}")
    print(f"平均 端到端延迟    : {summary['avg_latency_ms']} ms")
    print(f"总 input  tokens   : {summary['total_input_tokens']}")
    print(f"总 output tokens   : {summary['total_output_tokens']}")
    print(f"\nTool 调用总次数    : {summary['tool_calls_total']}")
    print(f"使用 tool 的题数   : {summary['cases_with_tool_call']} / "
          f"{summary['total_cases']} "
          f"({summary['tool_usage_rate'] * 100:.1f}%)")
    if summary.get("agent_repair_triggered", 0):
        print(f"\nAgent 平均 attempt 次数 : {summary['agent_avg_attempts']}")
        print(f"触发自修复的题数        : {summary['agent_repair_triggered']}")
        print(f"  └─ 修复后通过        : {summary['agent_repair_succeeded']}")
        print(f"  └─ 修复后仍失败      : "
              f"{summary['agent_repair_triggered'] - summary['agent_repair_succeeded']}")
    else:
        print(f"\nAgent 平均 attempt 次数 : {summary.get('agent_avg_attempts', 0)}（本轮无自修复触发）")


def main():
    parser = argparse.ArgumentParser(description="评测脚本（Day 4 起默认 RAG + Tools）")
    parser.add_argument("--limit", type=int, default=0, help="只跑前 N 题（0=全部）")
    parser.add_argument("--case", type=str, default="", help="只跑指定 case id（如 L4-01）")
    parser.add_argument("--tag", type=str, default="day4", help="基线文件名 tag")
    parser.add_argument("--no-rag", action="store_true",
                        help="禁用 Schema RAG，回退 Day 2 全 schema（同时强制关 tool）")
    parser.add_argument("--no-tools", action="store_true",
                        help="禁用 Function Calling，回退 Day 3 纯 RAG（对比用）")
    parser.add_argument("--top-k", type=int, default=5,
                        help="RAG 召回表数量，默认 5")
    parser.add_argument("--max-tool-rounds", type=int, default=3,
                        help="单题最多 tool calling 轮数（含最终给 SQL 那轮），默认 3")
    parser.add_argument("--use-agent", action="store_true",
                        help="启用 LangGraph SQL 自修复 Agent（Day 6）")
    parser.add_argument("--max-repair-attempts", type=int, default=3,
                        help="Agent 自修复最大重试次数（不含首次），默认 3（Day 7-8 起从 2 升到 3）")
    args = parser.parse_args()
    use_rag = not args.no_rag
    use_tools = not args.no_tools
    use_agent = args.use_agent

    require_api_key()

    counts = table_row_counts()
    empty = [t for t, c in counts.items() if c <= 0]
    if empty:
        print(f"[!] 数据库未就绪，空表：{empty}。请先 python -m data.seed_data")
        sys.exit(1)

    with open(TEST_FILE, "r", encoding="utf-8") as f:
        suite = json.load(f)
    cases = suite["cases"]

    if args.case:
        cases = [c for c in cases if c["id"] == args.case]
        if not cases:
            print(f"[!] 找不到 case id={args.case}")
            sys.exit(1)
    elif args.limit > 0:
        cases = cases[: args.limit]

    if not use_rag:
        mode_label = "全 schema（无 RAG）"
    elif use_agent and use_tools:
        mode_label = f"Agent + RAG top-{args.top_k} + Tools (max_repair={args.max_repair_attempts})"
    elif use_tools:
        mode_label = f"RAG top-{args.top_k} + Tools"
    else:
        mode_label = f"RAG top-{args.top_k}（无 tools）"
    print(f"\n=== 开始评测 ({len(cases)} 题, {mode_label}) ===\n")
    log_event("eval_start", {
        "total": len(cases), "tag": args.tag,
        "use_rag": use_rag, "use_tools": use_tools and use_rag,
        "use_agent": use_agent and use_rag,
        "top_k": args.top_k,
        "max_repair_attempts": args.max_repair_attempts,
    })

    results = []
    t_start = time.time()
    for i, case in enumerate(cases, 1):
        print(f"[{i}/{len(cases)}] {case['id']} ({case['difficulty']}) {case['question'][:40]}")
        try:
            r = run_one(case, use_rag=use_rag, use_tools=use_tools,
                        top_k=args.top_k, max_tool_rounds=args.max_tool_rounds,
                        use_agent=use_agent,
                        max_repair_attempts=args.max_repair_attempts)
        except Exception as e:
            print(f"  [!] 异常：{e}")
            r = {
                "id": case["id"], "difficulty": case["difficulty"],
                "question": case["question"], "sql": "",
                "pass": False, "tables_ok": False, "must_have_ok": False,
                "must_have_any_ok": False, "executable_ok": False,
                "missing_tables": case.get("expected_tables", []),
                "missing_must_have": case.get("must_have", []),
                "missing_must_have_any": case.get("must_have_any", []),
                "row_count": 0, "sql_error": str(e),
                "latency_ms": 0,
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
                "model": "",
                "rag": {"enabled": use_rag, "top_k": args.top_k if use_rag else 0,
                        "latency_ms": 0, "hits": []},
                "tools": {"enabled": use_tools and use_rag, "rounds": 0,
                          "call_count": 0, "looked_up_tables": []},
                "agent": {"enabled": use_agent and use_rag, "attempts": 0,
                          "repair_used": False, "repair_succeeded": False,
                          "history": []},
            }
        results.append(r)
        status = "PASS" if r["pass"] else "FAIL"
        tc = r.get("tools", {}).get("call_count", 0)
        tool_tag = f"  tool={tc}" if tc else ""
        agent_info = r.get("agent", {})
        attempt_tag = (
            f"  attempts={agent_info.get('attempts', 1)}"
            if agent_info.get("repair_used") else ""
        )
        print(f"      {status}  延迟 {r['latency_ms']}ms  "
              f"in/out={r['usage']['prompt_tokens']}/{r['usage']['completion_tokens']}"
              f"{tool_tag}{attempt_tag}")

    total_time = round(time.time() - t_start, 1)
    summary = summarize(results)

    print("\n")
    print_table(results)
    print_summary(summary)
    print(f"\n总耗时：{total_time}s")

    output = {
        "meta": {
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "today": "2026-05-20",
            "tag": args.tag,
            "total_wall_time_sec": total_time,
            "use_rag": use_rag,
            "use_tools": use_tools and use_rag,
            "use_agent": use_agent and use_rag,
            "top_k": args.top_k if use_rag else 0,
            "max_tool_rounds": args.max_tool_rounds,
            "max_repair_attempts": args.max_repair_attempts,
            "avg_rag_latency_ms": (
                round(mean(r["rag"]["latency_ms"] for r in results)) if use_rag else 0
            ),
        },
        "summary": summary,
        "cases": results,
    }
    out_path = EVAL_DIR / f"baseline_{args.tag}.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print(f"\n基线已写入：{out_path}")
    log_event("eval_end", {"tag": args.tag, "summary": summary})


if __name__ == "__main__":
    main()
