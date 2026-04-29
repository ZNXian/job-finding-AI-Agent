"""猎聘爬虫（Async Playwright + stealth_async）。

基于 `crawlers/liepin_legacy.py` 的流程重建为**纯异步**版本：
- 不允许使用 sync_playwright
- 每个新 page 在访问任何页面前：`await stealth_async(page)`

注意：legacy 文件不允许修改，本文件只提供可运行的异步主流程（列表→详情→返回岗位）。
"""

from __future__ import annotations

import asyncio
import random
import urllib.parse
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from playwright.async_api import Error, async_playwright
from playwright_stealth import stealth_async

import config as cfg
from config import log
from utils.browser import (
    BROWSER_USER_DATA_DIR,
    apply_anti_detect_init_scripts,
    get_browser,
    human_behavior,
    wait_for_browser_close,
)
from crawlers.liepin_higher_logic import _liepin_recover_list_login, crawl_with_higher_logic
LIEPIN_JOB_PLATFORM = "liepin"

LIEPIN_CITY_CODE: Dict[str, str] = {
    "北京": "010",
    "上海": "020",
    "广东": "050",
    "珠海": "050140",
    "深圳": "050090",
    "广州": "050020",
    "中山": "050130",
    "杭州": "070020",
}


async def _apply_stealth(page) -> None:
    # await stealth_async(page)  # 在访问任何页面之前执行
    pass


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


async def login_liepin(timeout: int = 120):
    """打开猎聘首页并等待用户关闭浏览器（用于人工登录保存态）。"""
    await wait_for_browser_close(
        "https://www.liepin.com/",
        timeout,
        user_data_dir=BROWSER_USER_DATA_DIR,
    )
    log.info("猎聘登录流程结束")


def _get_liepin_citycode(name: str) -> List[str]:
    n = (name or "").strip()
    if not n:
        return []
    c = LIEPIN_CITY_CODE.get(n)
    return [c] if c else []


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


async def _random_nav_delay() -> None:
    d = random.uniform(6.0, 12.0)
    await asyncio.sleep(d)


async def crawl_liepin(scene_id: Optional[int] = None, reset_checkpoint: bool = False):
    """列表页硬筛→详情页抽取介绍（VLM/HTML 同构），返回岗位 dict 列表。"""
    async with async_playwright() as pw:
        ss = _liepin_storage_state_for_launch()
        log.info("猎聘爬虫：无头模式（后台）" if cfg.CRAWL_HEADLESS else "猎聘爬虫：显示浏览器窗口")
        browser = await get_browser(pw, headless=cfg.CRAWL_HEADLESS, storage_state=ss)
        page = await browser.new_page()
        await apply_anti_detect_init_scripts(page)
        # await _apply_stealth(page)
        # 在进入高级逻辑前：先带 ss 访问首页检查登录态，必要时触发一次自动登录恢复
        try:
            # await page.goto("https://bot.sannysoft.com")
            # await human_behavior(page)
            # await page.screenshot(path="stealth_test.png", full_page=True)
            # await page.wait_for_timeout(5000)  # 手动查看截图结果
            # await page.goto("https://www.liepin.com/", timeout=60000, wait_until="domcontentloaded")
            await asyncio.sleep(3)
            browser, page, _used, ok, _refetch = await _liepin_recover_list_login(
                pw, browser, page, False, "https://www.liepin.com/"
            )
            log.info("已检查首页状态")
    
            if not ok:
                return []
        except Exception as e:
            log.info("进入高级逻辑前的首页登录态检查失败（忽略，继续爬取）：%s", e)
        try:
            jobs = await crawl_with_higher_logic(
                pw,
                browser,
                page,
                scene_id=scene_id,
                reset_checkpoint=reset_checkpoint,
            )
        finally:
            try:
                await browser.close()
            except Exception:
                pass
        return jobs


#
# legacy 中的大段“高级逻辑”已拆分到 crawlers.liepin_higher_logic
# 此处仅保留入口与 storage_state 路径等基础设施

