r"""
流式效果诊断脚本：让每个 chunk 的到达时间肉眼可见。

跑法：
    .\.venv\Scripts\python.exe -m scripts.debug_stream
    .\.venv\Scripts\python.exe -m scripts.debug_stream "你想问的问题"

输出形式（每个 chunk 一行）：
    [+0.123s] '上个月'
    [+0.156s] '销售额'
    ...
    共 42 个 chunk，平均 chunk 间隔 28ms，总耗时 1234ms
"""
import sys
import time

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

from src.config import require_api_key
from src.llm import chat_stream


DEFAULT_PROMPT = (
    "请用 100 字左右介绍一下 SAP ERP 系统的核心模块和价值，"
    "用中文回答，分 3-4 句话，每句单独成行。"
)


def main():
    require_api_key()
    question = " ".join(sys.argv[1:]) or DEFAULT_PROMPT
    print(f"问题：{question}\n")
    print("开始流式接收（每行 = 一个 chunk）：\n")

    t0 = time.time()
    chunks: list[tuple[float, str]] = []

    def on_token(text: str) -> None:
        chunks.append((time.time() - t0, text))
        # 每个 chunk 单独一行，带相对时间戳和 repr，便于看到空格 / 换行
        print(f"  [+{time.time() - t0:6.3f}s] {text!r}", flush=True)

    resp = chat_stream(
        [{"role": "user", "content": question}],
        on_token=on_token,
        tag="debug_stream",
    )

    print(f"\n{'=' * 60}")
    print(f"共 {len(chunks)} 个 chunk")
    if len(chunks) >= 2:
        gaps = [chunks[i][0] - chunks[i - 1][0] for i in range(1, len(chunks))]
        avg_gap_ms = int(sum(gaps) / len(gaps) * 1000)
        print(f"平均 chunk 间隔：{avg_gap_ms} ms")
        print(f"首 chunk 到达：  {chunks[0][0] * 1000:.0f} ms")
        print(f"末 chunk 到达：  {chunks[-1][0] * 1000:.0f} ms")
    print(f"总耗时（含网络）：{resp['latency_ms']} ms")
    print(f"in/out tokens : {resp['usage']['prompt_tokens']}/{resp['usage']['completion_tokens']}")

    if len(chunks) <= 2:
        print("\n[!] chunk 数 <= 2，说明 DeepSeek 这次几乎没分块，"
              "看起来就是'一次出完'。换个更长的问题再跑一次试试。")
    else:
        print(f"\n[OK] {len(chunks)} 个 chunk，说明流式确实在工作。"
              "在 main.py 里没察觉是因为解读太短 + DeepSeek 太快。")


if __name__ == "__main__":
    main()
