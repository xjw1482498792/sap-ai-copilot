r"""
Schema RAG 召回过程的诊断 / 断点锚点脚本。

只跑"问题 → 向量 → Chroma → top-K 表 → 拼好的 schema 文本"这一段，
不调 LLM、不跑 SQL，方便在 src/retriever.py 里设断点逐行看。

跑法：
    .\.venv\Scripts\python.exe -m scripts.debug_retrieval
    .\.venv\Scripts\python.exe -m scripts.debug_retrieval "上个月销售额前 5 的客户"
    .\.venv\Scripts\python.exe -m scripts.debug_retrieval --top-k 3 "..."

VSCode 调试：选 launch.json 里的 "调试：只看 Schema RAG 召回（不调 LLM）"，
F5 后会弹框输入问题，断点建议打在：
    src/retriever.py:107  q_emb = ...encode(...)              问题向量化
    src/retriever.py:108  res = coll.query(...)               Chroma 近邻搜索
    src/retriever.py:111  return [(name, 1-d) ...]            距离转相似度
    src/retriever.py:120  hits = retrieve(...)                retrieve_schema_text 入口
    src/retriever.py:126  return "\n\n".join(parts), hits    拼好的 prompt 出口
"""
from __future__ import annotations

import argparse
import sys
import time

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

from src.retriever import (
    retrieve,
    retrieve_schema_text,
    collection_size,
    EMBED_MODEL_NAME,
)


DEFAULT_QUERY = "今年销售额前 5 的客户名称和金额"


def main() -> None:
    parser = argparse.ArgumentParser(description="只看 Schema RAG 召回过程")
    parser.add_argument("question", nargs="*", help="中文业务问题（省略则用默认）")
    parser.add_argument("--top-k", type=int, default=5, help="召回表数量（默认 5）")
    args = parser.parse_args()
    query = " ".join(args.question) or DEFAULT_QUERY

    n = collection_size()
    if n == 0:
        print("[!] Chroma 集合为空，先跑：python -m scripts.build_index")
        sys.exit(1)
    print(f"集合就绪：{n} 张表已索引（模型 {EMBED_MODEL_NAME}）")
    print(f"问题：{query}\n")

    # ===== 第 1 阶段：retrieve() —— 返回 [(表名, 相似度), ...] =====
    print("─" * 60)
    print("阶段 1：retrieve()  —— 问题向量化 + Chroma 近邻搜索")
    print("─" * 60)
    t0 = time.time()
    hits = retrieve(query, top_k=args.top_k)            # ← 想看内部就 step into 这一行
    print(f"耗时：{int((time.time()-t0)*1000)} ms\n")
    print(f"  {'排名':<4}{'表名':<8}{'模块':<6}{'相似度':<8}")
    for i, (name, score) in enumerate(hits, 1):
        # 顺手补一下模块信息，让你看到召回的业务分布
        from src.schemas import get_table
        t = get_table(name)
        module = t["module"] if t else "?"
        print(f"  {i:<4}{name:<8}{module:<6}{score:<8.4f}")

    # ===== 第 2 阶段：retrieve_schema_text() —— 把召回结果拼成 prompt 文本 =====
    print("\n" + "─" * 60)
    print("阶段 2：retrieve_schema_text()  —— 拼成给 LLM 的 schema 段落")
    print("─" * 60)
    t0 = time.time()
    schema_text, hits2 = retrieve_schema_text(query, top_k=args.top_k)
    print(f"耗时（含 retrieve）：{int((time.time()-t0)*1000)} ms")
    print(f"字符数：{len(schema_text)}（≈ {len(schema_text)//2}-{len(schema_text)} tokens 区间）\n")

    # 完整打出来，让你看清楚最终送进 system prompt 的就是这段
    print("==== 最终塞进 system prompt 的 schema 段落 ====")
    print(schema_text)
    print("==== 段落结束 ====")

    print("\n[OK] 召回链路跑完。要看每一步的中间变量，在 src/retriever.py 里设断点后用")
    print("     'launch.json → 调试：只看 Schema RAG 召回（不调 LLM）' 跑。")


if __name__ == "__main__":
    main()
