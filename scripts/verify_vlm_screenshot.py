# -*- coding: utf-8 -*-
# AI 生成
# 生成目的：不启动浏览器，本地截屏走与爬虫子模块相同的 VLM（openai 兼容 + extract_by_vlm 重试统计）

import argparse
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# 需先拉取 config 以加载项目根 .env
import config as cfg  # noqa: E402
from crawlers.liepin_vlm import (  # noqa: E402
    extract_by_vlm,
    format_intro_dict_to_liepin_text,
)

# AI 生成
# 生成目的：默认用用户指定的单张图（相对项目根，便于在任意机器上克隆后仍有效）
_DEFAULT_REL = Path("screenshots") / "liepin_9b8abf5d07d8bc57754b_1777277077643.png"
DEFAULT_IMAGE = _ROOT / _DEFAULT_REL
# 用户本机显式绝对路径（与上为同一文件，若你移动了图可传参覆盖）
_ALSO_FIX = Path(r"D:\PythonProjects\job-finding-AI-Agent\screenshots\liepin_9b8abf5d07d8bc57754b_1777277077643.png")


def _pick_default() -> Path:
    if _ALSO_FIX.is_file():
        return _ALSO_FIX
    return DEFAULT_IMAGE


def main() -> int:
    ap = argparse.ArgumentParser(
        description="固定/指定 PNG → DashScope 多模态 VLM，与爬虫子模块一致（.env: DASHSCOPE_API_KEY，与 LLM 同源；VLM_MODEL、VLM_REQUEST_TIMEOUT_MS）",
    )
    ap.add_argument(
        "image",
        nargs="?",
        default=None,
        type=str,
        help="截屏路径；省略时优先 D:\\...\\liepin_9b8a... 若存在，否则用项目下 screenshots/... 默认名",
    )
    args = ap.parse_args()
    p = Path(args.image) if args.image else _pick_default()
    if not p.is_file():
        print("未找到截屏文件:", p.resolve() if p else p, file=sys.stderr)
        print("请传入路径或把 PNG 放在:", DEFAULT_IMAGE, file=sys.stderr)
        return 1

    print("image:", p.resolve())
    print("DASHSCOPE_API_KEY:", "已配置" if (cfg.DASHSCOPE_API_KEY or "").strip() else "未配置(请设 .env)（与 llm 共用）")
    print("DASHSCOPE_BASE_HTTP_API_URL:", getattr(cfg, "DASHSCOPE_BASE_HTTP_API_URL", ""), "(import config 时已设 dashscope.base_http_api_url)")
    print("VLM_MODEL:", getattr(cfg, "VLM_MODEL", "qwen3.6-plus"))
    print("VLM_REQUEST_TIMEOUT_MS:", getattr(cfg, "VLM_REQUEST_TIMEOUT_MS", 120_000))
    d = extract_by_vlm(p)
    print("---- JSON（五字段）----")
    print(json.dumps(d, ensure_ascii=False, indent=2))
    if d:
        print("---- 与主流程一致的「介绍」文本 ----")
        print(format_intro_dict_to_liepin_text(d))
    else:
        print("(空 dict：请见上方 logging 中 VLM 的 warning/错误说明)", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
