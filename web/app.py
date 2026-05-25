"""
Streamlit Web UI（Day 12）。

把 CLI 端的完整链路（RAG 召回 → LangGraph Agent → SQL 执行 → 业务解读）
搬到浏览器上演示，重点是把 Day 6 自修复 Agent 的中间过程肉眼可见地展示出来：
  - 召回了哪些表、各自的分数
  - Tool calling 调了 schema_lookup 哪几张表
  - Agent 跑了几次 attempt、每次的 SQL 和 ok/error
  - 最终 SQL + 结果表格 + 流式业务解读

启动：
  cd 项目根目录
  .venv\\Scripts\\streamlit.exe run web/app.py
或：
  .venv\\Scripts\\python.exe -m streamlit run web/app.py
"""
from __future__ import annotations

import sys
import time
from datetime import date
from pathlib import Path

import streamlit as st

# 保证 `from src.xxx import ...` 在 `streamlit run web/app.py` 时也能跑通
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src.config import require_api_key, DEEPSEEK_MODEL, APP_PASSWORD  # noqa: E402
from src.db import run_sql, table_row_counts  # noqa: E402
from src.llm import chat_stream_iter  # noqa: E402
from src.log import log_ask  # noqa: E402
from src.main import generate_sql_with_tools  # noqa: E402  (Day 4 直链备用)
from src.prompts import build_sql_messages, build_explain_messages  # noqa: E402
from src.retriever import retrieve_schema_text  # noqa: E402


PAGE_TITLE = "SAP 智能查询助手"
EXAMPLE_QUESTIONS = [
    "目前数据库里一共有多少个客户？",
    "查一下 2025 年销售订单总数和总金额",
    "销售额前 5 的客户是谁，分别销售了多少钱？",
    "今年采购订单金额最高的 3 个供应商",
    "销售数量最高的 5 个物料，列出物料编号、中文名称和销售总数量",
    "对比 2025 年销售订单总额和采购订单总额，哪一边更高",
]


# -------------------- Streamlit 缓存层 --------------------

@st.cache_resource(show_spinner="预热数据库连接...")
def _cached_db_counts() -> dict[str, int]:
    """表行数缓存。冷启动只查一次，后续 rerun 直接读缓存。"""
    return table_row_counts()


@st.cache_resource(show_spinner="加载 BGE 中文向量模型...")
def _warm_retriever() -> bool:
    """提前把 BGE 模型 + Chroma 客户端拉起来。
    没有这一步首次提问会卡 25 秒（加载模型）—— Web 端这种延迟体验很糟。"""
    from src.retriever import _get_model, _get_client  # noqa: F401
    _get_model()
    _get_client()
    return True


# -------------------- Sidebar：配置 --------------------

def _render_sidebar() -> dict:
    """绘制侧边栏，返回当前配置 dict。"""
    st.sidebar.title("⚙️ 模式与参数")

    st.sidebar.subheader("能力开关")
    use_rag = st.sidebar.checkbox(
        "Schema RAG（Day 3）",
        value=st.session_state.get("use_rag", True),
        help="开：BGE 向量召回 top-K 相关表；关：把全量 10 张表 schema 都塞 prompt",
    )
    use_tools = st.sidebar.checkbox(
        "Function Calling（Day 4）",
        value=st.session_state.get("use_tools", True),
        disabled=not use_rag,
        help="开：LLM 可主动调 schema_lookup 补字段。--no-rag 时强制关",
    )
    use_agent = st.sidebar.checkbox(
        "LangGraph 自修复 Agent（Day 6）",
        value=st.session_state.get("use_agent", True),
        disabled=not use_rag,
        help="开：SQL 执行报错时 reflect → 重新生成。关：单次生成，错就错",
    )

    st.sidebar.subheader("参数")
    top_k = st.sidebar.slider("RAG top-K", 1, 10, st.session_state.get("top_k", 5),
                              disabled=not use_rag)
    max_tool_rounds = st.sidebar.slider(
        "tool calling 最大轮数", 1, 6,
        st.session_state.get("max_tool_rounds", 3),
        disabled=not (use_tools and use_rag),
    )
    max_repair_attempts = st.sidebar.slider(
        "Agent 最大自修复次数", 0, 4,
        st.session_state.get("max_repair_attempts", 2),
        disabled=not (use_agent and use_rag),
        help="不含首次。0 = Agent 仅做执行 wrapper，不重试",
    )

    cfg = {
        "use_rag": use_rag,
        "use_tools": use_tools and use_rag,
        "use_agent": use_agent and use_rag,
        "top_k": top_k,
        "max_tool_rounds": max_tool_rounds,
        "max_repair_attempts": max_repair_attempts,
    }
    # 同步到 session，让下次 rerun 保留
    for k, v in cfg.items():
        st.session_state[k] = v

    st.sidebar.divider()
    st.sidebar.subheader("数据库")
    counts = _cached_db_counts()
    empty = [t for t, c in counts.items() if c <= 0]
    if empty:
        st.sidebar.error(f"以下表为空：{empty}\n请先跑 `python -m data.seed_data`")
    else:
        total = sum(counts.values())
        st.sidebar.success(f"就绪：{len(counts)} 张表，共 {total:,} 行")
        with st.sidebar.expander("表行数明细"):
            for t, c in counts.items():
                st.write(f"- `{t}`: {c:,}")

    st.sidebar.divider()
    st.sidebar.caption(f"模型：`{DEEPSEEK_MODEL}`")

    if st.sidebar.button("🗑️ 清空对话历史", use_container_width=True):
        st.session_state.messages = []
        st.rerun()

    return cfg


# -------------------- 渲染层：单条 assistant 消息 --------------------

def _badge(label: str, color: str = "blue") -> str:
    return f":{color}-background[{label}]"


def _mode_caption(cfg: dict) -> str:
    parts = []
    if cfg["use_rag"]:
        parts.append(f"RAG top-{cfg['top_k']}")
    else:
        parts.append("全 schema")
    if cfg["use_tools"]:
        parts.append("Tools")
    if cfg["use_agent"]:
        parts.append(f"Agent(max_repair={cfg['max_repair_attempts']})")
    return " + ".join(parts)


def _render_rag_hits(hits: list[tuple[str, float]], latency_ms: int) -> None:
    cols = st.columns(len(hits))
    for col, (name, score) in zip(cols, hits):
        col.metric(label=name, value=f"{score:.3f}")
    st.caption(f"召回耗时 {latency_ms} ms")


def _render_tool_calls(tool_calls: list[dict]) -> None:
    if not tool_calls:
        st.caption("（本次未调用任何 tool）")
        return
    for i, tc in enumerate(tool_calls, 1):
        st.markdown(f"**#{i}** `{tc['name']}` &nbsp; 参数：`{tc.get('arguments', '')}`")


def _render_agent_history(history: list[dict]) -> None:
    if not history:
        return
    for h in history:
        attempt_no = h["attempt"] + 1
        if h["ok"]:
            st.markdown(f"- ✅ **第 {attempt_no} 次尝试** — 执行成功，返回 {h['row_count']} 行")
        else:
            err = (h.get("error") or "")[:200]
            st.markdown(f"- ❌ **第 {attempt_no} 次尝试** — 执行失败：`{err}`")
        with st.expander(f"查看第 {attempt_no} 次的 SQL", expanded=False):
            st.code(h.get("sql", ""), language="sql")


def _render_result_table(result: dict) -> None:
    if not result["ok"]:
        st.error(f"SQL 执行失败：{result.get('error', '')}")
        return
    rows = result.get("rows", [])
    if not rows:
        st.info("查询返回 0 行。")
        return
    st.dataframe(rows, use_container_width=True, hide_index=True)
    st.caption(f"共 {result.get('row_count', len(rows))} 行（仅展示前 1000 行）")


# -------------------- 核心处理 --------------------

def _process_question(question: str, cfg: dict) -> dict:
    """跑一次完整链路，把过程和结果 dict 化返回 —— 便于历史回放。

    返回结构：
      {
        "question": str,
        "rag": {"enabled": bool, "hits": [(name, score)], "latency_ms": int} | None,
        "agent": {"enabled", "attempts", "repair_used", "history": [...], "tool_calls": [...]},
        "sql": str,
        "exec_result": {"ok", "rows", "row_count", "error"},
        "explain": str,                # 完整业务解读（流式吐完后存）
        "gen_meta": {                  # token/延迟元信息
          "input_tokens", "output_tokens", "latency_ms", "rounds"
        },
        "explain_meta": {"input_tokens", "output_tokens", "latency_ms"} | None,
        "mode": str,
      }
    """
    today = date.today().isoformat()
    out = {
        "question": question,
        "rag": None,
        "agent": None,
        "sql": "",
        "exec_result": {"ok": False, "rows": [], "row_count": 0, "error": ""},
        "explain": "",
        "gen_meta": {},
        "explain_meta": None,
        "mode": _mode_caption(cfg),
    }
    t_total = time.time()

    # === 1) RAG 召回 ===
    schema_text = None
    hits: list[tuple[str, float]] = []
    if cfg["use_rag"]:
        with st.status("🔍 Schema RAG 召回中…", expanded=True) as status:
            t_rag = time.time()
            schema_text, hits = retrieve_schema_text(question, top_k=cfg["top_k"])
            rag_ms = int((time.time() - t_rag) * 1000)
            _render_rag_hits(hits, rag_ms)
            status.update(label=f"🔍 Schema RAG 召回 {len(hits)} 张表（{rag_ms} ms）",
                          state="complete", expanded=False)
        out["rag"] = {"enabled": True, "hits": hits, "latency_ms": rag_ms}

    # === 2) SQL 生成（Agent / 直链） ===
    if cfg["use_agent"] and cfg["use_rag"]:
        with st.status("🤖 LangGraph Agent 生成 SQL（含自修复）…", expanded=True) as status:
            from src.agent import run_agent
            t_gen = time.time()
            agent_out = run_agent(
                question=question,
                today=today,
                schema_text=schema_text,
                use_tools=cfg["use_tools"],
                max_tool_rounds=cfg["max_tool_rounds"],
                max_repair_attempts=cfg["max_repair_attempts"],
            )
            gen_ms = int((time.time() - t_gen) * 1000)

            # 展示 attempts
            attempts = agent_out["attempts"]
            if agent_out["repair_used"]:
                st.warning(f"触发了 SQL 自修复：共尝试 {attempts} 次")
            else:
                st.success(f"首次生成即通过（未触发自修复）")
            _render_agent_history(agent_out["history"])

            # 展示 tool calls
            st.markdown("**🔧 Tool calls**")
            _render_tool_calls(agent_out["tool_calls"])

            status.update(
                label=f"🤖 Agent 完成（{attempts} 次尝试 / {gen_ms} ms / "
                      f"tokens in={agent_out['total_input_tokens']} "
                      f"out={agent_out['total_output_tokens']}）",
                state="complete",
                expanded=False,
            )

        out["sql"] = agent_out["sql"]
        out["exec_result"] = agent_out["exec_result"]
        out["agent"] = {
            "enabled": True,
            "attempts": attempts,
            "repair_used": agent_out["repair_used"],
            "history": agent_out["history"],
            "tool_calls": agent_out["tool_calls"],
        }
        out["gen_meta"] = {
            "input_tokens": agent_out["total_input_tokens"],
            "output_tokens": agent_out["total_output_tokens"],
            "latency_ms": agent_out["total_latency_ms"],
            "rounds": agent_out["rounds"],
        }
    else:
        with st.status("📝 生成 SQL（Day 4 直链 / 无 Agent 自修复）…", expanded=True) as status:
            messages = build_sql_messages(
                question, today,
                schema_text=schema_text,
                with_tool_catalog=cfg["use_tools"],
            )
            gen = generate_sql_with_tools(
                messages,
                use_tools=cfg["use_tools"],
                max_rounds=cfg["max_tool_rounds"],
                tag="web_sql_gen",
            )
            result = run_sql(gen["sql"])
            st.markdown("**🔧 Tool calls**")
            _render_tool_calls(gen["tool_calls"])
            status.update(
                label=f"📝 SQL 已生成（{gen['rounds']} 次 LLM 调用 / "
                      f"{gen['total_latency_ms']} ms）",
                state="complete",
                expanded=False,
            )
        out["sql"] = gen["sql"]
        out["exec_result"] = result
        out["agent"] = {"enabled": False, "attempts": 1, "repair_used": False,
                        "history": [], "tool_calls": gen["tool_calls"]}
        out["gen_meta"] = {
            "input_tokens": gen["total_input_tokens"],
            "output_tokens": gen["total_output_tokens"],
            "latency_ms": gen["total_latency_ms"],
            "rounds": gen["rounds"],
        }

    # === 3) 最终 SQL ===
    st.markdown("##### 📜 最终 SQL")
    st.code(out["sql"] or "(空)", language="sql")
    gm = out["gen_meta"]
    st.caption(
        f"LLM 调用 {gm['rounds']} 次 · 耗时 {gm['latency_ms']} ms · "
        f"tokens in={gm['input_tokens']} out={gm['output_tokens']}"
    )

    # === 4) 结果表 ===
    st.markdown("##### 📊 查询结果")
    _render_result_table(out["exec_result"])

    # === 5) 业务解读（流式） ===
    if out["exec_result"]["ok"] and out["exec_result"].get("rows"):
        st.markdown("##### 💬 业务解读")
        # 把结果文本化（最多前 20 行），喂给解读 prompt
        from tabulate import tabulate
        result_text = tabulate(
            out["exec_result"]["rows"][:20],
            headers="keys", tablefmt="simple", floatfmt=".2f",
        )
        explain_messages = build_explain_messages(question, out["sql"], result_text)
        meta_holder: dict = {}
        # st.write_stream 直接消费 yield 出来的字符串 chunk，按 chunk 节奏渲染，
        # 不需要 sleep（Web 端体验对 chunk 速度本来就敏感，sleep 反而拖慢）
        full_text = st.write_stream(
            chat_stream_iter(
                explain_messages,
                tag="web_explain",
                on_done=lambda d: meta_holder.update(d),
            )
        )
        out["explain"] = full_text if isinstance(full_text, str) else "".join(full_text)
        if meta_holder:
            out["explain_meta"] = {
                "input_tokens": meta_holder["usage"]["prompt_tokens"],
                "output_tokens": meta_holder["usage"]["completion_tokens"],
                "latency_ms": meta_holder["latency_ms"],
            }
            st.caption(
                f"流式 {out['explain_meta']['latency_ms']} ms · "
                f"tokens in={out['explain_meta']['input_tokens']} "
                f"out={out['explain_meta']['output_tokens']}"
            )

    # === 日志 ===
    log_ask(
        question=question,
        success=out["exec_result"]["ok"],
        total_latency_ms=int((time.time() - t_total) * 1000),
        tag="web",
    )
    return out


def _replay_assistant_message(msg: dict) -> None:
    """从 session_state 里把一条历史 assistant 消息重新渲染出来（不重新调 LLM）。"""
    st.caption(f"模式：{msg['mode']}")

    if msg.get("rag"):
        with st.expander(f"🔍 Schema RAG 召回 {len(msg['rag']['hits'])} 张表 "
                         f"（{msg['rag']['latency_ms']} ms）", expanded=False):
            _render_rag_hits(msg["rag"]["hits"], msg["rag"]["latency_ms"])

    agent = msg.get("agent") or {}
    if agent.get("enabled"):
        label = (f"🤖 Agent {agent['attempts']} 次尝试"
                 f"{'（触发自修复）' if agent.get('repair_used') else ''}")
    else:
        label = "📝 直链生成（无 Agent）"
    with st.expander(label, expanded=False):
        _render_agent_history(agent.get("history", []))
        st.markdown("**Tool calls**")
        _render_tool_calls(agent.get("tool_calls", []))

    st.markdown("##### 📜 最终 SQL")
    st.code(msg.get("sql") or "(空)", language="sql")
    gm = msg.get("gen_meta") or {}
    if gm:
        st.caption(
            f"LLM 调用 {gm.get('rounds', '?')} 次 · 耗时 {gm.get('latency_ms', '?')} ms · "
            f"tokens in={gm.get('input_tokens', '?')} out={gm.get('output_tokens', '?')}"
        )

    st.markdown("##### 📊 查询结果")
    _render_result_table(msg.get("exec_result", {"ok": False, "rows": [], "error": ""}))

    if msg.get("explain"):
        st.markdown("##### 💬 业务解读")
        st.markdown(msg["explain"])
        em = msg.get("explain_meta")
        if em:
            st.caption(
                f"流式 {em['latency_ms']} ms · "
                f"tokens in={em['input_tokens']} out={em['output_tokens']}"
            )


# -------------------- 密码门（Day 13-14 公网部署用） --------------------

def _check_password() -> bool:
    """APP_PASSWORD 为空时不启用密码门，直接放行；
    非空时前置一个密码输入页，校验通过后写 session_state 持久化到刷新前。

    用 st.stop() 控制流，避免主页面在未授权时被部分渲染。
    """
    if not APP_PASSWORD:
        return True
    if st.session_state.get("authed"):
        return True

    st.title("🔒 SAP 智能查询助手")
    st.caption("演示项目 · 输入访问密码继续")
    pwd = st.text_input("访问密码", type="password", key="_pwd_input")
    if st.button("进入", type="primary"):
        if pwd == APP_PASSWORD:
            st.session_state.authed = True
            st.rerun()
        else:
            st.error("密码错误")
    return False


# -------------------- main --------------------

def main() -> None:
    st.set_page_config(page_title=PAGE_TITLE, page_icon="📊", layout="wide")

    if not _check_password():
        st.stop()

    # 顶部
    st.title(f"📊 {PAGE_TITLE}")
    st.caption(
        "自然语言问 SAP 业务数据 · Schema RAG + LangGraph 自修复 Agent · "
        f"DeepSeek 驱动"
    )

    # 启动前置检查
    try:
        require_api_key()
    except Exception as e:
        st.error(f"❌ 缺少 DeepSeek API Key：{e}\n请检查 `.env` 里的 `DEEPSEEK_API_KEY`")
        st.stop()

    # 预热模型（首次需要 25 s 左右）
    _warm_retriever()

    # 侧边栏配置
    cfg = _render_sidebar()

    # session 状态
    if "messages" not in st.session_state:
        st.session_state.messages = []
    if "pending_question" not in st.session_state:
        st.session_state.pending_question = None

    # 空对话时显示示例
    if not st.session_state.messages and st.session_state.pending_question is None:
        st.markdown("#### 💡 试试这些问题")
        cols = st.columns(2)
        for i, q in enumerate(EXAMPLE_QUESTIONS):
            if cols[i % 2].button(q, key=f"ex_{i}", use_container_width=True):
                st.session_state.pending_question = q
                st.rerun()

    # 渲染历史
    for msg in st.session_state.messages:
        with st.chat_message(msg["role"]):
            if msg["role"] == "user":
                st.markdown(msg["content"])
            else:
                _replay_assistant_message(msg)

    # 接收新输入（chat_input 自带提交后清空 + 回车提交）
    user_input = st.chat_input("用中文问点啥，例如：销售额前 5 的客户是谁")

    question = user_input or st.session_state.pending_question
    if question:
        st.session_state.pending_question = None
        st.session_state.messages.append({"role": "user", "content": question})

        with st.chat_message("user"):
            st.markdown(question)

        with st.chat_message("assistant"):
            try:
                msg = _process_question(question, cfg)
            except Exception as e:
                st.exception(e)
                msg = {
                    "question": question, "mode": _mode_caption(cfg),
                    "sql": "", "exec_result": {"ok": False, "rows": [], "error": str(e)},
                    "rag": None, "agent": None, "explain": "",
                    "gen_meta": {}, "explain_meta": None,
                }
            msg["role"] = "assistant"
            st.session_state.messages.append(msg)


if __name__ == "__main__":
    main()
