# -*- coding: utf-8 -*-
# AI 生成
# 生成目的：猎聘岗位详情「截屏 + Qwen-VL」与「HTML 解析」统一为五字段 dict，并序列化到 job["介绍"]；含性能与 VLM 调用统计
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from config import log
import config as cfg
from services.vlm_services import (
    extract_intro_five_from_image,
    is_nonempty_intro_five,
    normalize_intro_five_dict,
)

# AI 生成
# 生成目的：项目根目录，用于相对路径 screenshots
_ROOT = Path(__file__).resolve().parent.parent

# AI 生成
# 生成目的：并发安全（未来若多 worker 可复用统计）
_liepin_vlm_stats_lock = threading.RLock()
_liepin_vlm_stats: Dict[str, Any] = {
    # 最终由 HTML 路径产出的介绍（含 VLM 失败后的 vlm_fallback）
    "html_path_count": 0,
    "html_path_ms_sum": 0.0,
    # 最终由 VLM 成功产出的介绍
    "vlm_path_count": 0,
    "vlm_path_ms_sum": 0.0,
    # 进入 VLM 分支的岗位数、VLM 成功/走 fallback 次数
    "vlm_branch_jobs": 0,
    "vlm_fallback_count": 0,
    # 多模态 API 调用次数、解析成功/失败
    "vlm_api_calls": 0,
    "vlm_api_ok": 0,
    "vlm_api_error": 0,
}


def get_liepin_vlm_stats() -> Dict[str, Any]:
    # AI 生成
    # 生成目的：对外只读副本，供日志或调试用
    with _liepin_vlm_stats_lock:
        return {k: v for k, v in _liepin_vlm_stats.items()}


def reset_liepin_vlm_stats() -> None:
    # AI 生成
    # 生成目的：每轮 _crawl_liepin 开始前清零，使 statistics 为「本轮」而非累计
    with _liepin_vlm_stats_lock:
        _liepin_vlm_stats.update(
            {
                "html_path_count": 0,
                "html_path_ms_sum": 0.0,
                "vlm_path_count": 0,
                "vlm_path_ms_sum": 0.0,
                "vlm_branch_jobs": 0,
                "vlm_fallback_count": 0,
                "vlm_api_calls": 0,
                "vlm_api_ok": 0,
                "vlm_api_error": 0,
            }
        )


def log_liepin_vlm_stats_summary() -> None:
    # AI 生成
    # 生成目的：一轮爬取结束后打一条汇总（% 具名占位符的键必须与入参 dict 键完全一致，否则 logging 会 KeyError）
    s = get_liepin_vlm_stats()
    ha = s["html_path_ms_sum"] / s["html_path_count"] if s["html_path_count"] else 0.0
    va = s["vlm_path_ms_sum"] / s["vlm_path_count"] if s["vlm_path_count"] else 0.0
    log.info(
        "猎聘VLM性能统计: html_path_count=%(html_path_count)s html_avg_ms=%(ha)s "
        "vlm_path_count=%(vlm_path_count)s vlm_avg_ms=%(va)s "
        "vlm_branch_jobs=%(vlm_branch_jobs)s vlm_fallback_count=%(vlm_fallback_count)s "
        "vlm_api_calls=%(vlm_api_calls)s vlm_api_ok=%(vlm_api_ok)s vlm_api_error=%(vlm_api_error)s",
        {**s, "ha": ha, "va": va},
    )


def _bump(
    key: str,
    *,
    by_int: int = 0,
    by_float: float = 0.0,
) -> None:
    with _liepin_vlm_stats_lock:
        if by_int:
            _liepin_vlm_stats[key] = int(_liepin_vlm_stats.get(key, 0)) + by_int
        if by_float:
            _liepin_vlm_stats[key] = float(_liepin_vlm_stats.get(key, 0.0)) + by_float


def _record_path_ms(path: str, ms: float) -> None:
    if path == "html":
        _bump("html_path_count", by_int=1)
        _bump("html_path_ms_sum", by_float=ms)
    else:
        _bump("vlm_path_count", by_int=1)
        _bump("vlm_path_ms_sum", by_float=ms)


def get_raw_job_intro_text_from_page(page) -> str:
    # AI 生成
    # 生成目的：从猎聘详情页 DOM 抽取岗位介绍纯文本（与原 extract_job_description 逻辑一致、不截断，供五字段与 HTML 路径用）
    try:
        job_intro_elem = page.query_selector(
            'dl.job-intro-container dd[data-selector="job-intro-content"]',
        )
        if job_intro_elem:
            job_desc = job_intro_elem.inner_text().strip()
            job_desc = job_desc.replace("&nbsp;", " ").replace("&nbsp", " ")
            job_desc = re.sub(r"\s+", " ", job_desc)
            job_desc = job_desc.strip('"').strip("'").strip()
            return job_desc
        log.info("未获取到岗位介绍 DOM（dd[data-selector=job-intro-content]）")
        return ""
    except Exception as e:
        log.error("get_raw_job_intro_text_from_page 失败：%s", e)
        return ""


def build_intro_dict_from_html(job: Dict[str, Any], raw_intro: str) -> Dict[str, Any]:
    # AI 生成
    # 生成目的：HTML 路径下五字段与 VLM 同构；列表页已有关键字放入 title/salary
    t = (job or {}).get("标题") or ""
    s = (job or {}).get("薪资") or ""
    req: List[str] = []
    if raw_intro and raw_intro.strip():
        req = [raw_intro.strip()]
    return normalize_intro_five_dict(
        {"title": t, "salary": s, "skills": [], "requirements": req, "benefits": []}
    )


def format_intro_dict_to_liepin_text(d: Dict[str, Any]) -> str:
    # AI 生成
    # 生成目的：与 llm_process_job 使用同一「岗位介绍」长文本格式；VLM 经 vlm_services（OpenAI 兼容）与 HTML 输出一致
    o = normalize_intro_five_dict(d)
    parts: List[str] = []
    if o.get("title"):
        parts.append(f"【职位】{o['title']}")
    if o.get("salary"):
        parts.append(f"【薪资】{o['salary']}")
    if o.get("skills"):
        parts.append("【技能】" + "、".join(o["skills"]))
    if o.get("requirements"):
        r = o["requirements"]
        if len(r) == 1 and len(r[0]) > 200:
            parts.append("【岗位要求与职责】\n" + r[0])
        else:
            parts.append("【岗位要求与职责】\n" + "\n".join(f"- {x}" for x in r if x))
    if o.get("benefits"):
        parts.append("【福利】" + "、".join(o["benefits"]))
    return "\n\n".join(parts).strip() if parts else (o["requirements"][0] if o.get("requirements") else "")


def make_job_screenshot_id(job: Dict[str, Any]) -> str:
    # AI 生成
    # 生成目的：截图文件名不冲突且可回溯岗位链接
    link = (job or {}).get("链接") or job.get("url") or "unknown"
    return hashlib.md5(str(link).encode("utf-8")).hexdigest()[:20]


# AI 生成
# 生成目的：实际调用在 services.vlm_services（langchain_openai + 百炼 compatible-mode）；此处仅重试与统计
def extract_by_vlm(image_path: Union[str, Path]) -> Dict[str, Any]:
    p = Path(image_path)
    if not p.is_file():
        log.warning("extract_by_vlm: 文件不存在 %s", image_path)
        return {}
    if not (getattr(cfg, "DASHSCOPE_API_KEY", None) or "").strip():
        log.error("extract_by_vlm: DASHSCOPE_API_KEY 未配置")
        return {}
    os.environ["DASHSCOPE_API_KEY"] = (cfg.DASHSCOPE_API_KEY or "").strip()
    last_err: Optional[Exception] = None
    max_attempts = 3
    for attempt in range(max_attempts):
        with _liepin_vlm_stats_lock:
            _liepin_vlm_stats["vlm_api_calls"] = int(_liepin_vlm_stats.get("vlm_api_calls", 0)) + 1
        try:
            d = extract_intro_five_from_image(p)
        except Exception as ex:
            last_err = ex
            log.warning("VLM 调用异常(attempt %s/3): %s", attempt + 1, ex)
            with _liepin_vlm_stats_lock:
                _liepin_vlm_stats["vlm_api_error"] = int(
                    _liepin_vlm_stats.get("vlm_api_error", 0)
                ) + 1
            continue
        # AI 生成
        # 生成目的：在日志中输出当次 VLM 解析结果（与是否通过五字段校验无关，便于调参/排查截屏与模型返回）
        try:
            log.info(
                "VLM 返回(原始五字段) attempt %s/3: %s",
                attempt + 1,
                json.dumps(d, ensure_ascii=False, default=str),
            )
        except (TypeError, ValueError):
            log.info("VLM 返回(原始五字段) attempt %s/3: %r", attempt + 1, d)
        if d and is_nonempty_intro_five(d):
            with _liepin_vlm_stats_lock:
                _liepin_vlm_stats["vlm_api_ok"] = int(_liepin_vlm_stats.get("vlm_api_ok", 0)) + 1
            return d
        with _liepin_vlm_stats_lock:
            _liepin_vlm_stats["vlm_api_error"] = int(
                _liepin_vlm_stats.get("vlm_api_error", 0)
            ) + 1
        last_err = ValueError("VLM 返回空或五字段无有效信息")
    if last_err is not None:
        log.info("VLM 失败原因(最后一次): %s", last_err)
    return {}


# AI 生成
# 生成目的：进详情后 networkidle 截主容器，否则全页
def take_screenshot(page, job_id: str, save_dir: str = "screenshots") -> str:
    try:
        page.wait_for_load_state("networkidle", timeout=30_000)
    except Exception as e:
        log.warning("wait_for_load_state networkidle: %s", e)
    base = _ROOT / save_dir
    base.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^\w.\-]+", "_", str(job_id))[:80] or "job"
    out_path = base / f"liepin_{safe}_{int(time.time() * 1000)}.png"
    # AI 生成
    # 生成目的：猎聘详情常见根节点（多选一，找不到则全页长图）
    main_selectors = [
        "div.job-apply-container",
        "div.job-view-box",
        "div.job-view-pc",
        "dl.job-intro-container",
    ]
    for sel in main_selectors:
        el = page.query_selector(sel)
        if not el:
            continue
        try:
            el.screenshot(path=str(out_path))
            log.info("已截取岗位主容器: selector=%s path=%s", sel, out_path)
            return str(out_path.resolve())
        except Exception as e:
            log.warning("元素截图失败 %s: %s，将尝试下一条或全页", sel, e)
    try:
        page.screenshot(path=str(out_path), full_page=True)
        log.info("已全页截屏: %s", out_path)
    except Exception as e:
        log.error("全页截屏失败: %s", e)
        raise
    return str(out_path.resolve())


def resolve_job_introduction_text(
    page,
    job: Dict[str, Any],
) -> str:
    # AI 生成
    # 生成目的：详情单岗位统一出口；VLM 关时只走 extract_job_description；VLM 开时 get_raw 供截屏/失败回退
    vlm_on = bool(getattr(cfg, "VLM_ENABLED", False))
    t_html_start = time.perf_counter()

    raw_html = get_raw_job_intro_text_from_page(page)
    t_html = (time.perf_counter() - t_html_start) * 1000.0

    # if not vlm_on:
    #     t_build_start = time.perf_counter()
    #     d = build_intro_dict_from_html(job, raw_html)
    #     intro = format_intro_dict_to_liepin_text(d)
    #     t_build = (time.perf_counter() - t_build_start) * 1000.0
    #     _record_path_ms("html", t_html + t_build)
    #     log.info(
    #         "liepin_detail timing path=html total_ms=%.1f (dom_ms=%.1f build_ms=%.1f) title=%s",
    #         t_html + t_build,
    #         t_html,
    #         t_build,
    #         (job or {}).get("标题", "")[:32],
    #     )
    #     return intro

    _bump("vlm_branch_jobs", by_int=1)
    t_vlm0 = time.perf_counter()
    j_id = make_job_screenshot_id(job)
    try:
        shot_path = take_screenshot(page, j_id, save_dir="screenshots")
    except Exception as e:
        log.warning("vlm_fallback: 截屏失败 %s，改用 HTML 解析", e, extra={"reason": "screenshot_failed"})
        _bump("vlm_fallback_count", by_int=1)
        t_fb = time.perf_counter()
        d = build_intro_dict_from_html(job, raw_html)
        t_fb_m = (time.perf_counter() - t_fb) * 1000.0
        _record_path_ms("html", t_html + t_fb_m)
        log.info(
            "vlm_fallback shot_fail path=html total_ms=%.1f title=%s",
            t_html + t_fb_m,
            (job or {}).get("标题", "")[:32],
        )
        return format_intro_dict_to_liepin_text(d)

    t_vlm_api = (time.perf_counter() - t_vlm0) * 1000.0
    t_api0 = time.perf_counter()
    vlm_d = extract_by_vlm(shot_path)
    t_vlm_end = (time.perf_counter() - t_api0) * 1000.0
    vlm_ms_for_record = t_vlm_api + t_vlm_end

    if is_nonempty_intro_five(vlm_d):
        intro = format_intro_dict_to_liepin_text(vlm_d)
        _record_path_ms("vlm", vlm_ms_for_record)
        log.info(
            "liepin_detail timing path=vlm total_ms=%.1f (shot+api) title=%s",
            vlm_ms_for_record,
            (job or {}).get("标题", "")[:32],
        )
        return intro

    # AI 生成
    # 生成目的：VLM 无有效内容时降级为 HTML
    log.warning(
        "vlm_fallback: VLM 无有效五字段，改用 HTML 解析",
        extra={"tag": "vlm_fallback", "title": (job or {}).get("标题", "")},
    )
    _bump("vlm_fallback_count", by_int=1)
    t_fb2 = time.perf_counter()
    d = build_intro_dict_from_html(job, raw_html)
    t_fb2_m = (time.perf_counter() - t_fb2) * 1000.0
    _record_path_ms("html", t_html + t_fb2_m)
    log.info(
        "vlm_fallback after_vlm path=html dom_ms=%.1f build_ms=%.1f vlm_shot_ms=%.1f vlm_api_ms=%.1f title=%s",
        t_html,
        t_fb2_m,
        t_vlm_api,
        t_vlm_end,
        (job or {}).get("标题", "")[:32],
    )
    return format_intro_dict_to_liepin_text(d)
