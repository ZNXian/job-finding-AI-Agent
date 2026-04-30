"""猎聘爬虫高级编排逻辑（Async 版）。

核心流程：
- 初始化运行时（scene 配置、分段 plan、checkpoint 恢复）
- 列表页循环（组装 list_url、导航、登录恢复、卡片拉取）
- 列表硬筛（可见性/陷阱卡片过滤、链接规范化、平台岗位 ID 去重、hard_filter）
- 详情批处理（可聊校验、介绍提取、风控早停、写入 SQLite）
- 断点与收尾（每页写 checkpoint、汇总 VLM 统计）

稳定性与反反爬虫特点：
- 页面行为拟人化：`human_behavior`
- 反检测注入：`apply_anti_detect_init_scripts`（新页面先注入再访问）
- 登录态自愈：检测登录页后自动登录恢复并回到列表 URL
- 异常兜底：`TargetClosedError` 手动关窗可安全返回已收集结果
- 长跑策略：累计岗位达到阈值后重启浏览器上下文再继续爬取

说明：
- legacy 备份文件不修改；本文件为 async 主逻辑实现
- 仅负责“编排与容错”，基础浏览器能力在 `utils/browser.py`
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import random
import re
import sys
import time
import urllib.parse
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from playwright.async_api import Error
from playwright._impl._errors import TargetClosedError
from playwright_stealth import stealth_async

import config as cfg
from config import log
from crawlers.liepin_vlm import resolve_job_introduction_text, reset_liepin_vlm_stats, log_liepin_vlm_stats_summary
from services.job_store import (
    extract_liepin_platform_job_id,
    is_crawl_list_platform_job_id_present,
    normalize_liepin_link_keep_first_q,
    upsert_crawl_list_job,
)
from utils.browser import apply_anti_detect_init_scripts, get_browser, human_behavior, is_trap_job_card
from utils.crawl_checkpoint import get_liepin_list_resume, set_liepin_list_checkpoint
from utils.filter import hard_filter, check_chatted

LIEPIN_JOB_PLATFORM = "liepin"


LIEPIN_CITY_CODE: Dict[str, str] = {
"北京": "010",
    "上海": "020",
    "广东": "050",
    "珠海": "050140",
    "深圳": "050090",
    "广州": "050020",
    "中山": "050130",
    "广西": "110",
    "江苏": "060",
    "天津": "030",
    "重庆": "040",
    "苏州": "060080",
    "杭州": "070020",
    "南京": "060020",
    "成都": "280020",
    "武汉": "170020",
    "西安": "270020",
    "浙江": "070",
    "四川": "280",
    "湖北": "170",
    "山东": "250",
    "河北": "140",
    "河南": "150",
    "湖南": "180",
    "安徽": "080",
    "福建": "090",
    "辽宁": "210",
    "黑龙江": "160",
    "吉林": "190",
    "陕西": "270",
    "山西": "260",
    "江西": "200",
    "贵州": "120",
    "云南": "310",
    "海南": "130",
    "甘肃": "100",
    "青海": "240",
    "内蒙古": "220",
    "新疆": "300",
    "宁夏": "230",
    "西藏": "290",
    "香港": "320",
    "澳门": "330",
    "台湾": "340",
}


async def _apply_stealth(page) -> None:
    await stealth_async(page)


def _liepin_storage_state_path() -> Path:
    root = Path(__file__).resolve().parent.parent
    raw = getattr(cfg, "LIEPIN_STORAGE_STATE_PATH", None) or str(
        root / "browser_data" / "liepin_storage_state.json"
    )
    return Path(raw).expanduser().resolve()


def _liepin_storage_state_for_launch() -> str | None:
    p = _liepin_storage_state_path()
    try:
        if p.is_file() and p.stat().st_size > 0:
            return str(p)
    except OSError:
        pass
    return None


def _get_liepin_citycode(city) -> List[str]:
    if city is None:
        return []
    name = str(city).strip()
    if not name:
        return []
    code = LIEPIN_CITY_CODE.get(name)
    return [code] if code else []


def _dqs_for_pub30(preferred_name_list: List[str], province_name: str) -> List[str]:
    out: List[str] = []
    seen: set[str] = set()
    for n in preferred_name_list:
        for c in _get_liepin_citycode(n):
            if c and c not in seen:
                seen.add(c)
                out.append(c)
    if not out and province_name:
        for c in _get_liepin_citycode(province_name.strip()):
            if c and c not in seen:
                seen.add(c)
                out.append(c)
    return out


def _all_dq_from_preferred_cities_only(preferred_name_list: List[str]) -> List[str]:
    out: List[str] = []
    seen: set[str] = set()
    for n in preferred_name_list:
        for c in _get_liepin_citycode(n):
            if c and c not in seen:
                seen.add(c)
                out.append(c)
    return out


async def _is_liepin_login_page(page) -> bool:
    try:
        url = (page.url or "").lower()
    except Exception:
        return False
    if any(x in url for x in ("login", "signin", "passport", "openlogin")):
        return True
    if "/account/" in url and any(x in url for x in ("login", "sign", "bind")):
        return True
    try:
        snippet = await page.evaluate(
            "() => (document.body && document.body.innerText) ? document.body.innerText.slice(0, 4000) : ''"
        )
        snippet = snippet or ""
    except Exception:
        return False
    if "登录/注册" in snippet or "有异" in snippet:
        return True
    markers = ("手机号登录", "验证码登录", "扫码登录", "密码登录", "短信登录")
    if any(m in snippet for m in markers) and ("猎聘" in snippet or "liepin" in snippet.lower()):
        return True
    return False


# async def _liepin_random_nav_delay() -> None:
#     d = random.uniform(10.0, 20.0)
#     log.info("页面切换随机等待 %.1f 秒（反爬节奏）", d)
#     await asyncio.sleep(d)


async def _liepin_try_auto_relogin(pw) -> tuple:
    """Async：调用 crawlers.liepin_login_save_state.liepin_login，然后重启持久化上下文。"""
    root = Path(__file__).resolve().parent.parent
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from crawlers.liepin_login_save_state import liepin_login

    path = str(_liepin_storage_state_path())
    slider = float(getattr(cfg, "LIEPIN_LOGIN_SLIDER_WAIT_SEC", 45.0) or 45.0)
    user = (getattr(cfg, "LOGIN_USERNAME", None) or "").strip()
    pwd = (getattr(cfg, "LOGIN_PASSWORD", None) or "").strip()
    if not user or not pwd:
        log.error("自动登录需配置 LOGIN_USERNAME / LOGIN_PASSWORD（.env 或 config）")
        return None, None

    # login_save_state 是 async（已迁移）
    ok, msg = await liepin_login(user, pwd, path, slider_wait_sec=slider)
    if not ok:
        log.error("猎聘自动登录失败: %s", msg)
        return None, None

    nb = await get_browser(pw, headless=cfg.CRAWL_HEADLESS, storage_state=path)
    np = await nb.new_page()
    await apply_anti_detect_init_scripts(np)
    # await _apply_stealth(np)
    return nb, np


async def _liepin_recover_list_login(
    pw, browser, page, login_recovery_used: bool, list_url: str
):
    """列表/卡片解析过程中若当前页为登录页，尝试一次自动登录并重新打开 list_url。"""
    if not await _is_liepin_login_page(page):
        return browser, page, login_recovery_used, True, False
    if login_recovery_used:
        log.error("列表/卡片处理中仍为登录页且已用过一次自动登录恢复，停止本页")
        return browser, page, login_recovery_used, False, False
    log.warning("列表/卡片处理中检测到登录态失效，启动自动登录流程…")
    try:
        await browser.close()
    except Exception:
        pass
    browser, page = await _liepin_try_auto_relogin(pw)
    if browser is None:
        log.error("列表/卡片处理中自动登录失败，停止翻页")
        return browser, page, login_recovery_used, False, False
    login_recovery_used = True
    try:
        await page.goto(list_url, timeout=60000, wait_until="domcontentloaded")
    except TargetClosedError:
        log.info("检测到浏览器被手动关闭：停止登录恢复并返回已收集岗位")
        return browser, page, login_recovery_used, False, False
    except Exception as e:
        log.info("登录恢复后列表 URL 访问失败：%s，停止翻页", e)
        return browser, page, login_recovery_used, False, False
    # await _liepin_random_nav_delay()
    if await _is_liepin_login_page(page):
        log.error("登录恢复后列表仍为登录页，停止翻页")
        return browser, page, login_recovery_used, False, False
    return browser, page, login_recovery_used, True, True


async def _recover_login_and_load_list_cards(
    pw,
    browser,
    page,
    login_recovery_used: bool,
    list_url: str,
) -> Tuple[Any, Any, bool, Optional[List[Any]]]:
    """列表页登录恢复后拉取卡片；无法继续时返回 items=None（调用方应 break）。"""
    browser, page, login_recovery_used, ok, _refetch = await _liepin_recover_list_login(
        pw, browser, page, login_recovery_used, list_url
    )
    if not ok:
        log.info(
            "列表页提前退出：_liepin_recover_list_login ok=False url=%s",
            getattr(page, "url", None),
        )
        return browser, page, login_recovery_used, None
    items = await _get_list_cards(page)
    if not items:
        log.info(
            "列表页提前退出：job_list_box 下未找到 items(.job-card-pc-container) url=%s",
            getattr(page, "url", None),
        )
        return browser, page, login_recovery_used, None
    return browser, page, login_recovery_used, items


async def crawl_with_higher_logic(
    pw,
    browser,
    page,
    *,
    scene_id: Optional[int],
    reset_checkpoint: bool,
) -> List[Dict[str, Any]]:
    """对齐 legacy：构 plan → 断点续爬 → 列表页硬筛 → 详情批处理 → 写 checkpoint。"""
    crawl_scene_id, encoded_key, salary_code, plan, seg_idx, list_start, max_page = _init_crawl_runtime(
        scene_id=scene_id,
        reset_checkpoint=reset_checkpoint,
    )
    login_recovery_used = False

    reset_liepin_vlm_stats()
    final_jobs: List[Dict[str, Any]] = []
    restart_after_jobs = 30
    jobs_since_browser_restart = 0
    start_seg_idx = seg_idx
    last_seg_city_code: Optional[str] = None
    stop_all = False
    while seg_idx < len(plan) and not stop_all:
        # 进入每个「非偏好城市全集」segment 时重启一次浏览器（pubTime=7），降低长跑被风控概率
        seg_city_code = str(plan[seg_idx].get("city_code") or "").strip()
        seg_pub_time = int(plan[seg_idx].get("pubTime", 30) or 30)
        if seg_pub_time == 7 and seg_city_code and seg_city_code != last_seg_city_code:
            log.info("进入非偏好城市 segment（pubTime=7 city=%s），重启浏览器以继续爬取", seg_city_code)
            try:
                await browser.close()
            except Exception:
                pass
            storage_state = _liepin_storage_state_for_launch()
            browser = await get_browser(
                pw,
                headless=cfg.CRAWL_HEADLESS,
                storage_state=storage_state,
            )
            page = await browser.new_page()
            await apply_anti_detect_init_scripts(page)
            login_recovery_used = False
            jobs_since_browser_restart = 0
        last_seg_city_code = seg_city_code

        current_page = int(list_start) if seg_idx == start_seg_idx else 0
        effective_max_page = max(1, int(max_page))
        loop_upper_page = current_page + effective_max_page
        real_max_page_checked = False

        while current_page < loop_upper_page:
            list_url = _build_list_url(
                plan_item=plan[seg_idx],
                current_page=current_page,
                encoded_key=encoded_key,
                salary_code=salary_code,
            )
            log.info(
                "list_url: city=%s pubTime=%s keyword=%s page=%s url=%s",
                str(plan[seg_idx].get("city_code") or ""),
                str(plan[seg_idx].get("pubTime") or ""),
                str(plan[seg_idx].get("keyword") or ""),
                current_page,
                list_url,
            )
            kept: List[Dict[str, Any]] = []
            try:
                await page.goto(list_url, timeout=60000, wait_until="domcontentloaded")
                await human_behavior(page, d_long_use=False)
                browser, page, login_recovery_used, items = await _recover_login_and_load_list_cards(
                    pw, browser, page, login_recovery_used, list_url
                )
                if items is None:
                    stop_all = True
                    break
                if not real_max_page_checked:
                    real_max_page = await _get_liepin_max_page_async(page)
                    effective_max_page = min(max(1, int(max_page)), max(1, int(real_max_page)))
                    loop_upper_page = current_page + effective_max_page
                    real_max_page_checked = True

                page_filter_pass_jobs = await _collect_page_filter_pass_jobs(
                    items=items,
                    page=page,
                    crawl_scene_id=crawl_scene_id,
                )

                kept, stop_result = await _process_detail_jobs(
                    page=page,
                    browser=browser,
                    page_filter_pass_jobs=page_filter_pass_jobs,
                    crawl_scene_id=crawl_scene_id,
                    final_jobs=final_jobs,
                )
                if stop_result is not None:
                    return stop_result
            except TargetClosedError:
                log.info("检测到浏览器被手动关闭：停止翻页并返回已收集岗位数=%s", len(final_jobs))
                return final_jobs

            final_jobs.extend(kept)
            jobs_since_browser_restart += len(kept)
            if jobs_since_browser_restart >= restart_after_jobs:
                log.info(
                    "已累计 %s 条岗位，重启浏览器以继续爬取",
                    jobs_since_browser_restart,
                )
                try:
                    await browser.close()
                except Exception:
                    pass
                storage_state = _liepin_storage_state_for_launch()
                browser = await get_browser(
                    pw,
                    headless=cfg.CRAWL_HEADLESS,
                    storage_state=storage_state,
                )
                page = await browser.new_page()
                await apply_anti_detect_init_scripts(page)
                login_recovery_used = False
                jobs_since_browser_restart = 0
            set_liepin_list_checkpoint(crawl_scene_id, plan, seg_idx, current_page)
            current_page += 1

        seg_idx += 1
        list_start = 0

    log_liepin_vlm_stats_summary()
    return final_jobs


def _init_crawl_runtime(
    *,
    scene_id: Optional[int],
    reset_checkpoint: bool,
) -> Tuple[int, str, str, List[Dict[str, Any]], int, int, int]:
    """初始化本轮爬取上下文：scene/plan/断点/分页。"""
    crawl_scene_id = int(scene_id) if scene_id is not None else 0

    def _parse_keywords(raw: Any) -> List[str]:
        if raw is None:
            return []
        if isinstance(raw, list):
            items = [str(x).strip() for x in raw if str(x).strip()]
        else:
            s = str(raw or "").strip()
            if not s:
                return []
            # 优先按常见分隔符拆分；若无分隔符但包含空格，再按空格拆
            parts = [p.strip() for p in re.split(r"[，,、]+", s) if p.strip()]
            if len(parts) <= 1 and " " in s:
                parts = [p.strip() for p in s.split() if p.strip()]
            items = parts
        # 去重（保留顺序），并限制最多 3 个
        seen: set[str] = set()
        out: List[str] = []
        for it in items:
            if it in seen:
                continue
            seen.add(it)
            out.append(it)
            if len(out) >= 3:
                break
        return out

    keywords = _parse_keywords(getattr(cfg, "SEARCH_KEYWORD", "") or "")
    if not keywords:
        # 兜底：保持旧行为（至少有一个 key）
        kw = str(getattr(cfg, "SEARCH_KEYWORD", "") or "").strip()
        keywords = [kw] if kw else [""]
    encoded_key = urllib.parse.quote(str(keywords[0] or "").strip())
    salary_code = str(int(cfg.MIN_SALARY * 12 * 0.1)) + "$" + str(int(cfg.MAX_SALARY * 14 * 0.1))

    preferred = getattr(cfg, "PREFERRED_CITIES", None) or []
    if not isinstance(preferred, list):
        preferred = []
    province_name = str(getattr(cfg, "PROVINCE", "") or "").strip()
    base_segments: List[Dict[str, Any]] = []
    preferred_dqs = _dqs_for_pub30([str(x) for x in preferred], province_name) or ["010"]
    for dq in preferred_dqs:
        base_segments.append({"city_code": dq, "pubTime": 30})

    # ACCEPT_REMOTE=True 时：追加「非偏好城市全集」segment（pubTime=7）
    # 注意：此处以 LIEPIN_CITY_CODE 的 code 为全集，排除 preferred_dqs 中已覆盖的 code。
    accept_remote = bool(getattr(cfg, "ACCEPT_REMOTE", False))
    if accept_remote:
        seen: set[str] = set(str(x) for x in preferred_dqs if str(x))
        all_codes = sorted({str(v) for v in (LIEPIN_CITY_CODE or {}).values() if str(v).strip()})
        for code in all_codes:
            if code in seen:
                continue
            base_segments.append({"city_code": code, "pubTime": 7})

    # 关键：按 city_segment → keyword 的顺序展开 plan
    plan: List[Dict[str, Any]] = []
    for seg in base_segments:
        for kw in keywords:
            plan.append(
                {
                    "city_code": str(seg.get("city_code") or "").strip(),
                    "pubTime": int(seg.get("pubTime", 30) or 30),
                    "keyword": str(kw or "").strip(),
                }
            )

    seg_idx, list_start = get_liepin_list_resume(crawl_scene_id, plan, reset=reset_checkpoint)
    seg_idx = max(0, min(int(seg_idx), len(plan) - 1))
    max_page = int(getattr(cfg, "MAX_PAGE", 1) or 1)
    return crawl_scene_id, encoded_key, salary_code, plan, seg_idx, int(list_start), max_page


def _build_list_url(
    *,
    plan_item: Dict[str, Any],
    current_page: int,
    encoded_key: str,
    salary_code: str,
) -> str:
    city_dq = str(plan_item.get("city_code"))
    pub_time = int(plan_item.get("pubTime", 30))
    kw = str(plan_item.get("keyword") or "").strip()
    key_encoded = urllib.parse.quote(kw) if kw else str(encoded_key or "")
    return (
        f"https://www.liepin.com/zhaopin/?city={city_dq}&dq={city_dq}"
        f"&pubTime={pub_time}&currentPage={current_page}&pageSize=40"
        f"&key={key_encoded}&salaryCode={salary_code}"
    )


async def _get_liepin_max_page_async(page) -> int:
    """从分页栏读取真实最大页数（Antd 分页）；失败返回 1。"""
    try:
        await page.wait_for_selector(".ant-pagination", timeout=5000)
        max_page = await page.evaluate(
            """() => {
                const numButtons = document.querySelectorAll(
                    '.ant-pagination-item:not(.ant-pagination-prev):not(.ant-pagination-next):not(.ant-pagination-jump-prev):not(.ant-pagination-jump-next)'
                );
                if (!numButtons || numButtons.length === 0) return 1;
                const lastBtn = numButtons[numButtons.length - 1];
                const pageNum = (lastBtn && (lastBtn.title || lastBtn.innerText)) || '';
                const n = parseInt(pageNum, 10);
                return Number.isFinite(n) && n > 0 ? n : 1;
            }"""
        )
        max_page = int(max_page or 1)
        log.info("✅ 识别到真实最大页数：%s", max_page)
        return max(1, max_page)
    except Exception as e:
        log.warning("⚠️ 读取最大页数失败，默认爬 1 页：%s", e)
        return 1


async def _collect_page_filter_pass_jobs(
    *,
    items,
    page,
    crawl_scene_id: int,
) -> List[Dict[str, Any]]:
    """列表页卡片解析与过滤，返回候选岗位。"""
    page_filter_pass_jobs: List[Dict[str, Any]] = []
    for card in items:
        try:
            list_job = await _parse_and_filter_list_card(card, page, crawl_scene_id)
            if not list_job:
                continue
            page_filter_pass_jobs.append(list_job)
        except Exception as e:
            log.debug(
                "列表卡片跳过：解析 card 异常 url=%s",
                getattr(page, "url", None),
                exc_info=True,
            )
            log.info(f"{e}")
            continue
    return page_filter_pass_jobs


async def _process_detail_jobs(
    *,
    page,
    browser,
    page_filter_pass_jobs: List[Dict[str, Any]],
    crawl_scene_id: int,
    final_jobs: List[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], Optional[List[Dict[str, Any]]]]:
    """处理详情页批次；返回 (kept, stop_result)。stop_result 非空时应立即 return。"""
    kept: List[Dict[str, Any]] = []
    for job in page_filter_pass_jobs:
        try:
            if not await _load_job_detail_page(page, job):
                continue
            job["介绍"] = await _extract_job_intro(page, job)
            if not job["介绍"]:
                log.info(
                    "风控风险,停止抓取,关闭浏览器并返回已收集岗位数=%s",
                    len(final_jobs) + len(kept),
                )
                stop_result = list(final_jobs) + list(kept)
                await browser.close()
                return kept, stop_result

            job["介绍"] = "公司业务方向与规模:"+job["业务方向与规模"] + "\n" +"招聘详情(已截断):"+ job["介绍"]
            _persist_passed_job(job, crawl_scene_id)
            kept.append(job)
        except TargetClosedError:
            log.info(
                "检测到浏览器被手动关闭：停止详情抓取并返回已收集岗位数=%s",
                len(final_jobs) + len(kept),
            )
            stop_result = list(final_jobs) + list(kept)
            return kept, stop_result
        except Error:
            continue
    return kept, None


async def extract_job_description(page, startfrom=50, endto=500):
    """
    从猎聘岗位详情页提取工作介绍文本（仅 HTML；内部使用与 VLM 同源的原文抽取再截断）
    :param page: Playwright 详情页
    :return: 截取 [startfrom:endto] 的岗位介绍文本（无则返回空字符串）
    """
    # from crawlers.liepin_vlm import get_raw_job_intro_text_from_page

    # t = get_raw_job_intro_text_from_page(page)
    # if not t:
    #     return ""
    # return t[startfrom:endto] if len(t) > endto else t
    
    
    # AI 删除 已恢复AI删除
    # 删除原因：DOM 提取逻辑已集中到 crawlers.liepin_vlm.get_raw_job_intro_text_from_page，本函数仅保留截断以兼容可能的外部调用
    try:
        # job_intro_elem = page.query_selector('dl.job-intro-container dd[data-selector="job-intro-content"]')
        job_intro_elem = await page.query_selector(
            'section.job-intro-container > dl:first-child > dd'
        )
        if job_intro_elem:
            # 提取文本并清理冗余字符（&nbsp、多余换行/空格）
            job_desc = ((await job_intro_elem.inner_text()) or "").strip()
            # 1. 替换HTML空格符 &nbsp;
            job_desc = job_desc.replace("&nbsp;", " ").replace("&nbsp", " ")
            # 2. 合并多余换行/空格为单个空格
            job_desc = re.sub(r"\s+", " ", job_desc)
            # 3. 去除首尾无用字符
            job_desc = job_desc.strip('"').strip("'").strip()
            return job_desc[startfrom:endto] if len(job_desc) > endto else job_desc
        else:
            log.critical(f"⚠️ 未获取到岗位详情")
            return ""
    except Exception as e:
        log.critical(f"提取岗位详情失败：{str(e)}")
        return ""


async def _load_job_detail_page(page, job: Dict[str, Any]) -> bool:
    """详情页打开 + 人类行为模拟 + 聊天过滤。"""
    await page.goto(job["链接"], timeout=60000, wait_until="domcontentloaded")
    await human_behavior(page)
    page_text = await page.evaluate("() => document.body.innerText") or ""
    return bool(check_chatted(page_text))


async def _extract_job_intro(page, job: Dict[str, Any]) -> str:
    """按 VLM 开关提取详情介绍文本。"""
    vlm_on = bool(getattr(cfg, "VLM_ENABLED", False))
    if not vlm_on:
        return str((await extract_job_description(page)) or "").strip()
    return str((await resolve_job_introduction_text(page, job)) or "").strip()


def _persist_passed_job(job: Dict[str, Any], crawl_scene_id: int) -> None:
    """将通过详情校验的岗位写入 SQLite。"""
    try:
        upsert_crawl_list_job(LIEPIN_JOB_PLATFORM, crawl_scene_id, job)
        log.info(
            "详情岗位写入 SQLite: scene_id=%s title=%s url=%s",
            crawl_scene_id,
            str((job or {}).get("标题", ""))[:60],
            str((job or {}).get("链接", ""))[:200],
        )
    except Exception as ex:
        log.debug("写入详情岗位到 SQLite 失败: %s", ex)


async def _get_list_cards(page):
    """获取当前列表页岗位卡片集合；找不到容器或卡片时返回空列表。"""
    job_list_box = await page.wait_for_selector(".job-list-box", timeout=10000)
    if not job_list_box:
        return []
    return await job_list_box.query_selector_all(".job-card-pc-container")


async def _list_card_validated_job_link(
    card,
    page,
    crawl_scene_id: int,
) -> Optional[Tuple[str, str]]:
    """解析卡片链接并完成平台岗位ID提取与去重；通过返回 (link, platform_job_id)。"""
    a = await card.query_selector("a[href*='liepin.com']")
    if a:
        href = await a.get_attribute("href")
        href = (href or "").strip().replace("&amp;", "&")
    else:
        card_text = ((await card.inner_text()) or "").strip()
        log.warning("未提取到链接，卡片内容: %s", card_text[:100])
        log.debug(
            "列表卡片跳过：href 为空（避免误写入 list_url）page_url=%s",
            getattr(page, "url", None),
        )
        return None

    raw_link = urllib.parse.urljoin(page.url or "https://www.liepin.com/", href)
    link = normalize_liepin_link_keep_first_q(raw_link)
    if (link or "").strip() == (page.url or "").strip():
        log.debug(
            "列表卡片跳过：link 等于当前 page.url（疑似 href 异常）page_url=%s",
            getattr(page, "url", None),
        )
        return None
    if not link:
        log.debug(
            "列表卡片跳过：link 为空 url=%s",
            getattr(page, "url", None),
        )
        return None
    platform_job_id = extract_liepin_platform_job_id(link)
    if not platform_job_id:
        log.debug(
            "列表卡片跳过：未匹配到平台岗位ID link=%s",
            link,
        )
        return None
    if is_crawl_list_platform_job_id_present(LIEPIN_JOB_PLATFORM, crawl_scene_id, platform_job_id):
        log.debug(
            "列表卡片跳过：已存在于 SQLite（按平台岗位ID去重）scene_id=%s platform_job_id=%s",
            crawl_scene_id,
            platform_job_id,
        )
        return None
    return link, platform_job_id


async def _list_card_listing_fields(card) -> Tuple[str, str, str, str, str]:
    """从列表卡片 DOM 提取 title / area / company / 业务方向与规模 / salary。"""
    ellipsis_elements = await card.query_selector_all(".ellipsis-1")

    title = ""
    if ellipsis_elements:
        title_el = ellipsis_elements[0]
        title = (await title_el.get_attribute("title")) or ""
        title = title.strip()
        if not title:
            title = ((await title_el.inner_text()) or "").strip()

    area = ""
    if len(ellipsis_elements) >= 2:
        area = ((await ellipsis_elements[1].inner_text()) or "").strip()

    company = ""
    if len(ellipsis_elements) >= 3:
        company = ((await ellipsis_elements[2].inner_text()) or "").strip()

    biz_scale = ""
    if len(ellipsis_elements) >= 4:
        spans = await ellipsis_elements[3].query_selector_all("span")
        parts: List[str] = []
        for sp in spans:
            t = ((await sp.inner_text()) or "").strip()
            if t:
                parts.append(t)
        biz_scale = " ".join(parts)

    salary_el = await card.query_selector("span:has-text('k'), span:has-text('薪')")
    salary = ((await salary_el.inner_text()) or "").strip() if salary_el else ""

    return title, area, company, biz_scale, salary


async def _parse_and_filter_list_card(
    card,
    page,
    crawl_scene_id: int,
) -> Optional[Dict[str, Any]]:
    """单卡片解析 + 去重 + hard_filter；通过返回 list_job，否则返回 None。"""
    if await is_trap_job_card(card):
        log.debug("列表卡片跳过：陷阱/不可见卡片")
        return None

    validated = await _list_card_validated_job_link(card, page, crawl_scene_id)
    if validated is None:
        return None
    link, platform_job_id = validated

    title, area, company, biz_scale, salary = await _list_card_listing_fields(card)

    if not hard_filter(title, area, salary):
        log.debug(
            "列表卡片跳过：hard_filter 未通过 title=%s area=%s salary=%s link=%s",
            title,
            area,
            salary,
            link,
        )
        return None

    return {
        "平台": "猎聘",
        "platform": "liepin",
        "platform_job_id": platform_job_id,
        "标题": title,
        "公司": company,
        "薪资": salary,
        "地点": area,
        "业务方向与规模": biz_scale,
        "工作年限": "",
        "链接": link,
        "介绍": "",
        "scene_id": crawl_scene_id,
    }