r"""
构建 Schema RAG 索引：把 10 张 SAP 表向量化后写入 Chroma。

跑法：
    .\.venv\Scripts\python.exe -m scripts.build_index

首次运行会下载 BGE-small-zh-v1.5（~95 MB，HuggingFace），后续从本地缓存读。
索引持久化在 data/chroma_db/，重跑会清空重建。

输出末尾会跑几个抽样问题，肉眼校验召回质量。
"""
from __future__ import annotations

import sys
import time

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

from src.retriever import (
    build_index,
    retrieve,
    EMBED_MODEL_NAME,
    CHROMA_DIR,
    COLLECTION_NAME,
)


SAMPLE_QUERIES = [
    "目前数据库里一共有多少个客户？",
    "今年（2026）每个月的销售订单数量",
    "采购金额最高的 3 个供应商",
    "2025 年公司代码 1000 的财务凭证里，借方总金额",
    "销售数量最高的 5 个物料及其中文名称",
]


def main() -> None:
    print(f"嵌入模型：{EMBED_MODEL_NAME}")
    print(f"持久化目录：{CHROMA_DIR}")
    print(f"集合名：{COLLECTION_NAME}\n")

    print("[1/2] 重建索引（清空旧集合 + 写入 10 张表）...")
    t0 = time.time()
    n = build_index(reset=True)
    print(f"  完成：写入 {n} 张表，耗时 {time.time() - t0:.1f} s\n")

    print("[2/2] 抽样召回（top-3）：")
    for q in SAMPLE_QUERIES:
        hits = retrieve(q, top_k=3)
        line = "  ".join(f"{name}({score:.3f})" for name, score in hits)
        print(f"  Q: {q}")
        print(f"     {line}")

    print("\n[OK] 索引构建完成。下一步可以跑：")
    print("     python -m eval.retrieval_eval     # 看 15 题召回率")
    print("     python -m eval.run_eval --tag day3 # 端到端评测对比 Day 2")


if __name__ == "__main__":
    main()
