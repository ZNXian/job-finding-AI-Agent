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
from typing import Optional

from playwright.sync_api import Error, sync_playwright

import config as cfg
from config import log
from services.job_store import (
    is_crawl_list_url_present,
    upsert_crawl_list_job,
)
from utils.browser import BROWSER_USER_DATA_DIR, get_browser, wait_for_browser_close
from utils.crawl_checkpoint import (
    get_resume_list_page_index,
    remove_scene_checkpoint,
    set_scene_last_page,
)
from utils.filter import *

LIEPIN_JOB_PLATFORM = "liepin"


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
    start_page = 0
    if scene_id is not None:
        start_page = get_resume_list_page_index(scene_id, reset=reset_checkpoint)
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
        try:
            jobs, browser = _crawl_liepin(
                p,
                browser,
                page,
                scene_id=scene_id,
                start_page=start_page,
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
    return nb, nb.new_page()


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
    d = random.uniform(5.0, 10.0)
    log.info("页面切换随机等待 %.1f 秒（反爬节奏）", d)
    time.sleep(d)


# AI 生成
# 生成目的：对单页硬校验通过的岗位批量打开详情；与列表阶段共用 login_recovery_used；每次 goto 前随机等待；失败时返回 crawl_stop 供上层立即 return
def _liepin_process_detail_batch(p, browser, page, login_recovery_used, jobs_batch):
    kept = []
    if not jobs_batch:
        return browser, page, login_recovery_used, kept, False
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


def _crawl_liepin(
    p,
    browser,
    page,
    scene_id: Optional[int] = None,
    start_page: int = 0,
):
    final_jobs = []
    encoded_key = urllib.parse.quote(cfg.SEARCH_KEYWORD)
    city ="410"
    salaryCode = str(int(cfg.MIN_SALARY *12 *0.1))+"$" +str(int(cfg.MAX_SALARY *14*0.1))
    # salaryCode = ""
    current_page = max(0, int(start_page))
    maxpage_url = None
    login_recovery_used = False
    crawl_finished_normally = False
    crawl_scene_id = int(scene_id) if scene_id is not None else 0
    while True: # 循环翻页：0 ~ maxpage
    # for current_page in range(0, min(maxpage_url,maxpage)):
        log.info(f"📄 猎聘正在抓取第 {current_page} 页")
        # 你的固定URL格式
        url = f"https://www.liepin.com/zhaopin/?city={city}&dq={city}&pubTime=30&currentPage={current_page}&pageSize=40&key={encoded_key}&salaryCode={salaryCode}"
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
            log.info(f"最大页数{maxpage_url}")
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
                _crawl_liepin.last_collected = final_jobs
                return final_jobs, browser
        else:
            log.info("📋 第 %s 页无硬校验通过岗位，跳过本页详情", current_page)

        _liepin_random_nav_delay()
        if scene_id is not None:
            set_scene_last_page(scene_id, current_page)
        current_page += 1
        if current_page >= min(cfg.MAX_PAGE, maxpage_url):
            crawl_finished_normally = True
            break

    if crawl_finished_normally and scene_id is not None:
        remove_scene_checkpoint(scene_id)

    log.info(f"📊 猎聘列表与详情处理完成，累计有效岗位：{len(final_jobs)} 个")

    # 在返回前保存到函数属性
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

def extract_job_description(page,startfrom=100,endto=400):
    """
    从猎聘岗位详情页提取工作介绍文本（优先解析结构化JSON，最稳定）
    :param page_text:
    :return: 截取前100-400字的岗位介绍文本（无则返回空字符串）
    """
    try:
        job_intro_elem = page.query_selector('dl.job-intro-container dd[data-selector="job-intro-content"]')
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