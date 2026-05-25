"""
端到端 CLI demo（Day 6 升级）。

流程：
  用户中文问题
   → Schema RAG 召回 top-K 相关表（BGE-small-zh + Chroma，Day 3）
   → LangGraph Agent 生成 SQL：generate → execute → (报错则) reflect → re-generate
       · 内部仍走 schema_lookup tool calling 循环（Day 4），保留主动补字段能力
       · 默认允许最多 2 次自修复（首次 + 2 次重试 = 至多 3 次 attempt）
   → SQLite 执行
   → DeepSeek 业务解读
   → 终端展示

Day 6 新增：
  --no-agent              关掉 LangGraph 自修复，回退 Day 4 单次 tool calling
  --max-repair-attempts N 最多自修复次数（不含首次），默认 2

Day 4 开关（保留）：
  --no-tools              关掉 Function Calling，回退 Day 3 纯 RAG（对比用）
  --max-tool-rounds N     单次生成内的 tool calling 轮数上限，默认 3

历史开关（保留）：
  --stream                业务解读流式吐字
  --no-rag                回退 Day 1/2 全 schema 朴素 prompt（同时会自动关 tool）
  --top-k N               改 RAG 召回数量
"""
import argparse
import json
import re
import sys
import time
from datetime import date

# Windows 控制台默认 GBK，强制 UTF-8 避免中文乱码
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

from tabulate import tabulate

from src.config import require_api_key
from src.db import run_sql, table_row_counts
from src.llm import chat, chat_stream
from src.log import log_ask, log_event, log_sql_exec
from src.prompts import build_sql_messages, build_explain_messages
from src.tools import ALL_TOOLS, execute_tool


def _typewriter_print(text: str, delay_per_char: float = 0.03) -> None:
    """逐字符打印 + 小延迟，把"几字一 chunk"匀成"一字一吐"的打字机节奏。
    DeepSeek 的 stream chunk 一次常常吐 2-4 个汉字，业务解读又只有 70 字左右，
    1.3 秒就吐完肉眼难察觉；按字节流后总耗时约 3 秒，正好和人的阅读速度对齐。"""
    for ch in text:
        print(ch, end="", flush=True)
        time.sleep(delay_per_char)


# 中文话术 trigger：只在出现这些词 / 标点时，才把"以中文开头的行"判定为
# 解释性段落起点。Day 7-8 引入白名单模式，修复 D9-11：原版"任何中文行都截"
# 的启发式会把主 SELECT 后的中文别名字段列表（如 "  月份," / "  月销售额,"）
# 误判为话术，把整段 SQL 砍掉。
_PROSE_LINE_TRIGGER = re.compile(
    "[。！？：；]|"
    "根据|首先|然后|接下来|现在|因此|所以|"
    "这条|这个|这是|这就|这里|上面|下面|上述|由于|"
    "等等|不过|另外|需要|应该|修正|重新|"
    "注意|说明|解释|总结|综上|至此|完成|"
    "让我|思考|分析|理解|意图|查询会|查询的|建议|推荐"
)


def _strip_code_fence(text: str) -> str:
    """从 LLM 输出里抽出干净 SQL。

    需要处理三类噪声：
      1. markdown 围栏 ```sql ... ```（Day 1-3 已有）
      2. tool calling 后续轮，LLM 偶尔会在 SQL 前吐一句中文开场白
         ("现在可以生成 SQL 了。") —— 用 SELECT/WITH/INSERT/UPDATE/DELETE/PRAGMA
         关键字定位真实 SQL 起点，切掉前置话术
      3. tool calling 后续轮，LLM 偶尔在给完一段 SQL 后又自言自语
         ("等等，总金额需要...") 再贴一段二次 SQL —— 用结束围栏 ``` 或
         "含明显话术 trigger 词的中文行" 作为 SQL 终点

    Day 7-8 修复（D9-11 根因）：第 4 步从"任何中文行都当终点"改为白名单
    trigger 词判定。中文字段别名（`月份,` `月销售额,`）一律保留，只有
    含"根据/这条/等等/注意/。/！/？" 等明显话术信号的中文行才视为终点。
    """
    text = text.strip()
    # 1) 整体剥首尾围栏
    if text.startswith("```"):
        text = re.sub(r"^```(?:sql)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
        text = text.strip()
    # 2) 截到第一个 SQL 关键字
    m = re.search(r"(?is)\b(SELECT|WITH|INSERT|UPDATE|DELETE|PRAGMA)\b", text)
    if m:
        text = text[m.start():]
    # 3) 截掉 SQL 后面跟的 ``` 围栏（含其后任何二次胡话）
    fence_pos = text.find("```")
    if fence_pos != -1:
        text = text[:fence_pos]
    # 4) 中文话术段落截断：只在行内含明显话术 trigger 时才截
    lines = text.splitlines()
    cut = len(lines)
    for i, ln in enumerate(lines):
        stripped = ln.strip()
        if not stripped:
            continue
        first = stripped[0]
        if not ("一" <= first <= "鿿"):
            continue
        if _PROSE_LINE_TRIGGER.search(stripped):
            cut = i
            break
    text = "\n".join(lines[:cut])
    return text.strip().rstrip(";").strip()


def _format_result(result: dict, max_rows: int = 20) -> str:
    if not result["ok"]:
        return f"[SQL 执行失败] {result['error']}"
    rows = result["rows"][:max_rows]
    if not rows:
        return "[查询无结果]"
    return tabulate(rows, headers="keys", tablefmt="simple", floatfmt=".2f")


def generate_sql_with_tools(
    messages: list[dict],
    use_tools: bool,
    max_rounds: int,
    tag: str,
) -> dict:
    """SQL 生成阶段（含 Day 4 的 tool calling 循环）。

    返回：
      {
        "sql": str,                       # 最终 SQL（已剥 code fence）
        "total_input_tokens": int,        # 多轮累加
        "total_output_tokens": int,
        "total_latency_ms": int,
        "rounds": int,                    # 实际调用 LLM 的次数
        "tool_calls": [ { "name", "arguments" }, ... ],
        "messages": list[dict],           # 完整对话历史（含 tool 消息），调试用
      }

    use_tools=False 时退化为单次调用（Day 3 行为），保持 --no-tools 等价。
    """
    total_in = 0
    total_out = 0
    total_latency = 0
    rounds = 0
    tool_calls_record: list[dict] = []

    tools_arg = ALL_TOOLS if use_tools else None

    for round_idx in range(max_rounds):
        rounds += 1
        resp = chat(
            messages,
            tools=tools_arg,
            tag=f"{tag}_r{round_idx}" if use_tools else tag,
        )
        total_in += resp["usage"]["prompt_tokens"]
        total_out += resp["usage"]["completion_tokens"]
        total_latency += resp["latency_ms"]

        tool_calls = resp.get("tool_calls")
        if not tool_calls:
            # LLM 给出了文本回答（即最终 SQL），结束循环
            sql = _strip_code_fence(resp["content"])
            return {
                "sql": sql,
                "total_input_tokens": total_in,
                "total_output_tokens": total_out,
                "total_latency_ms": total_latency,
                "rounds": rounds,
                "tool_calls": tool_calls_record,
                "messages": messages,
            }

        # 还有 tool_calls，把 assistant 这条消息追加到历史，再逐个执行 tool 并把结果追加
        messages.append({
            "role": "assistant",
            "content": resp["content"] or "",
            "tool_calls": [
                {
                    "id": tc["id"],
                    "type": "function",
                    "function": {"name": tc["name"], "arguments": tc["arguments"]},
                }
                for tc in tool_calls
            ],
        })

        for tc in tool_calls:
            tool_result = execute_tool(tc["name"], tc["arguments"])
            tool_calls_record.append({
                "name": tc["name"],
                "arguments": tc["arguments"],
                "result_preview": tool_result[:200],
            })
            messages.append({
                "role": "tool",
                "tool_call_id": tc["id"],
                "content": tool_result,
            })

    # 跑满 max_rounds 还在调 tool，把最后一次的文本（可能是空）当 SQL 返回，让上层判错
    log_event("tool_loop_exhausted", {"max_rounds": max_rounds, "tag": tag})
    last_content = messages[-1].get("content", "") if messages else ""
    return {
        "sql": _strip_code_fence(last_content if isinstance(last_content, str) else ""),
        "total_input_tokens": total_in,
        "total_output_tokens": total_out,
        "total_latency_ms": total_latency,
        "rounds": rounds,
        "tool_calls": tool_calls_record,
        "messages": messages,
    }


def ask(
    question: str,
    stream: bool = False,
    use_rag: bool = True,
    use_tools: bool = True,
    use_agent: bool = True,
    top_k: int = 5,
    max_tool_rounds: int = 3,
    max_repair_attempts: int = 3,
) -> None:
    today = date.today().isoformat()
    t_start = time.time()
    success = False

    # 第 0 步（Day 3）：Schema RAG 召回 top-K 相关表
    schema_text = None
    rag_info = ""
    if use_rag:
        from src.retriever import retrieve_schema_text  # 懒导入：--no-rag 时不付加载代价
        t_rag = time.time()
        schema_text, hits = retrieve_schema_text(question, top_k=top_k)
        rag_ms = int((time.time() - t_rag) * 1000)
        hit_str = "  ".join(f"{n}({s:.2f})" for n, s in hits)
        rag_info = f"[Schema RAG]  top{top_k} 召回（{rag_ms}ms）：{hit_str}"

    # 第 1 步：生成 SQL（Day 6 起默认走 LangGraph Agent，带自修复）
    print(f"\n问题：{question}")
    if rag_info:
        print(rag_info)

    tool_enabled = use_tools and use_rag  # --no-rag 时强制关 tool（朴素 prompt 不带 tool 指引）
    if use_agent and use_rag:
        mode_hint = f"Agent + RAG+Tools (max_repair={max_repair_attempts})"
    elif tool_enabled:
        mode_hint = "RAG+Tools"
    elif use_rag:
        mode_hint = "RAG"
    else:
        mode_hint = "全 schema"
    print(f"正在生成 SQL（{mode_hint}）...")

    # --no-agent 或 --no-rag 时走 Day 4 原直链；其余默认走 Agent 自修复
    if use_agent and use_rag:
        from src.agent import run_agent  # 懒导入：--no-agent 时不付 langgraph 加载代价
        agent_out = run_agent(
            question=question,
            today=today,
            schema_text=schema_text,
            use_tools=tool_enabled,
            max_tool_rounds=max_tool_rounds,
            max_repair_attempts=max_repair_attempts,
        )
        sql = agent_out["sql"]
        result = agent_out["exec_result"]
        gen_view = {
            "rounds": agent_out["rounds"],
            "total_latency_ms": agent_out["total_latency_ms"],
            "total_input_tokens": agent_out["total_input_tokens"],
            "total_output_tokens": agent_out["total_output_tokens"],
            "tool_calls": agent_out["tool_calls"],
        }
        # 把 Agent 的尝试历史打印出来，让"自修复"这件事在 demo 里看得见
        if agent_out["attempts"] > 1:
            print(f"\n[Agent] 共尝试 {agent_out['attempts']} 次，触发了 SQL 自修复：")
            for h in agent_out["history"]:
                tag = "PASS" if h["ok"] else "FAIL"
                line = f"  · 第 {h['attempt'] + 1} 次 [{tag}]"
                if not h["ok"]:
                    line += f"  err={h['error'][:80]}"
                else:
                    line += f"  row_count={h['row_count']}"
                print(line)
        else:
            print(f"\n[Agent] 首次生成即通过（未触发自修复）")
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
            tag="sql_gen_rag" if use_rag else "sql_gen",
        )
        sql = gen["sql"]
        result = run_sql(sql)
        gen_view = gen

    if gen_view["tool_calls"]:
        for i, tc in enumerate(gen_view["tool_calls"], 1):
            try:
                args = json.loads(tc["arguments"]) if tc["arguments"] else {}
            except json.JSONDecodeError:
                args = tc["arguments"]
            print(f"  [tool#{i}] {tc['name']}({args})")

    print(f"\n[最终 SQL]  (LLM 调用 {gen_view['rounds']} 次, 耗时 {gen_view['total_latency_ms']}ms, "
          f"tokens in={gen_view['total_input_tokens']} out={gen_view['total_output_tokens']})")
    print(sql)

    # 第 2 步：把执行结果落日志（Agent 模式里已经执行过，这里只补一条日志事件方便复盘）
    log_sql_exec(
        sql=sql,
        ok=result["ok"],
        row_count=result.get("row_count", 0),
        error=result.get("error", ""),
    )
    print(f"\n[查询结果] 共 {result.get('row_count', 0)} 行")
    result_text = _format_result(result)
    print(result_text)

    if not result["ok"]:
        if use_agent and use_rag:
            print(f"\n(Agent 已尝试 {max_repair_attempts + 1} 次仍失败，留待下一轮迭代。)")
        else:
            print("\n(--no-agent 模式不做自修复，可去掉 --no-agent 让 Agent 介入。)")
        log_ask(question=question, success=False,
                total_latency_ms=int((time.time() - t_start) * 1000),
                tag="stream" if stream else "non_stream")
        return

    # 第 3 步：业务解读
    print("\n正在生成业务解读 ...")
    messages = build_explain_messages(question, sql, result_text)
    if stream:
        print("\n[业务解读]")
        explain_resp = chat_stream(messages, on_token=_typewriter_print, tag="explain")
        print()  # 换行
        print(f"(流式耗时 {explain_resp['latency_ms']}ms, "
              f"tokens in={explain_resp['usage']['prompt_tokens']} "
              f"out={explain_resp['usage']['completion_tokens']})")
    else:
        explain_resp = chat(messages, tag="explain")
        print(f"\n[业务解读]\n{explain_resp['content']}")

    success = True
    log_ask(question=question, success=success,
            total_latency_ms=int((time.time() - t_start) * 1000),
            tag="stream" if stream else "non_stream")


def _smoke_check() -> bool:
    counts = table_row_counts()
    empty = [t for t, c in counts.items() if c <= 0]
    if empty:
        print(f"[!] 以下表为空或不存在：{empty}")
        print("    请先运行：python -m data.seed_data")
        return False
    print(f"[√] 数据库就绪：{counts}")
    return True


DEFAULT_QUESTIONS = [
    "目前数据库里一共有多少个客户？",
    "查一下 2025 年销售订单总数和总金额",
    "销售额前 5 的客户是谁，分别多少钱？",
    "今年采购订单金额最高的 3 个供应商",
]


def main():
    parser = argparse.ArgumentParser(
        description="SAP Smart Query Assistant - CLI Demo",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("question", nargs="*", help="中文业务问题，省略则跑默认示例集")
    parser.add_argument("--stream", action="store_true",
                        help="业务解读改用流式输出（逐字打印）")
    parser.add_argument("--no-rag", action="store_true",
                        help="禁用 Schema RAG，回退到 Day 1/2 全 schema 朴素 prompt（同时强制关 tool）")
    parser.add_argument("--no-tools", action="store_true",
                        help="禁用 Function Calling，回退 Day 3 纯 RAG（对比用）")
    parser.add_argument("--top-k", type=int, default=5,
                        help="Schema RAG 召回表数量，默认 5")
    parser.add_argument("--max-tool-rounds", type=int, default=3,
                        help="最多 tool calling 轮数（含最终给 SQL 那轮），默认 3")
    parser.add_argument("--no-agent", action="store_true",
                        help="禁用 LangGraph 自修复 Agent，回退 Day 4 单次 tool calling（对比用）")
    parser.add_argument("--max-repair-attempts", type=int, default=3,
                        help="Agent 自修复最大重试次数（不含首次），默认 3（Day 7-8 起从 2 升到 3）")
    args = parser.parse_args()

    require_api_key()

    if not _smoke_check():
        sys.exit(1)

    use_rag = not args.no_rag
    use_tools = not args.no_tools
    use_agent = not args.no_agent

    if args.question:
        ask(
            " ".join(args.question),
            stream=args.stream,
            use_rag=use_rag,
            use_tools=use_tools,
            use_agent=use_agent,
            top_k=args.top_k,
            max_tool_rounds=args.max_tool_rounds,
            max_repair_attempts=args.max_repair_attempts,
        )
        return

    mode = "流式" if args.stream else "非流式"
    if not use_rag:
        rag_mode = "全 schema（无 RAG）"
    elif use_agent and use_tools:
        rag_mode = f"Agent + RAG top-{args.top_k} + Tools"
    elif use_tools:
        rag_mode = f"RAG top-{args.top_k} + Tools（无 Agent）"
    else:
        rag_mode = f"RAG top-{args.top_k}（无 tools）"
    print(f"\n=== Day 6 Demo（{mode} / {rag_mode}）：自然语言查 SAP ===")
    print("默认会跑 4 个示例问题。要自己提问：python -m src.main [选项] \"你的问题\"\n")
    for q in DEFAULT_QUESTIONS:
        ask(
            q,
            stream=args.stream,
            use_rag=use_rag,
            use_tools=use_tools,
            use_agent=use_agent,
            top_k=args.top_k,
            max_tool_rounds=args.max_tool_rounds,
            max_repair_attempts=args.max_repair_attempts,
        )
        print("\n" + "=" * 60)


if __name__ == "__main__":
    main()
