"""临时调试辅助：按 session_id 过滤打印 logs/runs.jsonl 的关键字段。"""
import io
import json
import sys

sid = sys.argv[1] if len(sys.argv) > 1 else None
KEEP = ("tag", "sql", "ok", "row_count", "content_preview",
        "latency_ms", "node", "attempt", "error", "usage")

for line in io.open("logs/runs.jsonl", encoding="utf-8"):
    rec = json.loads(line)
    if sid and rec.get("session_id") != sid:
        continue
    p = rec.get("payload", {})
    slim = {k: p[k] for k in KEEP if k in p}
    print(f"[{rec['event']}] {json.dumps(slim, ensure_ascii=False)}")
