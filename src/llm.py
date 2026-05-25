"""
DeepSeek 客户端封装。

DeepSeek 完全兼容 OpenAI SDK，所以直接用 openai 库，只换 base_url 和 model 名。
后续 Day 9 做模型对比时，只要替换这里的 base_url + model 就能跑别的厂商，
所以这层封装设计成"模型可换"的接口。

Day 2 新增：
  - chat_stream() 流式接口，回调式吐字 + 自动累加 usage / latency
  - chat() / chat_stream() 都自动写 logs/runs.jsonl

Day 12 新增：
  - chat_stream_iter() 生成器版接口，每个 token chunk 直接 yield 出去 ——
    专门给 Streamlit 的 st.write_stream() 用，无需回调、无需 sleep、
    UI 自然按 chunk 节奏渲染（不像 CLI 那样需要打字机延迟，Web 端浏览器
    自己处理滚动和回流即可）
"""
import time
from typing import Callable, Iterator, Optional

from openai import OpenAI

from src.config import DEEPSEEK_API_KEY, DEEPSEEK_BASE_URL, DEEPSEEK_MODEL
from src.log import log_llm_call


_client = OpenAI(
    api_key=DEEPSEEK_API_KEY,
    base_url=DEEPSEEK_BASE_URL,
)


def chat(
    messages: list[dict],
    model: Optional[str] = None,
    temperature: float = 0.0,
    max_tokens: int = 2000,
    tools: Optional[list] = None,
    tag: str = "",
) -> dict:
    """
    调一次对话补全（非流式）。

    返回：
        {
            "content": str,            # 文本回复
            "tool_calls": list | None, # function calling 调用，Day 5 用
            "usage": {prompt_tokens, completion_tokens, total_tokens},
            "latency_ms": int,         # 本地测的总耗时（含网络）
            "model": str,
        }
    """
    t0 = time.time()
    use_model = model or DEEPSEEK_MODEL
    kwargs = dict(
        model=use_model,
        messages=messages,
        temperature=temperature,
        max_tokens=max_tokens,
    )
    if tools:
        kwargs["tools"] = tools
        kwargs["tool_choice"] = "auto"

    resp = _client.chat.completions.create(**kwargs)
    msg = resp.choices[0].message

    result = {
        "content": msg.content or "",
        "tool_calls": [
            {
                "id": tc.id,
                "name": tc.function.name,
                "arguments": tc.function.arguments,
            }
            for tc in (msg.tool_calls or [])
        ] or None,
        "usage": {
            "prompt_tokens": resp.usage.prompt_tokens,
            "completion_tokens": resp.usage.completion_tokens,
            "total_tokens": resp.usage.total_tokens,
        },
        "latency_ms": int((time.time() - t0) * 1000),
        "model": resp.model,
    }

    log_llm_call(
        model=result["model"],
        messages=messages,
        content=result["content"],
        usage=result["usage"],
        latency_ms=result["latency_ms"],
        stream=False,
        tag=tag,
    )
    return result


def chat_stream(
    messages: list[dict],
    on_token: Optional[Callable[[str], None]] = None,
    model: Optional[str] = None,
    temperature: float = 0.0,
    max_tokens: int = 2000,
    tag: str = "",
) -> dict:
    """
    流式对话补全。每收到一个 token chunk 就回调 on_token(text)。

    返回结构和 chat() 完全一致，调用方可以直接拿 content / usage / latency_ms。

    DeepSeek 兼容 OpenAI 协议：开启 stream_options={"include_usage": True}
    后，最后一个 chunk 会带完整 usage —— 否则流式回包 usage=None。
    """
    t0 = time.time()
    use_model = model or DEEPSEEK_MODEL

    stream = _client.chat.completions.create(
        model=use_model,
        messages=messages,
        temperature=temperature,
        max_tokens=max_tokens,
        stream=True,
        stream_options={"include_usage": True},
    )

    chunks: list[str] = []
    usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    real_model = use_model

    for event in stream:
        if event.model:
            real_model = event.model
        # usage 只在最后一个 chunk 里，choices 可能为空
        if event.usage:
            usage = {
                "prompt_tokens": event.usage.prompt_tokens,
                "completion_tokens": event.usage.completion_tokens,
                "total_tokens": event.usage.total_tokens,
            }
        if not event.choices:
            continue
        delta = event.choices[0].delta
        piece = getattr(delta, "content", None)
        if piece:
            chunks.append(piece)
            if on_token:
                on_token(piece)

    content = "".join(chunks)
    result = {
        "content": content,
        "tool_calls": None,
        "usage": usage,
        "latency_ms": int((time.time() - t0) * 1000),
        "model": real_model,
    }

    log_llm_call(
        model=result["model"],
        messages=messages,
        content=content,
        usage=usage,
        latency_ms=result["latency_ms"],
        stream=True,
        tag=tag,
    )
    return result


def chat_stream_iter(
    messages: list[dict],
    model: Optional[str] = None,
    temperature: float = 0.0,
    max_tokens: int = 2000,
    tag: str = "",
    on_done: Optional[Callable[[dict], None]] = None,
) -> Iterator[str]:
    """流式对话补全的生成器版本（Day 12 Web UI 专用）。

    和 chat_stream() 同协议拿 stream，但用 yield 把每个 token 吐出去，
    Streamlit 的 st.write_stream() 直接接受这个 generator 就能边收边渲染。

    日志和 usage 在流结束后通过 on_done(result_dict) 回调一次性回传，
    避免调用方拿不到完整 usage / latency_ms（这两个只在最后一个 chunk 出现）。
    """
    t0 = time.time()
    use_model = model or DEEPSEEK_MODEL

    stream = _client.chat.completions.create(
        model=use_model,
        messages=messages,
        temperature=temperature,
        max_tokens=max_tokens,
        stream=True,
        stream_options={"include_usage": True},
    )

    chunks: list[str] = []
    usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    real_model = use_model

    for event in stream:
        if event.model:
            real_model = event.model
        if event.usage:
            usage = {
                "prompt_tokens": event.usage.prompt_tokens,
                "completion_tokens": event.usage.completion_tokens,
                "total_tokens": event.usage.total_tokens,
            }
        if not event.choices:
            continue
        delta = event.choices[0].delta
        piece = getattr(delta, "content", None)
        if piece:
            chunks.append(piece)
            yield piece

    content = "".join(chunks)
    latency_ms = int((time.time() - t0) * 1000)

    log_llm_call(
        model=real_model,
        messages=messages,
        content=content,
        usage=usage,
        latency_ms=latency_ms,
        stream=True,
        tag=tag,
    )

    if on_done is not None:
        on_done({
            "content": content,
            "usage": usage,
            "latency_ms": latency_ms,
            "model": real_model,
        })
