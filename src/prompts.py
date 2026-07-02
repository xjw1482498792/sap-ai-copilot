"""
SQL 生成 Prompt。

演化路线：
  - Day 1-2: 全 schema 塞进 system message（all_schemas_prompt）
  - Day 3:   RAG 召回 top-K 相关表的 schema
  - Day 4:   在 RAG 召回基础上新增"全表目录"（仅表名+一句话）+ schema_lookup
             tool，让 LLM 在召回不全时主动补充未召回表的完整字段
  - Day 6:   build_repair_messages —— 当 SQL 执行报错时，把"问题 + 历次失败 SQL +
             错误信息"重新组装一份 prompt 给 LLM 修，配合 src/agent.py 的 LangGraph
             自修复循环用（本文件当前形态）

build_sql_messages() 兼容三种模式：
  - schema_text=None  →  退回到全 schema（Day 1/2 兼容）
  - schema_text 非空 + with_tool_catalog=False  →  Day 3 纯 RAG
  - schema_text 非空 + with_tool_catalog=True   →  Day 4，附带全表目录 + tool 指引
"""
import json
from typing import Optional

from src.schemas import SCHEMAS, all_schemas_prompt


SQL_GEN_SYSTEM_PROMPT = """你是 SAP 业务数据查询助手。用户用中文提问，你需要：
1. 理解用户的业务意图
2. 基于下面给出的 SAP 表结构，生成一条 SQLite 兼容的 SQL 查询
3. **只返回纯 SQL，不要 markdown 围栏、不要解释、不要二次自检改写**

可用 SAP 表（已按相关度召回，含字段详情）：
{schema_text}
{catalog_section}{tool_section}
注意事项：
- 使用 SQLite 语法（不是 ABAP OpenSQL，不能用 SELECT SINGLE、FOR ALL ENTRIES 等）
- 字符串字面值用单引号，如 'CNY'
- 日期格式 'YYYY-MM-DD'
- 表名、字段名都用大写，保持和 SAP 一致
- 涉及"上个月""今年""2025 年"等时间条件时，**必须**在 WHERE 子句里加日期范围过滤，
  基于今天的日期 {today} 计算；不要省略时间过滤
- 物料的可读名称要 JOIN MAKT 表（SPRAS='1' 表示中文描述）
- 客户名称在 KNA1.NAME1，物料描述在 MAKT.MAKTX
- 财务凭证按年份/季度/月份过滤时，业务标准做法是 JOIN BKPF 用过账日期 BUDAT
  过滤，**不要**只用 BSEG.GJAHR（GJAHR 只够粗判，业务报表口径要 BUDAT）
- 金额相关查询默认按 NETWR/DMBTR 这类净额字段
- 如果用户问题不清楚或缺关键信息，仍然给出最合理的 SQL，不要反问
- 优先使用上面"已按相关度召回"列出的表；如果你判断需要其它表（典型场景：
  查物料中文名要 JOIN MAKT），请先调用 schema_lookup 拉取那张表的字段再生成 SQL
"""


CATALOG_HEADER = "\n以下是数据库里全部可用表的目录（仅表名+一句话，字段未列出）：\n"

TOOL_HINT_SECTION = """
你可以调用工具 schema_lookup(table_names=[...]) 来补充任何"目录里有但召回列表里没有"
的表的完整字段。仅在确实需要某张未召回表时调用，不要为了"以防万一"频繁调。
拿到 schema_lookup 返回结果后，**下一条回复必须只输出最终 SQL 本身**，
不要写"现在可以生成 SQL 了""根据查询结果"之类的开场白或任何中文说明。
"""


def all_tables_catalog() -> str:
    """生成全部表的精简目录：每张表一行，仅表名 + 模块 + 一句话功能。

    设计目标：花极少的 token（10 张表大约 200-300 chars 中文 ≈ 100-150 input
    tokens）让 LLM 知道"数据库里还有哪些表可以 lookup"，否则 LLM 不知道 MAKT
    存在就不会主动去查。
    """
    lines = []
    for t in SCHEMAS:
        # 一句话描述去掉括号里的英文别名，更紧凑
        desc = t["desc"].split("（")[0].split("(")[0].strip()
        # desc 里如果有句号也只保留第一句
        desc = desc.split("。")[0]
        lines.append(f"- {t['name']} ({t['module']}): {desc}")
    return "\n".join(lines)


def build_sql_messages(
    user_question: str,
    today: str,
    schema_text: Optional[str] = None,
    with_tool_catalog: bool = False,
) -> list[dict]:
    """构造 SQL 生成的 prompt 消息列表。

    schema_text:
      - None  → 用 all_schemas_prompt()，全量 10 张表（Day 1/2 兼容模式）
      - 非空  → 直接当作 schema 段落填进 system prompt（Day 3+ RAG 模式）

    with_tool_catalog:
      - False → 不附加全表目录和 tool 指引（Day 3 行为）
      - True  → 附加目录 + tool 指引（Day 4 行为，配合 chat(tools=ALL_TOOLS) 使用）
    """
    if with_tool_catalog:
        catalog_section = CATALOG_HEADER + all_tables_catalog() + "\n"
        tool_section = TOOL_HINT_SECTION
    else:
        catalog_section = ""
        tool_section = ""

    system = SQL_GEN_SYSTEM_PROMPT.format(
        schema_text=schema_text if schema_text is not None else all_schemas_prompt(),
        catalog_section=catalog_section,
        tool_section=tool_section,
        today=today,
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user_question},
    ]


REPAIR_INSTRUCTION = """
上面是你之前生成 SQL 的失败历史。请基于错误信息修正 SQL。

常见错误的诊断方向：
- "no such column: X.Y"  —— X 表里没有 Y 字段。**优先调 schema_lookup(["X"]) 核实真实字段**，
  不要凭印象写。例：EKKO 没有 NETWR，采购金额在 EKPO.NETPR
- "no such table: X"      —— 表名拼错或不存在，用大写 SAP 标准名
- "ambiguous column name" —— JOIN 后同名字段要加表别名前缀
- "syntax error"          —— 检查 SQLite 语法，不能用 ABAP OpenSQL
- "incomplete input"      —— SQL 不完整或被截断，请输出**完整一条 SQL**，
  不要用 ; 分多条语句，避免在 SQL 之外写任何中文解释
- 结果为空但语义对的时候    —— 检查日期/币种/客户编号等过滤条件是否过严

修复要求：
- 只输出最终修正后的 SQL，**不要解释，不要 markdown 围栏**
- 如果不确定字段名，**先调 schema_lookup** 再写 SQL，不要凭猜测
- 不要重复之前已经失败的写法
"""


REPEAT_ERROR_WARNING = """
⚠️ 重复错误警告：你已经连续 {repeat_count} 次遇到了同样的 SQLite 错误
"{error_signature}"。
继续重复同一思路只会再次失败。本轮**必须**：
1. 换一个根本不同的写法（如：把 CTE 拆掉、把窗口函数换成子查询自连接、改 JOIN 方向）
2. 如果是 "no such column" / "no such table" 类，**先调 schema_lookup** 核字段，再写 SQL
3. 如果是 "incomplete input"，确保整段 SQL 写完整 + 没有截断 + 不用 ; 分多条
"""


def _format_repeat_warning(repeat_count: int, error_signature: str) -> str:
    if repeat_count <= 0:
        return ""
    return REPEAT_ERROR_WARNING.format(
        repeat_count=repeat_count + 1,  # +1 是因为本次也算
        error_signature=error_signature,
    )


def _format_repair_history(history: list[dict]) -> str:
    """把历次失败尝试拼成一段可读文本贴进 user message。

    每条记录包含：本轮调用的 tool 结果（如有）、生成的 SQL、执行结果/错误。
    tool 结果完整透传，让修复轮无需重复查表。
    """
    if not history:
        return ""
    lines = []
    for h in history:
        attempt = h.get("attempt", 0)
        sql = (h.get("sql") or "").strip()
        err = (h.get("error") or "").strip()
        row_count = h.get("row_count", 0)
        ok = h.get("ok", False)
        tool_calls = h.get("tool_calls", [])

        lines.append(f"--- 第 {attempt + 1} 次尝试 ---")

        if tool_calls:
            lines.append("本轮工具调用：")
            for tc in tool_calls:
                try:
                    args = json.loads(tc["arguments"]) if tc["arguments"] else {}
                except (json.JSONDecodeError, TypeError):
                    args = tc["arguments"]
                result = tc.get("result") or tc.get("result_preview", "")
                lines.append(f"  {tc['name']}({args}) 返回：")
                lines.append(f"  {result}")

        lines.append(f"SQL：\n{sql}")
        if ok:
            lines.append(f"执行结果：成功但返回 {row_count} 行（疑似过滤过严或语义偏差）")
        else:
            lines.append(f"执行错误：{err}")
    return "\n".join(lines)


def build_repair_messages(
    user_question: str,
    today: str,
    schema_text: Optional[str],
    with_tool_catalog: bool,
    history: list[dict],
    repeat_count: int = 0,
    error_signature: str = "",
) -> list[dict]:
    """构造带错误反馈的 SQL 修复 prompt（Day 6 自修复 Agent 用）。

    设计要点：
      1. system prompt 和首次生成同款（带 schema + catalog + tool 指引），保持 LLM
         对业务规则的认知一致；只在 user 侧追加失败上下文，避免 system 越改越长
      2. 把"所有"历次失败尝试都拼进来，让 LLM 看到完整轨迹（而不仅最近一次），
         否则可能反复犯同样的错
      3. 修复指令 REPAIR_INSTRUCTION 显式列出常见诊断方向，把"列名幻觉→先调
         schema_lookup"这一步说死，否则 LLM 倾向于直接重写另一种幻觉
      4. Day 7-8 新增：reflect 节点检测到连续相同 SQLite error 时，传入
         repeat_count > 0，在 user message 顶部加 REPEAT_ERROR_WARNING，
         强迫 LLM 跳出"反复犯同样错"的循环
    """
    system = SQL_GEN_SYSTEM_PROMPT.format(
        schema_text=schema_text if schema_text is not None else all_schemas_prompt(),
        catalog_section=(
            CATALOG_HEADER + all_tables_catalog() + "\n" if with_tool_catalog else ""
        ),
        tool_section=TOOL_HINT_SECTION if with_tool_catalog else "",
        today=today,
    )

    repeat_warning = _format_repeat_warning(repeat_count, error_signature)
    user_content = (
        f"原问题：{user_question}\n\n"
        f"{_format_repair_history(history)}\n"
        f"{repeat_warning}"
        f"{REPAIR_INSTRUCTION}"
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user_content},
    ]


EXPLAIN_RESULT_PROMPT = """你是 SAP 业务数据分析师。用户问了一个问题，系统执行 SQL 后得到结果。
请用 2-3 句中文，从业务视角解读这个结果，简洁直接，不要复述数字明细。

用户问题：{question}
执行的 SQL：{sql}
查询结果（最多前 20 行）：
{result}
"""


def build_explain_messages(question: str, sql: str, result_text: str) -> list[dict]:
    return [
        {
            "role": "user",
            "content": EXPLAIN_RESULT_PROMPT.format(
                question=question, sql=sql, result=result_text
            ),
        }
    ]
