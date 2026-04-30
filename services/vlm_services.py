# -*- coding: utf-8 -*-
# AI 生成
# 生成目的：百炼 OpenAI 兼容模式 chat.completions，多模态岗位截屏 → 五字段 JSON（与 crawlers.liepin_vlm 同构）。
import base64
import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from config import DASHSCOPE_API_KEY, log
import config as cfg

from services.dashscope_openai import get_dashscope_openai_client

os.environ["DASHSCOPE_API_KEY"] = (DASHSCOPE_API_KEY or "").strip()

VLM_RESUME_EXTRACT_PROMPT = (
    "你是简历/履历 OCR 与整理助手。请**仅**根据图片中的文字（含中英文），整理为**一份可读纯文本简历**。\n"
    "输出**一个** JSON 对象，不要 Markdown 代码块、不要其他说明。结构如下：\n"
    '{"plain_text":"..."}\n'
    "要求：\n"
    "- plain_text 为字符串，保留段落与条列（可用换行）；不要编造图中没有的经历。\n"
    "- 图中无法辨认处用「（不清晰）」占位，不要留空 JSON。\n"
    "- 不要输出岗位 JD 五字段结构；这是个人简历图，不是招聘页截图。"
)

VLM_EXTRACT_USER_TEXT = (
    "你是招聘网站岗位详情分析助手。请**仅**根据图片中的文字内容，"
    "输出**一个** JSON 对象，不要代码块、不要其他说明。结构如下，缺失字段用空字符串或空数组：\n"
    '{"title":"","salary":"","skills":[],"requirements":[],"benefits":[]}\n'
    "字段说明：\n"
    "title: 岗位名称；salary: 薪资或「面议」；"
    "skills: 技术栈/硬技能关键词列表；"
    "requirements: 岗位职责、任职要求、工作内容等，可拆成多条；"
    "benefits: 福利、公司福利等，可拆成多条。"
)


def _vlm_model_name() -> str:
    return (getattr(cfg, "VLM_MODEL", None) or "qwen-vl-ocr-latest").strip() or "qwen-vl-ocr-latest"


def encode_image(image_path: Union[str, Path]) -> str:
    p = Path(image_path)
    with p.open("rb") as image_file:
        return base64.b64encode(image_file.read()).decode("utf-8")


def _data_url_for_local_image(image_path: Union[str, Path]) -> str:
    p = Path(image_path)
    ext = p.suffix.lower()
    base64_image = encode_image(p)
    if ext in (".jpg", ".jpeg"):
        return f"data:image/jpeg;base64,{base64_image}"
    if ext == ".webp":
        return f"data:image/webp;base64,{base64_image}"
    return f"data:image/png;base64,{base64_image}"


def normalize_intro_five_dict(d: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not isinstance(d, dict):
        return {
            "title": "",
            "salary": "",
            "skills": [],
            "requirements": [],
            "benefits": [],
        }

    def _as_str_list(x: Any) -> List[str]:
        if x is None:
            return []
        if isinstance(x, str):
            s = x.strip()
            return [s] if s else []
        if isinstance(x, (list, tuple)):
            return [str(i).strip() for i in x if str(i).strip()]
        return [str(x).strip()] if str(x).strip() else []

    return {
        "title": str(d.get("title", "") or "").strip(),
        "salary": str(d.get("salary", "") or "").strip(),
        "skills": _as_str_list(d.get("skills")),
        "requirements": _as_str_list(d.get("requirements")),
        "benefits": _as_str_list(d.get("benefits")),
    }


def _parse_vlm_json_payload(text: str) -> Optional[Dict[str, Any]]:
    if not text or not str(text).strip():
        return None
    t = str(text).strip()
    t = t.replace("```json", "").replace("```", "")
    t = t.strip()
    try:
        o = json.loads(t)
        if isinstance(o, dict):
            return o
    except json.JSONDecodeError:
        pass
    m = re.search(r"\{[\s\S]*\}\s*$", t)
    if m:
        try:
            o = json.loads(m.group(0))
            if isinstance(o, dict):
                return o
        except json.JSONDecodeError:
            return None
    return None


def is_nonempty_intro_five(d: Dict[str, Any]) -> bool:
    o = normalize_intro_five_dict(d)
    if o.get("title") or o.get("salary"):
        return True
    if o.get("skills") or o.get("benefits"):
        return True
    for r in o.get("requirements") or []:
        if r and str(r).strip():
            return True
    return False


def extract_intro_five_from_image(image_path: Union[str, Path]) -> Dict[str, Any]:
    p = Path(image_path)
    if not p.is_file():
        log.warning("vlm_services: 图片不存在 %s", image_path)
        return {}
    if not (DASHSCOPE_API_KEY or "").strip():
        log.error("vlm_services: DASHSCOPE_API_KEY 未配置")
        return {}
    client = get_dashscope_openai_client()
    if client is None:
        return {}
    data_url = _data_url_for_local_image(p)
    min_px = int(getattr(cfg, "VLM_IMAGE_MIN_PIXELS", 32 * 32 * 3) or (32 * 32 * 3))
    max_px = int(getattr(cfg, "VLM_IMAGE_MAX_PIXELS", 32 * 32 * 8192) or (32 * 32 * 8192))
    try:
        completion = client.chat.completions.create(
            model=_vlm_model_name(),
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {"url": data_url},
                            "min_pixels": min_px,
                            "max_pixels": max_px,
                        },
                        {
                            "type": "text",
                            "text": VLM_EXTRACT_USER_TEXT,
                        },
                    ],
                }
            ],
        )
    except Exception as e:
        log.warning("vlm_services: chat.completions.create 异常: %s", e)
        return {}
    try:
        raw = completion.choices[0].message.content
    except (IndexError, AttributeError) as e:
        log.warning("vlm_services: 无 choices 内容: %s", e)
        return {}
    if raw is None:
        log.warning("vlm_services: 模型 content 为 None")
        return {}
    raw = str(raw).strip()
    if not raw:
        log.warning("vlm_services: 模型 content 为空")
        return {}
    parsed = _parse_vlm_json_payload(raw)
    if not parsed:
        log.warning("vlm_services: JSON 解析失败，raw 片段: %s", raw[:300])
        return {}
    d = normalize_intro_five_dict(parsed)
    if not is_nonempty_intro_five(d):
        log.warning("vlm_services: 五字段均为空")
        return {}
    return d


def extract_resume_plain_text_from_image(image_path: Union[str, Path]) -> str:
    """
    简历/履历类图片 → 纯文本（JSON.plain_text）。与岗位截图 extract_intro_five_from_image 分流，勿混用 prompt。
    """
    p = Path(image_path)
    if not p.is_file():
        log.warning("vlm_services: 简历图不存在 %s", image_path)
        return ""
    if not (DASHSCOPE_API_KEY or "").strip():
        log.error("vlm_services: DASHSCOPE_API_KEY 未配置，无法 VLM 解析简历图")
        return ""
    client = get_dashscope_openai_client()
    if client is None:
        return ""
    data_url = _data_url_for_local_image(p)
    min_px = int(getattr(cfg, "VLM_IMAGE_MIN_PIXELS", 32 * 32 * 3) or (32 * 32 * 3))
    max_px = int(getattr(cfg, "VLM_IMAGE_MAX_PIXELS", 32 * 32 * 8192) or (32 * 32 * 8192))
    try:
        completion = client.chat.completions.create(
            model=_vlm_model_name(),
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {"url": data_url},
                            "min_pixels": min_px,
                            "max_pixels": max_px,
                        },
                        {
                            "type": "text",
                            "text": VLM_RESUME_EXTRACT_PROMPT,
                        },
                    ],
                }
            ],
        )
    except Exception as e:
        log.warning("vlm_services: 简历 VLM chat.completions 异常: %s", e)
        return ""
    try:
        raw = completion.choices[0].message.content
    except (IndexError, AttributeError) as e:
        log.warning("vlm_services: 简历 VLM 无 choices: %s", e)
        return ""
    if raw is None:
        return ""
    raw = str(raw).strip()
    if not raw:
        return ""
    parsed = _parse_vlm_json_payload(raw)
    if not isinstance(parsed, dict):
        return ""
    pt = parsed.get("plain_text")
    if pt is None:
        return ""
    s = str(pt).strip()
    return s


def get_vlm_openai_client():
    """兼容旧调用名：与 get_dashscope_openai_client 相同。"""
    return get_dashscope_openai_client()
