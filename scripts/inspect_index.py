r"""
"打开看一下知识库"的工具脚本。

Chroma 把数据存成 SQLite + 二进制向量文件，肉眼读不了。
这个脚本调 Chroma API 把每条记录的 4 个字段（id / document / embedding / metadata）
都拿出来打印，让你随时确认知识库里到底装了什么。

跑法：
    .\.venv\Scripts\python.exe -m scripts.inspect_index            # 列全部
    .\.venv\Scripts\python.exe -m scripts.inspect_index --id VBAK  # 只看一张表
    .\.venv\Scripts\python.exe -m scripts.inspect_index --full     # 文档/向量都不截断
"""
from __future__ import annotations

import argparse
import sys

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

import chromadb

from src.retriever import CHROMA_DIR, COLLECTION_NAME


def main() -> None:
    parser = argparse.ArgumentParser(description="打开看 Chroma 知识库的内容")
    parser.add_argument("--id", default="", help="只看某个 id（表名，如 VBAK），默认看全部")
    parser.add_argument("--full", action="store_true",
                        help="不截断文档文本和向量（默认前 80 字 + 前 5 维）")
    args = parser.parse_args()

    client = chromadb.PersistentClient(path=str(CHROMA_DIR))
    coll = client.get_collection(COLLECTION_NAME)

    print(f"集合名      ：{coll.name}")
    print(f"持久化目录  ：{CHROMA_DIR}")
    print(f"条目数      ：{coll.count()}")
    print(f"相似度配置  ：{coll.metadata}\n")

    kwargs: dict = {"include": ["documents", "metadatas", "embeddings"]}
    if args.id:
        kwargs["ids"] = [args.id]
    res = coll.get(**kwargs)

    if not res["ids"]:
        print(f"[!] 找不到 id={args.id}")
        sys.exit(1)

    for i, doc_id in enumerate(res["ids"]):
        doc = res["documents"][i]
        meta = res["metadatas"][i]
        emb = res["embeddings"][i]

        print(f"── [{i + 1}] id = {doc_id}")
        print(f"   metadata        : {meta}")
        if args.full:
            print(f"   document        : {doc}")
            print(f"   embedding (512) : {[round(float(x), 4) for x in emb]}")
        else:
            print(f"   document (前80字): {doc[:80]}...")
            print(f"   embedding 维度  : {len(emb)}   "
                  f"前 5 维 : {[round(float(x), 4) for x in emb[:5]]}")
        print()


if __name__ == "__main__":
    main()
