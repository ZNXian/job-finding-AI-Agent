# -*- coding: utf-8 -*-
# 生成目的：百炼 DashScope OpenAI 兼容模式单例客户端，供 VLM 与文本 LLM 共用（api_key + base_url）。
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

import config as cfg
from config import DASHSCOPE_API_KEY, log

_client: Any = None

# 与参考「结构化 JSON」约定一致：供 llm_services 拼接在 schema 说明后（仍走 OpenAI SDK，非手写 requests）
STRUCTURED_JSON_ENGINE_RULES = """【输出强制要求】
1. 仅输出纯 JSON 文本，禁止 Markdown 代码块（如 ```json）、禁止注释或额外说明文字。
2. 键名必须为双引号字符串，值类型与【目标结构说明】一致，无尾部逗号。
3. 字段无信息时：字符串用 ""；数组用 []；布尔与数字用符合语义的值；不要编造用户未提及的经历。
4. 不要使用「好的」「以下是结果」等前缀或后缀；正文从第一个 { 起至最后一个 } 止。"""


def clean_json_markdown_fences(content: str) -> str:
    """去掉模型偶发包裹的 ``` / ```json 围栏（与手写 HTTP 示例中的清洗逻辑一致）。"""
    if not content:
        return ""
    text = str(content).strip()
    text = re.sub(r"^```(?:[jJ]son)?\s*", "", text)
    text = re.sub(r"\s*```\s*$", "", text)
    return text.strip()


def _base_url() -> str:
    u = (getattr(cfg, "DASHSCOPE_BASE_HTTP_API_URL", None) or "").strip()
    return u or "https://dashscope.aliyuncs.com/compatible-mode/v1"


def _timeout_seconds() -> float:
    vlm_ms = int(getattr(cfg, "VLM_REQUEST_TIMEOUT_MS", 120_000) or 120_000)
    llm_ms = int(getattr(cfg, "LLM_REQUEST_TIMEOUT_MS", 120_000) or 120_000)
    ms = max(vlm_ms, llm_ms)
    return float(min(600, max(30, ms // 1000)))


def get_dashscope_openai_client():
    global _client
    if _client is not None:
        return _client
    if not (DASHSCOPE_API_KEY or "").strip():
        log.error("dashscope_openai: DASHSCOPE_API_KEY 未配置")
        return None
    try:
        from openai import OpenAI
    except ImportError as e:
        log.error("dashscope_openai: 需要 openai 包: %s", e)
        return None
    _client = OpenAI(
        api_key=(DASHSCOPE_API_KEY or "").strip(),
        base_url=_base_url(),
        timeout=_timeout_seconds(),
    )
    return _client


def chat_completion_text(
    messages: List[Dict[str, Any]],
    *,
    model: Optional[str] = None,
    temperature: float = 0.1,
    response_format: Optional[Dict[str, str]] = None,
) -> str:
    """chat.completions.create 非流式（OpenAI SDK）；json_object 时对 content 做 Markdown 围栏清理。"""
    client = get_dashscope_openai_client()
    if client is None:
        return ""
    m = (model or getattr(cfg, "LLM_CHAT_MODEL", None) or "qwen-flash").strip() or "qwen-flash"
    max_tokens = int(getattr(cfg, "LLM_MAX_TOKENS", 2048) or 2048)
    kwargs: Dict[str, Any] = {
        "model": m,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max(256, min(8192, max_tokens)),
    }
    if response_format is not None:
        kwargs["response_format"] = response_format
    try:
        completion = client.chat.completions.create(**kwargs)
        # token 使用统计（若 provider 返回）
        usage = getattr(completion, "usage", None)
        if usage is not None:
            pt = getattr(usage, "prompt_tokens", None)
            ct = getattr(usage, "completion_tokens", None)
            tt = getattr(usage, "total_tokens", None)
            log.debug(
                "LLM usage: prompt_tokens=%s completion_tokens=%s total_tokens=%s model=%s",
                pt,
                ct,
                tt,
                m,
            )
        else:
            log.debug("LLM usage: (provider did not return usage) model=%s", m)
        raw = completion.choices[0].message.content
    except Exception as e:
        log.warning("dashscope_openai chat.completions: %s", e)
        return ""
    if raw is None:
        return ""
    out = str(raw).strip()
    if response_format and response_format.get("type") == "json_object":
        out = clean_json_markdown_fences(out)
    return out
