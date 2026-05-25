r"""
RAG 召回率评测（Day 3）。

复用 Day 2 的 test_cases.json 里每题的 expected_tables 作为"标准答案"，
对每题跑 retrieve(query, top_k) 并计算：

  - hit@k        ：top_k 召回里是否覆盖了所有 expected_tables
  - recall@k     ：召回了多少比例的 expected_tables（覆盖率）
  - 全量平均 / 分难度分布

为什么不算 precision@k：召回阶段宁多勿漏，本项目下游 prompt 只塞 5 张表
（每张 ~50 token），多召一两张表的成本远小于漏召导致 SQL 错。recall 是
更贴目标的指标。

跑法：
    .\.venv\Scripts\python.exe -m eval.retrieval_eval
    .\.venv\Scripts\python.exe -m eval.retrieval_eval --top-k 3
    .\.venv\Scripts\python.exe -m eval.retrieval_eval --top-k 1 3 5

会产出：eval/retrieval_day3.json（结构化基线，写入 simhash 友好）
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from statistics import mean

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

from tabulate import tabulate

from src.retriever import retrieve, collection_size, EMBED_MODEL_NAME


ROOT = Path(__file__).resolve().parent.parent
TEST_FILE = ROOT / "eval" / "test_cases.json"
OUT_FILE = ROOT / "eval" / "retrieval_day3.json"


def grade_case(expected: list[str], hits: list[tuple[str, float]]) -> dict:
    """对单题计算 hit / recall / 缺失表。"""
    expected_up = {t.upper() for t in expected}
    hit_names = {n.upper() for n, _ in hits}
    matched = expected_up & hit_names
    missing = sorted(expected_up - hit_names)
    return {
        "hit": not missing,                              # 全部 expected 都召回了
        "recall": round(len(matched) / len(expected_up), 3) if expected_up else 0.0,
        "matched": sorted(matched),
        "missing": missing,
    }


def evaluate(cases: list[dict], top_k: int) -> dict:
    rows = []
    case_results = []
    t0 = time.time()
    for c in cases:
        q = c["question"]
        expected = c.get("expected_tables", [])
        hits = retrieve(q, top_k=top_k)
        g = grade_case(expected, hits)
        case_results.append({
            "id": c["id"],
            "difficulty": c["difficulty"],
            "question": q,
            "expected_tables": expected,
            "top_k_hits": [{"table": n, "score": s} for n, s in hits],
            **g,
        })
        rows.append([
            c["id"],
            c["difficulty"],
            "HIT" if g["hit"] else "MISS",
            f"{g['recall'] * 100:.0f}%",
            ",".join(expected),
            ",".join(n for n, _ in hits),
            ",".join(g["missing"]) or "-",
        ])

    elapsed = round(time.time() - t0, 2)
    n = len(case_results)
    hit_rate = sum(1 for r in case_results if r["hit"]) / n if n else 0
    avg_recall = mean(r["recall"] for r in case_results) if case_results else 0

    by_diff: dict[str, list[dict]] = {}
    for r in case_results:
        by_diff.setdefault(r["difficulty"], []).append(r)
    by_diff_stats = {
        diff: {
            "total": len(items),
            "hit": sum(1 for x in items if x["hit"]),
            "hit_rate": round(sum(1 for x in items if x["hit"]) / len(items), 3),
            "avg_recall": round(mean(x["recall"] for x in items), 3),
        }
        for diff, items in sorted(by_diff.items())
    }

    return {
        "top_k": top_k,
        "total": n,
        "hit": sum(1 for r in case_results if r["hit"]),
        "hit_rate": round(hit_rate, 3),
        "avg_recall": round(avg_recall, 3),
        "by_difficulty": by_diff_stats,
        "elapsed_sec": elapsed,
        "rows": rows,
        "cases": case_results,
    }


def print_one(report: dict) -> None:
    print(f"\n=== top_k = {report['top_k']} ===")
    print(tabulate(
        report["rows"],
        headers=["ID", "难度", "结果", "recall", "expected", f"top{report['top_k']} 召回", "缺失"],
        tablefmt="simple",
        maxcolwidths=[None, None, None, None, 20, 28, 16],
    ))
    print()
    print(f"hit@{report['top_k']}     ：{report['hit']}/{report['total']} "
          f"({report['hit_rate'] * 100:.1f}%)")
    print(f"avg recall@{report['top_k']}：{report['avg_recall'] * 100:.1f}%")
    print("\n按难度分布：")
    for diff, s in report["by_difficulty"].items():
        print(f"  {diff}: hit {s['hit']}/{s['total']} "
              f"({s['hit_rate'] * 100:.0f}%)  avg recall {s['avg_recall'] * 100:.0f}%")
    print(f"耗时：{report['elapsed_sec']}s")


def main() -> None:
    parser = argparse.ArgumentParser(description="Schema RAG 召回率评测")
    parser.add_argument(
        "--top-k",
        type=int,
        nargs="+",
        default=[1, 3, 5],
        help="要评测的 top_k 列表（可多个，比如 --top-k 1 3 5）",
    )
    args = parser.parse_args()

    n_in_coll = collection_size()
    if n_in_coll == 0:
        print("[!] Chroma 集合为空，先跑：python -m scripts.build_index")
        sys.exit(1)
    print(f"集合就绪：{n_in_coll} 张表已索引（模型 {EMBED_MODEL_NAME}）")

    with open(TEST_FILE, "r", encoding="utf-8") as f:
        suite = json.load(f)
    cases = suite["cases"]
    print(f"评测题数：{len(cases)}\n")

    reports = []
    for k in args.top_k:
        r = evaluate(cases, top_k=k)
        print_one(r)
        # 写入文件时去掉 rows（rows 是人类看的，文件里只留结构化数据）
        r_copy = {k_: v for k_, v in r.items() if k_ != "rows"}
        reports.append(r_copy)

    output = {
        "meta": {
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "model": EMBED_MODEL_NAME,
            "tag": "day3_retrieval",
            "n_tables_in_index": n_in_coll,
            "n_cases": len(cases),
        },
        "reports": reports,
    }
    with open(OUT_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print(f"\n基线已写入：{OUT_FILE}")


if __name__ == "__main__":
    main()
