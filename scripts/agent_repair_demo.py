"""
Agent 自修复链路 fault-injection 演示。

为什么需要这个脚本：
  Day 6 baseline 15/15 全通过，但**自修复一次都没触发**（DeepSeek 这一轮没在
  任何题上犯列名幻觉）。这是工程上的好结果，但故事性弱 —— 没法证明"SQL 报错时
  Agent 真的能修"。本脚本独立验证这条路径：

  1. 故意构造一条会报错的 SQL（典型场景：EKKO.NETWR 列名幻觉）
  2. 把它当作"首轮失败历史"塞进 build_repair_messages
  3. 调一次 LLM 看修复后的 SQL
  4. 执行修复 SQL，验证能跑通

  这等价于 Agent _generate 节点在 attempt > 0 时的行为，是 Agent 自修复
  分支的"单元验收"。

用法：
  python -m scripts.agent_repair_demo
"""
from __future__ import annotations

import sys

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

from src.config import require_api_key
from src.db import run_sql, table_row_counts
from src.main import generate_sql_with_tools
from src.prompts import build_repair_messages
from src.retriever import retrieve_schema_text


def demo_column_hallucination_repair() -> bool:
    """场景一：EKKO.NETWR 列名幻觉（这是 Day 4 baseline 实际发生过的错）。

    EKKO（采购订单抬头）没有 NETWR 字段。采购订单金额在 EKPO（行项目）的 NETPR。
    LLM 在 Day 4 Some run 把 NETWR 写到 EKKO 上 → SQLite 报错。
    """
    question = "对比 2025 年销售订单总额和采购订单总额，哪一边更高"
    today = "2026-05-20"

    bad_sql = (
        "SELECT 'sales' AS biz, SUM(VBAK.NETWR) AS total FROM VBAK "
        "WHERE VBAK.ERDAT >= '2025-01-01' AND VBAK.ERDAT < '2026-01-01' "
        "UNION ALL "
        "SELECT 'purchase' AS biz, SUM(EKKO.NETWR) AS total FROM EKKO "
        "WHERE EKKO.BEDAT >= '2025-01-01' AND EKKO.BEDAT < '2026-01-01'"
    )

    print("=" * 70)
    print("场景 1：列名幻觉  EKKO.NETWR （EKKO 表里没有 NETWR 字段）")
    print("=" * 70)
    print("\n【故意构造的首轮 SQL】")
    print(bad_sql)

    bad_result = run_sql(bad_sql)
    print(f"\n【真实执行结果】ok={bad_result['ok']}")
    if not bad_result["ok"]:
        print(f"  error: {bad_result['error']}")
    else:
        print("  没报错（本场景不成立，请换一个 bad SQL）")
        return False

    fake_history = [{
        "attempt": 0,
        "sql": bad_sql,
        "ok": False,
        "error": bad_result["error"],
        "row_count": 0,
    }]

    schema_text, hits = retrieve_schema_text(question, top_k=5)
    print(f"\n【RAG 召回】top-5: {[(n, round(s, 2)) for n, s in hits]}")

    messages = build_repair_messages(
        user_question=question,
        today=today,
        schema_text=schema_text,
        with_tool_catalog=True,
        history=fake_history,
    )

    print("\n【触发自修复：调用 LLM with build_repair_messages】")
    gen = generate_sql_with_tools(
        messages, use_tools=True, max_rounds=3, tag="repair_demo_1"
    )
    if gen["tool_calls"]:
        print(f"  LLM 调了 {len(gen['tool_calls'])} 次 schema_lookup 核查字段：")
        for tc in gen["tool_calls"]:
            print(f"    · {tc['name']}({tc['arguments']})")

    print(f"\n【修复后 SQL】（{gen['rounds']} 轮 LLM 调用，"
          f"tokens in={gen['total_input_tokens']} out={gen['total_output_tokens']}）")
    print(gen["sql"])

    fixed_result = run_sql(gen["sql"])
    print(f"\n【修复后执行结果】ok={fixed_result['ok']}, "
          f"row_count={fixed_result.get('row_count', 0)}")
    if fixed_result["ok"]:
        print("【数据预览】", fixed_result["rows"][:3])
        return True
    else:
        print(f"  仍然报错：{fixed_result['error']}")
        return False


def main():
    require_api_key()
    counts = table_row_counts()
    empty = [t for t, c in counts.items() if c <= 0]
    if empty:
        print(f"[!] 数据库未就绪，空表：{empty}。请先 python -m data.seed_data")
        sys.exit(1)

    ok = demo_column_hallucination_repair()
    print("\n" + "=" * 70)
    print(f"自修复演示结果：{'SUCCESS' if ok else 'FAILED'}")
    print("=" * 70)


if __name__ == "__main__":
    main()
