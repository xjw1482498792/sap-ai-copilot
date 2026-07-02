"""确定性验证：自修复 prompt 里"透传上一轮工具调用结果"的作用。

不依赖 LLM 是否真的自修复（那是非确定性的），而是直接构造一段
"首轮调过 schema_lookup 但 SQL 写错失败"的 history，喂给 build_repair_messages，
对比【带 tool_calls 记录】和【不带】两种情况下修复 prompt 的差异。
"""
import io
import sys

# 让控制台中文不乱码
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from src.prompts import build_repair_messages

# 模拟首轮：LLM 调了 schema_lookup(['MAKT']) 补到字段，但 SQL 用错了列名导致执行失败
MAKT_SCHEMA_RESULT = (
    "MAKT（物料描述文本表）字段：\n"
    "  MATNR 物料号 | SPRAS 语言键(1=中文) | MAKTX 物料中文名"
)

failed_attempt = {
    "attempt": 0,
    "sql": "SELECT MATNR, MAKTX_CN FROM MAKT WHERE SPRAS='1'",  # 错列名 MAKTX_CN
    "ok": False,
    "error": "no such column: MAKTX_CN",
    "row_count": 0,
    "tool_calls": [
        {
            "name": "schema_lookup",
            "arguments": '{"table_names": ["MAKT"]}',
            "result": MAKT_SCHEMA_RESULT,
            "result_preview": MAKT_SCHEMA_RESULT[:200],
        }
    ],
}

# 同一条失败记录，去掉 tool_calls（= 改动前的旧行为）
failed_no_record = {k: v for k, v in failed_attempt.items() if k != "tool_calls"}

common = dict(
    user_question="销售数量最高的 5 个物料，列出物料编号和中文名称",
    today="2026-06-14",
    schema_text="（此处省略 RAG 召回的其它表 schema）",
    with_tool_catalog=True,
)


def show(title, history):
    msgs = build_repair_messages(history=history, **common)
    user_msg = msgs[1]["content"]
    print("=" * 70)
    print(title)
    print("=" * 70)
    # 只打印 user message 里的"历史"段落，省掉冗长的 system
    print(user_msg)
    print()


show("【带 tool_calls 记录】修复轮 prompt（当前保留的增强）", [failed_attempt])
show("【不带 tool_calls 记录】修复轮 prompt（改动前的旧行为）", [failed_no_record])
