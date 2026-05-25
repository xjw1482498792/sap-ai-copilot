"""
Schema RAG 检索层（Day 3）。

设计要点：
  1. embedding 模型：BGE-small-zh-v1.5（中文 SOTA 之一，512 维，本地推理无成本）
  2. 向量库：Chroma 持久化在 data/chroma_db/，集合名 sap_tables
  3. 相似度：cosine（Chroma 默认 hnsw:space=cosine 时 distance = 1 - sim）
  4. embedding 由本模块自己控制，不依赖 Chroma 内置 embedding_function ——
     这样后续换模型只动这一个地方，Chroma 只当向量存储用。
  5. 每张表索引成一个文档：表名 + 模块 + 业务描述 + 所有字段中文含义拼接，
     让用户的中文业务问题能召中正确的德文缩写表。

调用层：
    from src.retriever import build_index, retrieve, retrieve_schema_text
    build_index()                          # 一次性，重跑会重建
    retrieve("上个月销售额前 5 的客户", top_k=5)
    # -> [("VBAK", 0.71), ("KNA1", 0.65), ...]
    retrieve_schema_text("...", top_k=5)   # 直接拿拼好的 prompt 文本
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from src.config import ROOT
from src.schemas import SCHEMAS, get_table, schema_to_prompt_text


CHROMA_DIR = ROOT / "data" / "chroma_db"
COLLECTION_NAME = "sap_tables"
EMBED_MODEL_NAME = "BAAI/bge-small-zh-v1.5"

# HuggingFace 在 Windows 上没管理员权限会刷一行符号链接 warning，关掉
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

# 模型 / 客户端用全局懒加载：第一次调用才付加载代价（~1s + 首次 ~95MB 下载）
_model = None
_client = None


def _get_model():
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer
        _model = SentenceTransformer(EMBED_MODEL_NAME)
    return _model


def _get_client():
    global _client
    if _client is None:
        import chromadb
        CHROMA_DIR.mkdir(parents=True, exist_ok=True)
        _client = chromadb.PersistentClient(path=str(CHROMA_DIR))
    return _client


def _get_or_create_collection(reset: bool = False):
    client = _get_client()
    if reset:
        try:
            client.delete_collection(COLLECTION_NAME)
        except Exception:
            pass
    return client.get_or_create_collection(
        COLLECTION_NAME,
        metadata={"hnsw:space": "cosine"},
    )


def build_doc_text(table: dict) -> str:
    """为单张表构造用于 embedding 的文档文本。

    把"表的所有业务语义"压成一段中文：表名 + 模块 + 一句话功能 + 所有字段含义。
    用户的问题里既可能出现业务词（"销售订单""客户"），也可能出现字段语义
    （"金额""数量""创建日期"），把字段含义也塞进去能显著提升中→德的召回。
    """
    fields = "、".join(f"{c['name']}({c['desc']})" for c in table["columns"])
    return (
        f"{table['name']} - {table['module']} 模块。{table['desc']}\n"
        f"字段：{fields}"
    )


def build_index(reset: bool = True) -> int:
    """把 SCHEMAS 全量索引到 Chroma。默认重建（reset=True）。"""
    coll = _get_or_create_collection(reset=reset)
    docs = [build_doc_text(t) for t in SCHEMAS]
    ids = [t["name"] for t in SCHEMAS]
    metas = [
        {"table": t["name"], "module": t["module"], "desc": t["desc"]}
        for t in SCHEMAS
    ]
    embs = _get_model().encode(docs, normalize_embeddings=True).tolist()
    coll.add(ids=ids, documents=docs, metadatas=metas, embeddings=embs)
    return len(docs)


def retrieve(query: str, top_k: int = 5) -> list[tuple[str, float]]:
    """召回 top_k 张表，返回 [(table_name, cosine_similarity), ...] 按相似度降序。"""
    coll = _get_or_create_collection(reset=False)
    if coll.count() == 0:
        raise RuntimeError(
            "Chroma 集合为空。请先运行：python -m scripts.build_index"
        )
    q_emb = _get_model().encode([query], normalize_embeddings=True).tolist()
    res = coll.query(query_embeddings=q_emb, n_results=top_k)
    names = res["ids"][0]
    distances = res["distances"][0]
    return [(name, round(1.0 - d, 4)) for name, d in zip(names, distances)]


def retrieve_schema_text(query: str, top_k: int = 5) -> tuple[str, list[tuple[str, float]]]:
    """召回 + 拼成给 LLM 的 schema 文本。返回 (prompt_text, hits)。

    prompt 文本沿用 schema_to_prompt_text() 的格式，保证和 Day 1/2 的 prompt
    风格一致 —— 这样 Day 3 vs Day 2 的对比只变 schema 数量，不掺其他变量。
    """
    hits = retrieve(query, top_k=top_k)
    parts = []
    for name, _score in hits:
        t = get_table(name)
        if t is not None:
            parts.append(schema_to_prompt_text(t))
    return "\n\n".join(parts), hits


def collection_size() -> int:
    """诊断用：当前集合里有多少条文档。"""
    coll = _get_or_create_collection(reset=False)
    return coll.count()
