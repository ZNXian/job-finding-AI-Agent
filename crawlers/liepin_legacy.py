# -*- coding:utf-8 -*-
# @CreatTime : 2026 19:39
# @Author : XZN
import concurrent.futures
import random
import re
import sys
import time
import urllib.parse
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from playwright.sync_api import Error, sync_playwright

import config as cfg
from config import log
from services.job_store import (
    is_crawl_list_url_present,
    upsert_crawl_list_job,
)
from utils.browser import BROWSER_USER_DATA_DIR, get_browser, wait_for_browser_close
from utils.crawl_checkpoint import (
    get_liepin_list_resume,
    remove_scene_checkpoint,
    set_liepin_list_checkpoint,
)
from utils.filter import *
from crawlers.liepin_vlm import (
    log_liepin_vlm_stats_summary,
    reset_liepin_vlm_stats,
    resolve_job_introduction_text,
)

LIEPIN_JOB_PLATFORM = "liepin"

# Playwright Stealth（可选依赖）：在访问任何页面之前调用，减少自动化特征
# 注意：本爬虫使用 playwright.sync_api；因此需要 stealth_sync（而非仅提供 stealth_async 的版本）。
try:
    from playwright_stealth import stealth_sync  # type: ignore
except Exception:  # pragma: no cover
    stealth_sync = None
try:
    from playwright_stealth import stealth_async  # type: ignore
except Exception:  # pragma: no cover
    stealth_async = None


def _apply_stealth_if_available(page) -> None:
    if stealth_sync is None:
        # 只安装了 stealth_async 的情况下，sync Playwright 无法 await 注入；给出提示并继续爬取。
        if stealth_async is not None:
            log.warning(
                "检测到 playwright_stealth.stealth_async，但当前爬虫为 sync_playwright；"
                "请安装支持 stealth_sync 的 playwright-stealth 版本，或将爬虫迁移到 async_playwright。"
            )
        return
    try:
        stealth_sync(page)
    except Exception as e:
        log.debug("stealth_sync 失败（可忽略继续爬取）: %s", e)


# AI 生成
# 生成目的：VLM_ENABLED=False 时若连续多次无法提取岗位介绍，视为账号可能被风控，提前退出并返回已抓取结果
_LIEPIN_EMPTY_INTRO_STREAK = 0


def _liepin_storage_state_path() -> Path:
    # AI 生成
    # 生成目的：与 liepin_login、config 共用同一 storage_state 文件路径
    root = Path(__file__).resolve().parent.parent
    raw = getattr(cfg, "LIEPIN_STORAGE_STATE_PATH", None) or str(
        root / "browser_data" / "liepin_storage_state.json"
    )
    return Path(raw).expanduser().resolve()


def _liepin_storage_state_for_launch() -> str | None:
    # AI 生成
    # 生成目的：存在非空 storage_state 文件时供 launch_persistent_context 合并，否则匿名打开
    p = _liepin_storage_state_path()
    try:
        if p.is_file() and p.stat().st_size > 0:
            return str(p)
    except OSError:
        pass
    return None


def login_liepin(timeout: int = 120):
    """打开猎聘首页，使用与爬虫相同的用户目录，便于保存登录态。"""
    wait_for_browser_close(
        "https://www.liepin.com/",
        timeout,
        user_data_dir=BROWSER_USER_DATA_DIR,
    )
    log.info("猎聘登录流程结束")


def crawl_liepin(
    scene_id: Optional[int] = None,
    reset_checkpoint: bool = False,
):
    # AI 生成
    # 生成目的：每次接口爬取重置「连续空岗位介绍」计数，避免跨请求误判风控
    global _LIEPIN_EMPTY_INTRO_STREAK
    _LIEPIN_EMPTY_INTRO_STREAK = 0
    with sync_playwright() as p:
        jobs = []
        log.info("猎聘爬虫：无头模式（后台）" if cfg.CRAWL_HEADLESS else "猎聘爬虫：显示浏览器窗口")
        ss = _liepin_storage_state_for_launch()
        if ss:
            log.info("猎聘启动：检测到 storage_state，将合并打开持久化上下文 path=%s", ss)
        else:
            log.info("猎聘启动：无有效 storage_state 文件，匿名打开持久化上下文")
        browser = get_browser(
            p,
            headless=cfg.CRAWL_HEADLESS,
            storage_state=ss,
        )
        page = browser.new_page()
        _apply_stealth_if_available(page)  # 在访问任何页面之前执行
        try:
            jobs, browser = _crawl_liepin(
                p,
                browser,
                page,
                scene_id=scene_id,
                reset_checkpoint=reset_checkpoint,
            )
        except Exception as e:
            error_msg = str(e).lower()
            if any(kw in error_msg for kw in ["closed", "detached", "target", "context"]):
                if not getattr(cfg, "CRAWL_HEADLESS", True):
                    log.warning("手动关闭浏览器，正常结束爬取（返回已收集岗位）")
                else:
                    log.warning("浏览器已关闭，返回当前已收集的数据")
                if hasattr(_crawl_liepin, "last_collected"):
                    jobs = _crawl_liepin.last_collected
                    log.info(f"已返回 {len(jobs)} 个岗位")
                else:
                    jobs = []
            else:
                log.error(f"爬取失败: {e}")
                jobs = []
        finally:
            try:
                _liepin_safe_export_storage_state(browser)
            except Exception:
                pass
            try:
                browser.close()
            except Exception:
                pass

        log.info(f"可处理岗位：{len(jobs)}")
        return jobs

def _liepin_try_auto_relogin(p) -> tuple:
    # AI 生成
    # 生成目的：调用 scripts.liepin_login_save_state.liepin_login，并用新 storage_state 重启持久化浏览器
    # liepin_login 内部使用独立 sync_playwright；若在 crawl_liepin 外层 with sync_playwright 同线程调用会嵌套失败，
    # 故在单独线程中执行自动登录脚本（不修改 liepin_login_save_state 实现）。
    root = Path(__file__).resolve().parent.parent
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from scripts.liepin_login_save_state import liepin_login

    path = str(_liepin_storage_state_path())
    slider = float(getattr(cfg, "LIEPIN_LOGIN_SLIDER_WAIT_SEC", 45.0) or 45.0)
    user = (getattr(cfg, "LOGIN_USERNAME", None) or "").strip()
    pwd = (getattr(cfg, "LOGIN_PASSWORD", None) or "").strip()
    if not user or not pwd:
        log.error("自动登录需配置 LOGIN_USERNAME / LOGIN_PASSWORD（.env 或 config）")
        return None, None
    if not (getattr(cfg, "captcha_api_key", None) or "").strip():
        log.error("自动登录需配置 captcha_api_key（腾讯云/极验验证码）")
        return None, None

    def _run_liepin_login():
        return liepin_login(user, pwd, path, slider_wait_sec=slider)

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            ok, msg = pool.submit(_run_liepin_login).result()
    except Exception as e:
        log.error("猎聘自动登录（独立线程）执行异常: %s", e)
        return None, None
    if not ok:
        log.error("猎聘自动登录失败: %s", msg)
        return None, None
    log.info("猎聘自动登录成功: %s", msg)
    nb = get_browser(
        p,
        headless=cfg.CRAWL_HEADLESS,
        storage_state=path,
    )
    np = nb.new_page()
    _apply_stealth_if_available(np)  # 在访问任何页面之前执行
    return nb, np


# AI 生成
# 生成目的：封装列表/翻页/卡片解析中的「检测登录页 → 最多一次自动登录 → 重开 list_url → 必要时通知调用方重新 query 岗位卡片」
def _liepin_recover_list_login(
    p, browser, page, login_recovery_used: bool, list_url: str
):
    """列表/卡片解析过程中若当前页为登录页，尝试一次自动登录并重新打开 list_url。
    返回 (browser, page, login_recovery_used, ok_continue, refetch_items)。
    refetch_items=True 表示已重新进入列表，调用方须重新 query_selector_all 岗位卡片。"""
    if not _is_liepin_login_page(page):
        return browser, page, login_recovery_used, True, False
    if login_recovery_used:
        log.error("列表/卡片处理中仍为登录页且已用过一次自动登录恢复，停止本页")
        return browser, page, login_recovery_used, False, False
    log.warning("列表/卡片处理中检测到登录态失效，启动自动登录流程…")
    try:
        browser.close()
    except Exception:
        pass
    browser, page = _liepin_try_auto_relogin(p)
    if browser is None:
        log.error("列表/卡片处理中自动登录失败，停止翻页")
        browser = get_browser(
            p,
            headless=cfg.CRAWL_HEADLESS,
            storage_state=_liepin_storage_state_for_launch(),
        )
        page = browser.new_page()
        _apply_stealth_if_available(page)  # 在访问任何页面之前执行
        return browser, page, login_recovery_used, False, False
    login_recovery_used = True
    try:
        page.goto(list_url, timeout=60000)
    except Exception as e:
        log.info("登录恢复后列表 URL 访问失败：%s，停止翻页", e)
        return browser, page, login_recovery_used, False, False
    _liepin_random_nav_delay()
    if _is_liepin_login_page(page):
        log.error("登录恢复后列表仍为登录页，停止翻页")
        return browser, page, login_recovery_used, False, False
    try:
        _liepin_export_storage_state(browser)
    except Exception:
        pass
    return browser, page, login_recovery_used, True, True


# AI 生成
# 生成目的：列表上下文下任意时刻（含 page.goto 列表 URL 之后、或列表内异步重定向）统一先检验是否登录页再打日志，再调用 _liepin_recover_list_login 恢复
def _liepin_verify_list_login_and_recover(
    p, browser, page, login_recovery_used, list_url: str, context: str
):
    if _is_liepin_login_page(page):
        try:
            u = (page.url or "")[:200]
        except Exception:
            u = ""
        log.warning("跳转/会话后登录页检验 [%s]：命中猎聘登录页，url=%s", context, u)
    return _liepin_recover_list_login(p, browser, page, login_recovery_used, list_url)


# AI 生成
# 生成目的：结合当前 URL 与页面正文片段识别猎聘登录页，供列表/卡片/详情等多处触发自动登录恢复时复用
def _is_liepin_login_page(page) -> bool:
    """判断当前页是否为登录页或未登录态（含列表首屏仅出现「登录/注册」入口的情况）。"""
    try:
        url = (page.url or "").lower()
    except Exception:
        return False
    if any(x in url for x in ("login", "signin", "passport", "openlogin")):
        return True
    if "/account/" in url and any(x in url for x in ("login", "sign", "bind")):
        return True
    try:
        snippet = page.evaluate(
            "() => (document.body && document.body.innerText) ? document.body.innerText.slice(0, 4000) : ''"
        ) or ""
    except Exception:
        return False
    # 招聘列表/首页未登录：顶栏常见「登录/注册」，URL 仍可能为 zhaopin 而非 passport
    if "登录/注册" in snippet:
        return True
    if "有异" in snippet:
        return True
    markers = ("手机号登录", "验证码登录", "扫码登录", "密码登录", "短信登录")
    if any(m in snippet for m in markers) and ("猎聘" in snippet or "liepin" in snippet.lower()):
        return True
    return False


def _liepin_export_storage_state(browser) -> None:
    # AI 生成
    # 生成目的：将当前持久化上下文的 cookies 等写入 LIEPIN_STORAGE_STATE_PATH（刷新或新建）
    out = _liepin_storage_state_path()
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        browser.storage_state(path=str(out))
        log.info("猎聘已刷新 storage_state: %s", out)
    except Exception as e:
        log.debug("猎聘刷新 storage_state 失败（可忽略）: %s", e)


def _liepin_safe_export_storage_state(browser) -> None:
    # AI 生成
    # 生成目的：避免在未登录态下覆盖已有有效 JSON，仅在当前页不像登录页时落盘
    try:
        pages = list(browser.pages)
        page = pages[0] if pages else None
        if page is not None and _is_liepin_login_page(page):
            log.info("猎聘跳过 storage_state 落盘：当前页仍判定为登录/未登录态")
            return
    except Exception:
        pass
    _liepin_export_storage_state(browser)


# AI 生成
# 生成目的：为详情页 goto 设置视口与 Referer/UA，降低异常跳转登录的概率，并在自动登录恢复后再次应用
def _apply_detail_page_headers(page):
    page.set_viewport_size({"width": 1920, "height": 1080})
    page.set_extra_http_headers({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Referer": "https://www.liepin.com/",
    })


# AI 生成
# 生成目的：列表/详情等每次跳转前的随机等待 5–10 秒，满足反爬节奏（与翻页、详情链式切换一致）
def _liepin_random_nav_delay():
    d = random.uniform(10.0, 20.0)
    log.info("页面切换随机等待 %.1f 秒（反爬节奏）", d)
    time.sleep(d)


# AI 生成
# 生成目的：对单页硬校验通过的岗位批量打开详情；与列表阶段共用 login_recovery_used；每次 goto 前随机等待；失败时返回 crawl_stop 供上层立即 return
def _liepin_process_detail_batch(p, browser, page, login_recovery_used, jobs_batch):
    kept = []
    if not jobs_batch:
        return browser, page, login_recovery_used, kept, False
    # AI 生成
    # 生成目的：本批次详情抓取共用「连续空岗位介绍」计数；达到阈值判风控后停止并返回已抓取
    global _LIEPIN_EMPTY_INTRO_STREAK
    _apply_detail_page_headers(page)
    start_idx = 0
    while start_idx < len(jobs_batch):
        relogin_retry = False
        for idx in range(start_idx, len(jobs_batch)):
            job = jobs_batch[idx]
            try:
                _liepin_random_nav_delay()
                log.info(f"🔍 校验详情页：{job['标题']} | {job['链接']}")
                page.goto(
                    job["链接"],
                    timeout=10000,
                    wait_until="commit",
                )
                try:
                    page.wait_for_load_state("domcontentloaded", timeout=15000)
                except Exception:
                    pass
                # AI 生成
                # 生成目的：详情 page.goto 并等待加载后，必须检验是否被重定向到登录页，再决定自动登录或继续解析正文
                try:
                    du = (page.url or "")[:200]
                except Exception:
                    du = ""
                if _is_liepin_login_page(page):
                    log.warning(
                        "详情跳转后登录页检验：已重定向至登录页，将尝试自动登录 | %s | url=%s",
                        job.get("标题", ""),
                        du,
                    )
                    if not login_recovery_used:
                        try:
                            browser.close()
                        except Exception:
                            pass
                        browser, page = _liepin_try_auto_relogin(p)
                        if browser is None:
                            log.error("详情页自动登录失败，结束详情抓取")
                            browser = get_browser(
                                p,
                                headless=cfg.CRAWL_HEADLESS,
                                storage_state=_liepin_storage_state_for_launch(),
                            )
                            page = browser.new_page()
                            _apply_stealth_if_available(page)  # 在访问任何页面之前执行
                            return browser, page, login_recovery_used, kept, True
                        login_recovery_used = True
                        _apply_detail_page_headers(page)
                        start_idx = idx
                        relogin_retry = True
                        break
                    log.warning(
                        "登录恢复后仍跳转到登录页，结束详情抓取，仅返回已收集的岗位"
                    )
                    return browser, page, login_recovery_used, kept, True

                page_text = page.evaluate("() => document.body.innerText")

                if not check_chatted(page_text):
                    log.info(f"⚠️ 详情页含「继续聊」，排除岗位：{job['标题']}")
                else:
                    job["介绍"] = extract_job_description(page)
                    # AI 生成
                    # 生成目的：VLM_ENABLED=False 时若岗位介绍为空则不保留该岗位；连续两次为空则判风控并提前退出
                    if not (job.get("介绍") or "").strip():
                        _LIEPIN_EMPTY_INTRO_STREAK += 1
                        log.warning(
                            "详情页岗位介绍为空（extract_job_description 返回空），已连续 %s 次：%s",
                            _LIEPIN_EMPTY_INTRO_STREAK,
                            (job.get("链接") or "")[:120],
                        )
                        if _LIEPIN_EMPTY_INTRO_STREAK >= 2:
                            log.critical(
                                "连续两次无法提取岗位介绍，疑似账号被风控：将提前退出爬虫并返回已抓取岗位（count=%s）",
                                len(kept),
                            )
                            return browser, page, login_recovery_used, kept, True
                        continue
                    _LIEPIN_EMPTY_INTRO_STREAK = 0
                    # AI 生成
                    # 生成目的：VLM_ENABLED 时截屏+Qwen-VL 五字段，否则 HTML 五字段；两路经 format 后 job[「介绍」] 同构
                    # job["介绍"] = resolve_job_introduction_text(page, job)
                    try:
                        # 详情阶段把「介绍」回写到列表快照库，避免 list_jobs.description 为空
                        from services.job_store import update_crawl_list_description

                        update_crawl_list_description(
                            LIEPIN_JOB_PLATFORM,
                            int(job.get("scene_id") or 0),
                            str(job.get("链接") or ""),
                            str(job.get("介绍") or ""),
                        )
                    except Exception as ex:
                        log.debug("回写列表 description 失败（可忽略继续爬取）: %s", ex)
                    
                    kept.append(job)
                    intro_preview = (job["介绍"][:50] + "...") if len(job.get("介绍") or "") > 50 else job.get("介绍", "")
                    log.info(
                        f"✅ 详情页无「继续聊」，保留岗位：{job['标题']}，提取岗位介绍：{intro_preview}"
                    )
            except Exception as e:
                log.error(f"❌ 详情页校验失败 {job['标题']}：{str(e)}")
                break
        if relogin_retry:
            continue
        break
    return browser, page, login_recovery_used, kept, False


# AI 生成
# 生成目的：猎聘列表 URL 的 city / dq 参数，键为名称（城市或省级行政区），值为站点区划编码
LIEPIN_CITY_CODE = {
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


def _get_liepin_citycode(city) -> List[str]:
    # AI 生成
    # 生成目的：按城市/省名在表中查找猎聘 dq 编码；可扩展为一名多码时返回多元素列表
    if city is None:
        return []
    name = str(city).strip()
    if not name:
        return []
    code = LIEPIN_CITY_CODE.get(name)
    return [code] if code else []


def _dqs_for_pub30(preferred_name_list: List[str], province_name: str) -> List[str]:
    # AI 生成
    # 生成目的：pubTime=30 时优先按 PREFERRED 各项依次得到 dq；若全未命中再尝试 province
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
    # AI 生成
    # 生成目的：accept_remote 的 pubTime=7 段使用「各期望城市名」在表中的全部编码（去重保序，不含省 fallback）
    out: List[str] = []
    seen: set[str] = set()
    for n in preferred_name_list:
        for c in _get_liepin_citycode(n):
            if c and c not in seen:
                seen.add(c)
                out.append(c)
    return out


def _liepin_paginate_list_url(
    p,
    browser,
    page,
    login_recovery_used: bool,
    *,
    city_dq: str,
    pub_time: int,
    list_start: int,
    encoded_key: str,
    salaryCode: str,
    crawl_scene_id: int,
    scene_id: Optional[int],
    plan: List[Dict[str, Any]],
    segment_index: int,
    has_following_list_segment: bool,
) -> Tuple[object, object, bool, list, bool, bool]:
    # AI 生成
    # 生成目的：对单条 (city_code, pubTime) 列表流翻页+详情；自 list_start 起至 min(断点+MAX_PAGE, 站点总页)；断点写 plan+segment_index+last_list_page，达总页且仍有后续子任务时推进到下一 (city_code, pubTime) 的 -1
    # AI 返回：(browser, page, login_recovery_used, segment_final_jobs, stop_crawl, segment_natural_end) stop_crawl=应向上层立即 return，segment_natural_end=本流因该 URL 下已达站点总页
    final_jobs: list = []
    current_page = list_start
    maxpage_url = None
    crawl_finished_normally = False
    # AI 生成
    # 生成目的：自 list_start 起，结束于 min(断点+MAX_PAGE, 站点总页)（均为列表页 0 基，与 get_liepin_list_resume 一致）
    while True:
        log.info(
            "📄 猎聘列表 [pubTime=%s dq=%s] 正在抓取第 %s 页",
            pub_time,
            city_dq,
            current_page,
        )
        url = f"https://www.liepin.com/zhaopin/?city={city_dq}&dq={city_dq}&pubTime={pub_time}&currentPage={current_page}&pageSize=40&key={encoded_key}&salaryCode={salaryCode}"
        try:
            page.goto(url, timeout=60000)
        except Exception as e:
            log.info(f"❌ 第 {current_page} 页访问失败：{e}，停止翻页")
            break
        # AI 生成
        # 生成目的：列表 page.goto 之后必须检验是否重定向登录页，再走自动登录并重开当前列表页
        browser, page, login_recovery_used, ok, _ = _liepin_verify_list_login_and_recover(
            p, browser, page, login_recovery_used, url, context="列表页.goto(url)后"
        )
        if not ok:
            break
        _liepin_random_nav_delay()
        if maxpage_url is None:
            maxpage_url = get_liepin_max_page(page)
            mpu = int(maxpage_url) if maxpage_url is not None else None
            if mpu is not None and list_start >= mpu:
                log.info("断点起始页 %s 已不小于站点总页上界 %s，不再抓取", list_start, mpu)
                break
            log.info(
                "最大页数=%s，本任务自断点 %s 将最多至 min(断点+MAX_PAGE, 总页) 即 min(%s, %s)",
                maxpage_url,
                list_start,
                list_start + int(cfg.MAX_PAGE),
                mpu,
            )
        # 1. 定位岗位列表容器
        job_list_box = page.query_selector(".job-list-box")
        if not job_list_box:
            log.info(f"⚠️ 第 {current_page} 页未找到岗位列表，结束抓取")
            crawl_finished_normally = True
            break

        # 2. 定位每个岗位卡片
        items = job_list_box.query_selector_all(".job-card-pc-container")
        if len(items) == 0:
            log.info(f"⚠️ 第 {current_page} 页无岗位卡片，正常结束抓取")
            crawl_finished_normally = True
            break

        log.info(f"🔍 第 {current_page} 页找到 {len(items)} 个岗位卡片")

        page_filter_pass_jobs = []

        # AI 生成
        # 生成目的：遍历卡片时会话可能中途失效；自动登录后原 ElementHandle 失效，故用 item_idx + while + refetch 重拉 items，失败则 break_pagination 退出翻页
        # 3. 逐个处理岗位（会话可能在遍历卡片时失效，需检测登录页并刷新 items）
        item_idx = 0
        break_pagination = False
        while item_idx < len(items):
            # AI 生成
            # 生成目的：每轮卡片处理前检验当前页是否已变为登录页（含异步重定向），再按需恢复列表会话
            browser, page, login_recovery_used, ok, refetch = _liepin_verify_list_login_and_recover(
                p, browser, page, login_recovery_used, url, context="列表卡片循环.每轮开头"
            )
            if not ok:
                break_pagination = True
                break
            if refetch:
                job_list_box = page.query_selector(".job-list-box")
                if not job_list_box:
                    log.info("⚠️ 登录恢复后未找到岗位列表，结束抓取")
                    break_pagination = True
                    break
                items = job_list_box.query_selector_all(".job-card-pc-container")
                if not items:
                    log.info("⚠️ 登录恢复后本页无岗位，结束抓取")
                    break_pagination = True
                    break
                item_idx = min(item_idx, len(items) - 1)
                continue

            item = items[item_idx]
            try:
                # ====== 第一步：提取列表页基础信息 ======
                # 提取岗位详情链接
                job_link_tag = item.query_selector('a[data-nick="job-detail-job-info"]')
                link = job_link_tag.get_attribute("href") if job_link_tag else ""
                if not link:
                    item_idx += 1
                    continue  # 无链接跳过
                if is_crawl_list_url_present(LIEPIN_JOB_PLATFORM, crawl_scene_id, link):
                    log.info(
                        "列表链接已在平台库 %s（scene_id=%s）中存在，跳过该岗位",
                        LIEPIN_JOB_PLATFORM,
                        crawl_scene_id,
                    )
                    item_idx += 1
                    continue
                # 提取岗位名称
                title_tag = item.query_selector('.ellipsis-1')
                title = title_tag.get_attribute("title") if title_tag else ""

                # 提取薪资
                salary_tag = item.query_selector('span._40108E8PWS')
                salary = salary_tag.inner_text().strip() if salary_tag else ""

                # 提取工作地点
                area = ""
                area_tags = item.query_selector_all("span")
                for tag in area_tags:
                    text = tag.inner_text().strip()
                    if "-" in text and len(text) < 15 and not text.isdigit():
                        area = text
                        break

                # 提取工作年限
                exp_tag = item.query_selector('span._40108hJbMI')
                experience = exp_tag.inner_text().strip() if exp_tag else ""

                # 提取公司名称
                company_tag = item.query_selector('span._40108K6Y1.ellipsis-1')
                company = company_tag.inner_text().strip() if company_tag else ""
                # 兼容备选：如果上面没抓到，取data-nick="job-detail-company-info"下的ellipsis-1
                if not company:
                    company_container = item.query_selector('[data-nick="job-detail-company-info"]')
                    if company_container:
                        company_tag2 = company_container.query_selector(".ellipsis-1")
                        company = company_tag2.inner_text().strip() if company_tag2 else ""
                # ====== 第二步：硬校验（DOM 拉取后也可能已被重定向到登录页）======
                # AI 生成
                # 生成目的：字段从列表 DOM 读完后、硬校验前再次检验是否登录页（与列表 goto 后检验同一套列表恢复逻辑），避免登录页 DOM 被误判为硬校验失败
                browser, page, login_recovery_used, ok, refetch = _liepin_verify_list_login_and_recover(
                    p, browser, page, login_recovery_used, url, context="列表卡片.硬校验前"
                )
                if not ok:
                    break_pagination = True
                    break
                if refetch:
                    job_list_box = page.query_selector(".job-list-box")
                    if not job_list_box:
                        log.info("⚠️ 硬校验前登录恢复后未找到岗位列表，结束抓取")
                        break_pagination = True
                        break
                    items = job_list_box.query_selector_all(".job-card-pc-container")
                    if not items:
                        log.info("⚠️ 硬校验前登录恢复后本页无岗位，结束抓取")
                        break_pagination = True
                        break
                    item_idx = min(item_idx, len(items) - 1)
                    continue

                if not hard_filter(title ,area,salary):
                    log.info(f"🔍 硬校验失败{title},{area},{salary}{link}")
                    item_idx += 1
                    continue
                log.info(f"🔍 硬校验通过{title},{area},{salary}{link}")

                # ====== 第四步：校验通过，收集本页待拉详情的岗位 ======
                list_job = {
                    "平台": "猎聘",
                    "标题": title,
                    "公司": company,
                    "薪资": salary,
                    "地点": area,
                    "工作年限": experience,
                    "链接": link,
                    "介绍": "",
                    # 详情阶段回写 list_jobs.description 需要 scene_id（否则无法定位行）
                    "scene_id": int(crawl_scene_id),
                }
                page_filter_pass_jobs.append(list_job)
                try:
                    upsert_crawl_list_job(LIEPIN_JOB_PLATFORM, crawl_scene_id, list_job)
                except Exception as ex:
                    log.warning("写入列表岗位快照失败（可继续爬取）: %s", ex)
                log.info(f"✅ 公司 {company} 岗位 {title} 校验通过，加入本页待详情队列")

            except Exception as e:
                log.debug(f"岗位处理失败: {e}")
                # 出错后回到列表页，避免卡住
                time.sleep(2)
            item_idx += 1

        # AI 生成
        # 生成目的：列表卡片阶段登录恢复失败或中止时，跳出外层翻页循环，避免在无有效列表态下继续下一页
        if break_pagination:
            break

        # AI 生成
        # 生成目的：每爬完一页列表并硬筛选后，立即跳转该页全部通过岗位的详情（再翻下一页列表）；与列表共用 login_recovery_used
        if page_filter_pass_jobs:
            log.info(
                "🚀 第 %s 页硬校验通过 %s 个岗位，开始本页详情批量校验",
                current_page,
                len(page_filter_pass_jobs),
            )
            browser, page, login_recovery_used, batch_kept, crawl_stop = (
                _liepin_process_detail_batch(
                    p, browser, page, login_recovery_used, page_filter_pass_jobs
                )
            )
            final_jobs.extend(batch_kept)
            if crawl_stop:
                return (browser, page, login_recovery_used, final_jobs, True, False)
        else:
            log.info("📋 第 %s 页无硬校验通过岗位，跳过本页详情", current_page)

        _liepin_random_nav_delay()
        if scene_id is not None and plan:
            set_liepin_list_checkpoint(
                int(scene_id),
                plan,
                int(segment_index),
                int(current_page),
            )
        current_page += 1
        # AI 生成
        # 生成目的：下标为开区间；本段允许的最大下标 = min(断点+MAX_PAGE, 站点总页数)；仅当因「已达站点总页」停止时清断点
        mpu = int(maxpage_url) if maxpage_url is not None else None
        run_end_exclusive = list_start + int(cfg.MAX_PAGE)
        if mpu is not None:
            end_exclusive = min(run_end_exclusive, mpu)
        else:
            end_exclusive = run_end_exclusive
        if current_page >= end_exclusive:
            if mpu is not None and current_page >= mpu:
                crawl_finished_normally = True
            break
    if (
        crawl_finished_normally
        and has_following_list_segment
        and scene_id is not None
        and plan
    ):
        set_liepin_list_checkpoint(
            int(scene_id),
            plan,
            int(segment_index) + 1,
            -1,
        )
    return (browser, page, login_recovery_used, final_jobs, False, crawl_finished_normally)


def _crawl_liepin(
    p,
    browser,
    page,
    scene_id: Optional[int] = None,
    reset_checkpoint: bool = False,
):
    # AI 生成
    # 生成目的：每轮爬取开始重置 VLM/HTML 计时与 VLM 调用统计，避免跨次累加
    reset_liepin_vlm_stats()
    final_jobs = []
    encoded_key = urllib.parse.quote(cfg.SEARCH_KEYWORD)
    salaryCode = str(int(cfg.MIN_SALARY * 12 * 0.1)) + "$" + str(int(cfg.MAX_SALARY * 14 * 0.1))
    login_recovery_used = False
    crawl_scene_id = int(scene_id) if scene_id is not None else 0

    preferred = getattr(cfg, "PREFERRED_CITIES", None) or []
    if not isinstance(preferred, list):
        preferred = [preferred] if preferred else []
    preferred = [str(x).strip() for x in preferred if str(x).strip()]
    province = str(getattr(cfg, "PROVINCE", "") or "").strip()
    accept_remote = bool(getattr(cfg, "ACCEPT_REMOTE", False))

    dqs_30 = _dqs_for_pub30(preferred, province)
    used_30 = set(dqs_30)
    # AI 生成
    # 生成目的：与列表任务顺序一一对应，供断点记录全量 city_code+pubTime 及当前子任务
    plan: List[Dict[str, Any]] = [
        {"city_code": dq, "pubTime": 30} for dq in dqs_30
    ]
    if accept_remote:
        for dq in _all_dq_from_preferred_cities_only(preferred):
            if dq not in used_30:
                plan.append({"city_code": dq, "pubTime": 7})
    n_plan = len(plan)
    if not plan:
        log.info(
            "猎聘未生成任何城市 dq（PREFERRED+province 在表中无匹配或为空），仅输出统计。preferred=%r province=%r",
            preferred,
            province,
        )
        log_liepin_vlm_stats_summary()
        _crawl_liepin.last_collected = []
        log.info("✅ 猎聘最终有效岗位：0 个")
        return [], browser

    if scene_id is not None:
        start_seg, start_list = get_liepin_list_resume(
            int(scene_id),
            plan,
            reset=reset_checkpoint,
        )
    else:
        start_seg, start_list = 0, 0

    last_segment_natural = False
    for seg_idx in range(start_seg, n_plan):
        dq = str(plan[seg_idx]["city_code"])
        pub = int(plan[seg_idx]["pubTime"])
        ls = start_list if seg_idx == start_seg else 0
        has_next = seg_idx < n_plan - 1
        log.info(
            "猎聘列表子任务 [段 %s/%s pubTime=%s city_code=%s] 起始页=%s",
            seg_idx + 1,
            n_plan,
            pub,
            dq,
            ls,
        )
        (browser, page, login_recovery_used, seg, stop, natural) = _liepin_paginate_list_url(
            p,
            browser,
            page,
            login_recovery_used,
            city_dq=dq,
            pub_time=pub,
            list_start=ls,
            encoded_key=encoded_key,
            salaryCode=salaryCode,
            crawl_scene_id=crawl_scene_id,
            scene_id=scene_id,
            plan=plan,
            segment_index=seg_idx,
            has_following_list_segment=has_next,
        )
        final_jobs.extend(seg)
        if stop:
            _crawl_liepin.last_collected = final_jobs
            return final_jobs, browser
        last_segment_natural = natural

    if last_segment_natural and scene_id is not None:
        remove_scene_checkpoint(scene_id)

    log.info(f"📊 猎聘列表与详情处理完成，累计有效岗位：{len(final_jobs)} 个")
    log_liepin_vlm_stats_summary()
    _crawl_liepin.last_collected = final_jobs
    log.info(f"✅ 猎聘最终有效岗位：{len(final_jobs)} 个")
    return final_jobs, browser


def get_liepin_max_page(page):
    """
    从分页栏读取真实最大页数，适配猎聘Antd分页
    :param page: playwright的page对象
    :return: 真实最大页数（int），失败则返回1
    """
    try:
        # 等待分页栏加载完成（避免读取不到）
        page.wait_for_selector('.ant-pagination', timeout=5000)

        # 执行JS读取最后一个数字页码（核心逻辑）
        max_page = page.evaluate("""() => {
            // 筛选出所有「数字页码按钮」（排除上一页/下一页/跳转）
            const numButtons = document.querySelectorAll(
                '.ant-pagination-item:not(.ant-pagination-prev):not(.ant-pagination-next):not(.ant-pagination-jump-prev):not(.ant-pagination-jump-next)'
            );
            if (numButtons.length === 0) return 1; // 无分页，默认1页

            // 取最后一个数字按钮的文本/title（两种都兼容）
            const lastBtn = numButtons[numButtons.length - 1];
            const pageNum = lastBtn.title || lastBtn.innerText;
            return parseInt(pageNum) || 1;
        }""")
        log.info(f"✅ 识别到真实最大页数：{max_page}")
        return max_page
    except Exception as e:
        log.warning(f"⚠️ 读取最大页数失败，默认爬1页：{e}")
        return 1

def extract_job_description(page, startfrom=50, endto=500):
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
        job_intro_elem = page.query_selector('section.job-intro-container > dl:first-child > dd')
        if job_intro_elem:
            # 提取文本并清理冗余字符（&nbsp、多余换行/空格）
            job_desc = job_intro_elem.inner_text().strip()
            # 1. 替换HTML空格符 &nbsp;
            job_desc = job_desc.replace("&nbsp;", " ").replace("&nbsp", " ")
            # 2. 合并多余换行/空格为单个空格
            job_desc = re.sub(r'\s+', ' ', job_desc)
            # 3. 去除首尾无用字符
            job_desc = job_desc.strip('"').strip("'").strip()
            return job_desc[startfrom:endto] if len(job_desc) > endto else job_desc
        else:
            log.info(f"⚠️ 未获取到岗位详情")
            return ""
    except Exception as e:
        log.error(f"提取岗位详情失败：{str(e)}")
        return ""

