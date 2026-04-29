# -*- coding:utf-8 -*-
# @CreatTime : 2026 10:14
# @Author : XZN
# 文本 LLM：百炼 OpenAI 兼容模式；筛选与招呼语按批（最多 50 条）结构化 JSON 调用。
import json
import os
from typing import Any, Dict, List, Optional

import config as cfg
from config import DASHSCOPE_API_KEY, log

from services.dashscope_openai import (
    STRUCTURED_JSON_ENGINE_RULES,
    chat_completion_text,
)

os.environ["DASHSCOPE_API_KEY"] = (DASHSCOPE_API_KEY or "").strip()

LLM_JOB_FILTER_BATCH_MAX = 50

_FILTER_BATCH_SYSTEM = (
    "你是一个严格的数据格式化引擎。用户消息中包含同一批多个岗位（编号从 0 开始连续整数），"
    "须对每个岗位独立判断，不得混淆不同编号。\n"
    "【目标结构】\n"
    "输出 JSON 对象，键 items 为数组。items 长度必须等于本批岗位数 N。"
    "每一项对象必须包含且仅包含：\n"
    "- index: integer，0 到 N-1，与消息中「岗位编号」一致\n"
    "- match_level: string，仅能为 高、中、低 之一\n"
    "- reason: string，简述匹配或不匹配理由\n"
    "- apply: string，仅能为 是 或 否，表示是否建议投递\n"
    "\n"
    + STRUCTURED_JSON_ENGINE_RULES
)

_GREETING_BATCH_SYSTEM = (
    "你是一个严格的数据格式化引擎。用户消息中包含【我的求职场景】及若干「需要撰写打招呼」的岗位工作介绍，"
    "每个岗位带整数序号（与筛选批内编号一致）。\n"
    "招呼语须：语气专业有礼貌、突出与岗位匹配点、不编造简历没有的经历；单段、不超过 200 汉字（含标点），"
    "不要标题、不要分条、不要「敬上」等套话结尾。\n"
    "【目标结构】\n"
    "输出 JSON：键 items 为数组。每项含：\n"
    "- index: integer，与消息中的岗位序号一致\n"
    "- greeting: string，该岗位对应的一段招呼语\n"
    "items 必须覆盖消息中列出的全部序号，各 index 唯一。\n"
    "\n"
    + STRUCTURED_JSON_ENGINE_RULES
)

_SCENE_USER_TMPL = """你是求职场景智能匹配助手。

已有场景：
{scene_list}

用户需求与简历：
{user_text}

请判断是否匹配已有场景。
如果匹配，返回场景编号，例如：2
如果不匹配，返回 new

只返回结果，不要多余文字。
"""

_STANDARD_SYSTEM = (
    "你是一个严格的数据格式化引擎。请将用户【求职需求与简历】解析为指定的 JSON 结构（薪资单位：K）。\n"
    "【目标结构说明】\n"
    "必须包含且仅包含以下键：\n"
    "- search_keywords: string[]，搜索关键词，至多约 8 个\n"
    "- city: string 或 string[]，期望城市，可多城\n"
    "- province: string，省/自治区/直辖市，无可填空字符串\n"
    "- accept_remote: boolean\n"
    "- min_salary, max_salary: number，单位 K\n"
    "- requirements: string[]，个人要求与背景要点，至多 8 条\n"
    "\n"
    + STRUCTURED_JSON_ENGINE_RULES
)


def _job_info_block(job: Dict[str, Any]) -> str:
    return (
        f"岗位：{job.get('标题', '')}\n公司：{job.get('公司', '')}\n薪资：{job.get('薪资', '')}\n"
        f"地点：{job.get('地点', '')}\n岗位介绍：{job.get('介绍', '')}"
    )


def _build_filter_batch_user_message(jobs: List[Dict[str, Any]]) -> str:
    n = len(jobs)
    last = n - 1
    lines: List[str] = [
        "你是专业求职筛选助手，严格按条件独立判断本批每一个岗位。",
        "",
        "我的要求：",
        str(cfg.MY_REQUIREMENT),
        "",
        f"本批共 {n} 个岗位，编号 0 到 {last}。请对每个编号分别给出匹配度、理由、是否投递（结构化结果见系统说明）。",
        "",
    ]
    for i, job in enumerate(jobs):
        lines.append(f"--- 岗位编号 {i} ---")
        lines.append(_job_info_block(job))
        lines.append("")
    return "\n".join(lines)


def _parse_json_object(raw: str) -> Optional[Dict[str, Any]]:
    if not raw or not str(raw).strip():
        return None
    try:
        o = json.loads(raw)
        return o if isinstance(o, dict) else None
    except json.JSONDecodeError:
        return None


def _normalize_match_level(s: Any) -> str:
    t = str(s or "").strip()
    if "高" in t:
        return "高"
    if "中" in t:
        return "中"
    if "低" in t:
        return "低"
    return "低"


def _normalize_apply(s: Any) -> str:
    t = str(s or "").strip()
    if t.startswith("是"):
        return "是"
    return "否"


def _filter_item_to_three_lines(item: Dict[str, Any]) -> str:
    tier = _normalize_match_level(
        item.get("match_level") or item.get("匹配度") or item.get("tier")
    )
    reason = str(item.get("reason") or item.get("理由") or "").strip() or "（无）"
    ap = _normalize_apply(item.get("apply") or item.get("是否投递") or item.get("apply_whether"))
    return f"【匹配度】{tier}\n【理由】{reason}\n【是否投递】{ap}\n"


def _call_filter_batch_structured(jobs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """返回与 jobs 等长的 item 字典列表（按 index 对齐）。"""
    n = len(jobs)
    user = _build_filter_batch_user_message(jobs)
    raw = chat_completion_text(
        [
            {"role": "system", "content": _FILTER_BATCH_SYSTEM},
            {"role": "user", "content": user},
        ],
        temperature=0.1,
        response_format={"type": "json_object"},
    )
    data = _parse_json_object(raw) or {}
    items = data.get("items")
    if not isinstance(items, list):
        log.warning("批量筛选：返回无 items 数组，已用占位填充")
        return [
            {"index": i, "match_level": "低", "reason": "解析失败", "apply": "否"}
            for i in range(n)
        ]
    by_idx: Dict[int, Dict[str, Any]] = {}
    for it in items:
        if not isinstance(it, dict):
            continue
        try:
            idx = int(it.get("index"))
        except (TypeError, ValueError):
            continue
        by_idx[idx] = it
    out: List[Dict[str, Any]] = []
    for i in range(n):
        it = by_idx.get(i)
        if it is None:
            out.append(
                {"index": i, "match_level": "低", "reason": "模型未返回该编号", "apply": "否"}
            )
            continue
        out.append(
            {
                "index": i,
                "match_level": _normalize_match_level(
                    it.get("match_level") or it.get("匹配度")
                ),
                "reason": str(it.get("reason") or it.get("理由") or "").strip() or "（无）",
                "apply": _normalize_apply(it.get("apply") or it.get("是否投递")),
            }
        )
    return out


def _build_greeting_batch_user_message(
    scene_context: str,
    jobs: List[Dict[str, Any]],
    indices: List[int],
) -> str:
    lines: List[str] = [
        "以下为【我的求职场景】：",
        scene_context[:6000],
        "",
        "以下岗位在筛选中匹配度为「高」或「中」，请为每个序号各写一段打招呼（见系统 JSON 说明）。",
        "",
    ]
    for i in indices:
        intro = (jobs[i].get("介绍") or "").strip() or "（无文本介绍，请结合标题与要求撰写）"
        lines.append(f"--- 岗位序号 {i} 工作介绍 ---")
        lines.append(intro[:4000])
        lines.append("")
    return "\n".join(lines)


def _call_greeting_batch_structured(
    scene_context: str,
    jobs: List[Dict[str, Any]],
    indices: List[int],
) -> Dict[int, str]:
    if not indices:
        return {}
    user = _build_greeting_batch_user_message(scene_context, jobs, indices)
    raw = chat_completion_text(
        [
            {"role": "system", "content": _GREETING_BATCH_SYSTEM},
            {"role": "user", "content": user},
        ],
        temperature=0.1,
        response_format={"type": "json_object"},
    )
    data = _parse_json_object(raw) or {}
    items = data.get("items")
    out: Dict[int, str] = {}
    if not isinstance(items, list):
        return out
    for it in items:
        if not isinstance(it, dict):
            continue
        try:
            idx = int(it.get("index"))
        except (TypeError, ValueError):
            continue
        g = str(it.get("greeting") or it.get("content") or "").strip()
        out[idx] = g[:200] if g else ""
    return out


def _scene_context_for_greeting(scene_id: int) -> str:
    from services.scences import scene_manager

    s = scene_manager.get_scene_by_id(int(scene_id))
    if not s:
        return "（无法加载场景，请只依据岗位信息撰写）"
    kws = s.get("search_keywords") or []
    if isinstance(kws, list):
        kws = "、".join(str(x) for x in kws)
    city = s.get("city", "")
    if isinstance(city, list):
        city = "、".join(str(x) for x in city)
    prov = (s.get("province") or "").strip()
    reqs = s.get("requirements") or []
    if isinstance(reqs, list):
        reqs = "\n".join(f"- {r}" for r in reqs)
    return (
        f"关键词：{kws}\n"
        f"城市：{city}\n"
        f"省份：{prov or '无'}\n"
        f"接受远程：{s.get('accept_remote', False)}\n"
        f"期望薪资：{s.get('min_salary', '')}–{s.get('max_salary', '')}K\n"
        f"个人要求与背景（节选）：\n{reqs or '无'}"
    )


def llm_process_jobs_batch(
    jobs: List[Dict[str, Any]],
    scene_id: Optional[int] = None,
) -> List[Dict[str, str]]:
    """
    批量筛选（每批最多 LLM_JOB_FILTER_BATCH_MAX 条）：结构化 JSON → 每条合成三行 ai_result；
    对匹配度为「高」「中」的岗位再批量生成招呼语（JSON items）。
    返回与 jobs 等长的 dict 列表，元素形如：
      {
        "match_level": "高/中/低",
        "reason": "...",
        "apply": "是/否",
        "hr_greeting": "..."  # 仅高/中且有 scene_id 时生成
      }
    """
    if not jobs:
        return []
    results: List[Dict[str, str]] = []
    for start in range(0, len(jobs), LLM_JOB_FILTER_BATCH_MAX):
        chunk = jobs[start : start + LLM_JOB_FILTER_BATCH_MAX]
        log.info(
            "🧠 批量 AI 筛选本批 %s 条岗位（offset=%s）",
            len(chunk),
            start,
        )
        filter_items = _call_filter_batch_structured(chunk)
        greet_indices = [
            i
            for i, it in enumerate(filter_items)
            if it.get("match_level") in ("高", "中")
        ]
        greetings: Dict[int, str] = {}
        if greet_indices and scene_id is not None:
            try:
                sc = _scene_context_for_greeting(int(scene_id))
                greetings = _call_greeting_batch_structured(sc, chunk, greet_indices)
            except Exception as e:
                log.warning("批量招呼语生成失败: %s", e)
        for i, it in enumerate(filter_items):
            hr = ""
            if it.get("match_level") in ("高", "中"):
                hr = greetings.get(i, "").strip()[:200]
            results.append(
                {
                    "match_level": str(it.get("match_level") or "低"),
                    "reason": str(it.get("reason") or "").strip(),
                    "apply": str(it.get("apply") or "否"),
                    "hr_greeting": hr,
                }
            )
    return results


def llm_process_job(
    job: Dict[str, Any],
    scene_id: Optional[int] = None,
) -> Dict[str, str]:
    log.info(f"🧠 AI 正在处理：{job['平台']} | {job['标题']} | {job['薪资']}")
    log.info("=" * 70)
    log.info(f"{job['平台']} | {job['标题']} | {job['薪资']}")
    return llm_process_jobs_batch([job], scene_id=scene_id)[0]


def llm_identify_scene(user_text, scenes):
    """
    输入历史场景文本，判断是否匹配原本场景
    :param user_text: Str
    :param scenes: List[Dict]
    :return: is_new(TRUE / FALSE), scene_id(isdigit)
    """
    if len(scenes) > 0:
        scene_list = "\n".join(
            [
                f"场景{s['scene_id']}：关键词={s['search_keywords']}, 城市={s['city']}, "
                f"省份={s.get('province', '')}, 远程={s['accept_remote']}, 薪资={s['min_salary']}-{s['max_salary']}"
                for s in scenes
            ]
        )
        scene_user = _SCENE_USER_TMPL.format(
            user_text=user_text,
            scene_list=scene_list,
        )
        match_result = chat_completion_text(
            [{"role": "user", "content": scene_user}],
            temperature=0.1,
        ).strip()
        if match_result.isdigit():
            return False, int(match_result)
    standard_result = chat_completion_text(
        [
            {"role": "system", "content": _STANDARD_SYSTEM},
            {"role": "user", "content": user_text},
        ],
        temperature=0.1,
        response_format={"type": "json_object"},
    )
    return True, standard_result
