# -*- coding:utf-8 -*-
# @CreatTime : 2026 19:39
# @Author : XZN
import signal
import sys
from contextlib import contextmanager
from playwright.sync_api import sync_playwright,Error
import urllib.parse
import time
import config as cfg
from config import log
from utils.browser import BROWSER_USER_DATA_DIR, get_browser, wait_for_browser_close
from utils.filter import *

def login_liepin(timeout: int = 120):
    """打开猎聘首页，使用与爬虫相同的用户目录，便于保存登录态。"""
    wait_for_browser_close(
        "https://www.liepin.com/",
        timeout,
        user_data_dir=BROWSER_USER_DATA_DIR,
    )
    log.info("猎聘登录流程结束")


def crawl_liepin():
    with sync_playwright() as p:
        jobs = []
        log.info("猎聘爬虫：无头模式（后台）" if cfg.CRAWL_HEADLESS else "猎聘爬虫：显示浏览器窗口")
        browser = get_browser(p, headless=cfg.CRAWL_HEADLESS)
        page = browser.new_page()
        try:
            jobs, browser = _crawl_liepin(p, browser, page)
        except Exception as e:
            error_msg = str(e).lower()
            if any(kw in error_msg for kw in ["closed", "detached", "target", "context"]):
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
                browser.close()
            except Exception:
                pass

        log.info(f"可处理岗位：{len(jobs)}")
        return jobs

def _is_liepin_login_page(page) -> bool:
    """判断当前页是否为登录页（会话失效后详情链接常被重定向至此）。"""
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
            "() => (document.body && document.body.innerText) ? document.body.innerText.slice(0, 800) : ''"
        ) or ""
    except Exception:
        return False
    markers = ("手机号登录", "验证码登录", "扫码登录", "密码登录", "短信登录")
    if any(m in snippet for m in markers) and ("猎聘" in snippet or "liepin" in snippet.lower()):
        return True
    return False


def _apply_detail_page_headers(page):
    page.set_viewport_size({"width": 1920, "height": 1080})
    page.set_extra_http_headers({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Referer": "https://www.liepin.com/",
    })


def _crawl_liepin(p, browser, page):
    filter_pass_jobs = []
    encoded_key = urllib.parse.quote(cfg.SEARCH_KEYWORD)
    city ="410"
    salaryCode = str(int(cfg.MIN_SALARY *12 *0.1))+"$" +str(int(cfg.MAX_SALARY *14*0.1))
    # salaryCode = ""
    current_page=0
    while True: # 循环翻页：0 ~ maxpage
    # for current_page in range(0, min(maxpage_url,maxpage)):
        log.info(f"📄 猎聘正在抓取第 {current_page} 页")
        # 你的固定URL格式
        url = f"https://www.liepin.com/zhaopin/?city={city}&dq={city}&pubTime=30&currentPage={current_page}&pageSize=40&key={encoded_key}&salaryCode={salaryCode}"
        try:
            page.goto(url, timeout=60000)
            time.sleep(5)  # 等待列表页加载完成
        except Exception as e:
            log.info(f"❌ 第 {current_page} 页访问失败：{e}，停止翻页")
            break
        if current_page==0:#先进入第一页获取最大页数
            maxpage_url = get_liepin_max_page(page)
            log.info(f"最大页数{maxpage_url}")
        # 1. 定位岗位列表容器
        job_list_box = page.query_selector(".job-list-box")
        if not job_list_box:
            log.info(f"⚠️ 第 {current_page} 页未找到岗位列表，结束抓取")
            break

        # 2. 定位每个岗位卡片
        items = job_list_box.query_selector_all(".job-card-pc-container")
        if len(items) == 0:
            log.info(f"⚠️ 第 {current_page} 页无岗位，结束抓取")
            break

        log.info(f"🔍 第 {current_page} 页找到 {len(items)} 个岗位卡片")

        # 3. 逐个处理岗位（先跳详情页校验「继续聊」）
        for item in items:
            try:
                # ====== 第一步：提取列表页基础信息 ======
                # 提取岗位详情链接
                job_link_tag = item.query_selector('a[data-nick="job-detail-job-info"]')
                link = job_link_tag.get_attribute("href") if job_link_tag else ""
                if not link:
                    continue  # 无链接跳过
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
                # ====== 第二步：硬校验 ======
                if not hard_filter(title ,area,salary):
                    log.info(f"🔍 硬校验失败{title},{area},{salary}{link}")
                    continue
                log.info(f"🔍 硬校验通过{title},{area},{salary}{link}")

                # ====== 第四步：校验通过，收集岗位信息 ======
                filter_pass_jobs.append({
                    "平台": "猎聘",
                    "标题": title,
                    "公司": company,
                    "薪资": salary,
                    "地点": area,
                    "工作年限": experience,
                    "链接": link,
                    "介绍":""
                })
                log.info(f"✅ 公司 {company} 岗位 {title} 校验通过，加入列表")

            #
            except Exception as e:
                log.debug(f"岗位处理失败: {e}")
                # 出错后回到列表页，避免卡住
                time.sleep(2)
        current_page +=1
        if current_page>= min(cfg.MAX_PAGE,maxpage_url):
            break

    log.info(f"📊 猎聘列表页筛选完成，共 {len(filter_pass_jobs)} 个岗位通过硬筛选")
    # 第二步：批量跳转详情页，校验「聊一聊/继续聊」；会话失效时唤起登录并最多恢复一次
    final_jobs = []
    if filter_pass_jobs:
        log.info(f"🚀 开始批量校验详情页（共 {len(filter_pass_jobs)} 个岗位）")
        _apply_detail_page_headers(page)
        start_idx = 0
        login_recovery_used = False
        while start_idx < len(filter_pass_jobs):
            relogin_retry = False
            for idx in range(start_idx, len(filter_pass_jobs)):
                job = filter_pass_jobs[idx]
                try:
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
                    if _is_liepin_login_page(page):
                        if not login_recovery_used:
                            log.warning(
                                "检测到详情页被重定向到登录页（可能因请求过多会话失效），"
                                "将关闭当前浏览器并弹出窗口，请完成登录后关闭浏览器"
                            )
                            try:
                                browser.close()
                            except Exception:
                                pass
                            login_liepin()
                            login_recovery_used = True
                            browser = get_browser(p, headless=cfg.CRAWL_HEADLESS)
                            page = browser.new_page()
                            _apply_detail_page_headers(page)
                            start_idx = idx
                            relogin_retry = True
                            break
                        log.warning(
                            "登录恢复后仍跳转到登录页，结束详情抓取，仅返回已收集的岗位"
                        )
                        _crawl_liepin.last_collected = final_jobs
                        return final_jobs, browser

                    page_text = page.evaluate("() => document.body.innerText")

                    if not check_chatted(page_text):
                        log.info(f"⚠️ 详情页含「继续聊」，排除岗位：{job['标题']}")
                    else:
                        job["介绍"] = extract_job_description(page)
                        final_jobs.append(job)
                        intro_preview = (job["介绍"][:50] + "...") if len(job.get("介绍") or "") > 50 else job.get("介绍", "")
                        log.info(
                            f"✅ 详情页无「继续聊」，保留岗位：{job['标题']}，提取岗位介绍：{intro_preview}"
                        )
                    time.sleep(3)
                except Exception as e:
                    # todo  详情页校验失败原因需要测试
                    log.error(f"❌ 详情页校验失败 {job['标题']}：{str(e)}")
                    # continue
                    break
            if relogin_retry:
                continue
            break

    # 在返回前保存到函数属性
    _crawl_liepin.last_collected = final_jobs  # 或 filter_pass_jobs
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