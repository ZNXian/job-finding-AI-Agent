"""猎聘爬虫高级逻辑（Async 版）。

来源：按 `crawlers/liepin_legacy.py` 的组织方式拆出高级逻辑：
- 自动登录恢复（检测登录页→触发自动登录→重开列表 URL）
- 复杂分段 plan（city_code/pubTime 组合 + checkpoint 的 segment_index/last_list_page）
- 多页多段翻页 + 详情批处理

注意：
- legacy 文件不允许修改；本文件为 async 改写版（接口/命名尽量贴近 legacy）
- stealth 注入为页面级：每次 new_page 后必须 `await stealth_async(page)`
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
from services.job_store import is_crawl_list_url_present, upsert_crawl_list_job
from utils.browser import apply_anti_detect_init_scripts, get_browser, human_behavior
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


async def crawl_with_higher_logic(
    pw,
    browser,
    page,
    *,
    scene_id: Optional[int],
    reset_checkpoint: bool,
) -> List[Dict[str, Any]]:
    """对齐 legacy：构 plan → 断点续爬 → 列表页硬筛 → 详情批处理 → 写 checkpoint。"""
    crawl_scene_id = int(scene_id) if scene_id is not None else 0
    encoded_key = urllib.parse.quote(str(getattr(cfg, "SEARCH_KEYWORD", "") or "").strip())
    salaryCode = str(int(cfg.MIN_SALARY * 12 * 0.1)) + "$" + str(int(cfg.MAX_SALARY * 14 * 0.1))

    preferred = getattr(cfg, "PREFERRED_CITIES", None) or []
    if not isinstance(preferred, list):
        preferred = []
    province_name = str(getattr(cfg, "PROVINCE", "") or "").strip()
    plan: List[Dict[str, Any]] = []
    for dq in _dqs_for_pub30([str(x) for x in preferred], province_name) or ["010"]:
        plan.append({"city_code": dq, "pubTime": 30})

    seg_idx, list_start = get_liepin_list_resume(crawl_scene_id, plan, reset=reset_checkpoint)
    seg_idx = max(0, min(int(seg_idx), len(plan) - 1))
    current_page = int(list_start)
    max_page = int(getattr(cfg, "MAX_PAGE", 1) or 1)
    login_recovery_used = False

    reset_liepin_vlm_stats()
    final_jobs: List[Dict[str, Any]] = []

    while current_page < list_start + max_page:
        city_dq = str(plan[seg_idx].get("city_code"))
        pub_time = int(plan[seg_idx].get("pubTime", 30))
        list_url = (
            f"https://www.liepin.com/zhaopin/?city={city_dq}&dq={city_dq}"
            f"&pubTime={pub_time}&currentPage={current_page}&pageSize=40"
            f"&key={encoded_key}&salaryCode={salaryCode}"
        )
        log.info(f"list_url: {list_url}")
        try:
            await page.goto(list_url, timeout=60000, wait_until="domcontentloaded")
            await human_behavior(page)
        except TargetClosedError:
            log.info("检测到浏览器被手动关闭：停止翻页并返回已收集岗位数=%s", len(final_jobs))
            return final_jobs

        
        browser, page, login_recovery_used, ok, refetch = await _liepin_recover_list_login(
            pw, browser, page, login_recovery_used, list_url
        )
        if not ok:
            log.info("列表页提前退出：_liepin_recover_list_login ok=False url=%s", getattr(page, "url", None))
            break
        # if refetch:
        #     await _apply_stealth(page)
        job_list_box = await page.wait_for_selector(".job-list-box", timeout=10000)
        if not job_list_box:
            log.info("列表页提前退出：未找到 job_list_box(.job-list-box) url=%s", getattr(page, "url", None))
            break
  
        items = await job_list_box.query_selector_all(".job-card-pc-container")
        if not items:
            log.info(
                "列表页提前退出：job_list_box 下未找到 items(.job-card-pc-container) url=%s",
                getattr(page, "url", None),
            )
            break

        page_filter_pass_jobs: List[Dict[str, Any]] = []
        for card in items:
            try:
                a = await card.query_selector("a[href*='liepin.com']")
                if a:
                    href = await a.get_attribute("href")
                    href = (href or "").strip().replace("&amp;", "&")
                else:
                    log.warning(f"未提取到链接，卡片内容: {await card.inner_text()[:100]}")
                    # continue
    
                    log.debug(
                        "列表卡片跳过：href 为空（避免误写入 list_url）page_url=%s",
                        getattr(page, "url", None),
                    )
                    continue
                # 相对路径拼接为完整 URL
                link = urllib.parse.urljoin(page.url or "https://www.liepin.com/", href)
                # 兜底：极端情况下 urljoin 可能回退成当前列表页 URL，必须跳过
                if (link or "").strip() == (page.url or "").strip():
                    log.debug(
                        "列表卡片跳过：link 等于当前 page.url（疑似 href 异常）page_url=%s",
                        getattr(page, "url", None),
                    )
                    continue
                if not link:
                    log.debug(
                        "列表卡片跳过：link 为空 url=%s",
                        getattr(page, "url", None),
                    )
                    continue
                if is_crawl_list_url_present(LIEPIN_JOB_PLATFORM, crawl_scene_id, link):
                    log.debug(
                        "列表卡片跳过：已存在于 SQLite（去重）scene_id=%s link=%s",
                        crawl_scene_id,
                        link,
                    )
                    continue
                

                # 只保留 title / area / salary（更稳：一次性获取所有 .ellipsis-1）
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

                if not hard_filter(title, area, salary):
                    log.debug(
                        "列表卡片跳过：hard_filter 未通过 title=%s area=%s salary=%s link=%s",
                        title,
                        area,
                        salary,
                        link,
                    )
                    continue

                list_job = {
                    "平台": "猎聘",
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
                page_filter_pass_jobs.append(list_job)
            except Exception as e :
                log.debug(
                    "列表卡片跳过：解析 card 异常 url=%s",
                    getattr(page, "url", None),
                    exc_info=True,
                )
                log.info(f"{e}")
                continue

        # 详情批处理（异步顺序）
        kept: List[Dict[str, Any]] = []
        for job in page_filter_pass_jobs:
            # await _liepin_random_nav_delay()
            try:
                await page.goto(job["链接"], timeout=60000, wait_until="domcontentloaded")
                await human_behavior(page)
                # await _apply_stealth(page)
                page_text = await page.evaluate("() => document.body.innerText") or ""
                if not check_chatted(page_text):
                    continue
                vlm_on = bool(getattr(cfg, "VLM_ENABLED", False))
                if not vlm_on:
                    job["介绍"] = str((await extract_job_description(page)) or "").strip()
                else:
                    job["介绍"] = str((await resolve_job_introduction_text(page, job)) or "").strip()
                if not job["介绍"]:
                    #!注意,获取不到岗位详情很可能被风控,停止抓取并返回已收集的岗位数
                    log.info("风控风险,停止抓取并返回已收集岗位数=%s",len(final_jobs) + len(kept),)
                    final_jobs.extend(kept)
                    return final_jobs
                
                    # continue
                job["介绍"] = job["业务方向与规模"] + "\n" + job["介绍"]
                # 仅在详情页成功提取到“介绍”后写入 SQLite（否则视为不通过，不入库）
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
                kept.append(job)
            except TargetClosedError:
                log.info(
                    "检测到浏览器被手动关闭：停止详情抓取并返回已收集岗位数=%s",
                    len(final_jobs) + len(kept),
                )
                final_jobs.extend(kept)
                return final_jobs
            except Error:
                continue

        final_jobs.extend(kept)
        set_liepin_list_checkpoint(crawl_scene_id, plan, seg_idx, current_page)
        current_page += 1

    log_liepin_vlm_stats_summary()
    return final_jobs

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