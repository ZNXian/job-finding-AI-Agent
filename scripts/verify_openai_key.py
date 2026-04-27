# -*- coding: utf-8 -*-
# AI 生成
# 生成目的：最小化调用 OpenAI Embeddings，验证 config.openai_API_KEY 是否有效（与 job_store 向量链路一致）

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import config as cfg  # noqa: E402


def main() -> int:
    # AI 生成
    # 生成目的：无 Key 时直接退出，避免误报为网络错误
    key = (getattr(cfg, "openai_API_KEY", None) or "").strip()
    if not key:
        print("失败：config.openai_API_KEY 为空，请先在 config.py 中填写。")
        return 1

    model = (getattr(cfg, "OPENAI_EMBEDDING_MODEL", None) or "text-embedding-3-small").strip()
    base_raw = (getattr(cfg, "OPENAI_API_BASE", None) or "").strip()
    # AI 生成
    # 生成目的：与提供商示例一致 base_url="https://ai.nengyongai.cn/v1"；仅主机名时补 /v1
    base = base_raw.rstrip("/")
    if base and not base.endswith("/v1"):
        base = f"{base}/v1"

    try:
        from openai import OpenAI
    except ImportError:
        print("失败：未安装 openai 包，请执行: pip install openai")
        return 1

    # AI 生成
    # 生成目的：对齐提供商：OpenAI(api_key=..., base_url=...) 后 embeddings.create(input=..., model=...)
    client_kw: dict = {"api_key": key}
    if base:
        client_kw["base_url"] = base
    client = OpenAI(**client_kw)
    resp = client.embeddings.create(input="查询北京天气", model=model)
    vec = resp.data[0].embedding
    dim = len(vec)
    print(f"成功：OpenAI API Key 可用；模型={model}，返回向量维度={dim}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
