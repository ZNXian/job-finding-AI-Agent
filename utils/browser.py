# -*- coding:utf-8 -*-
# @CreatTime : 2026 19:14
# @Author : XZN

# 浏览器相关工具
import logging
import time
import functools
from playwright.sync_api import sync_playwright,Error
# from typing import Callable, Any
# from playwright.sync_api import sync_playwright


log = logging.getLogger(__name__)

from pathlib import Path

# 与猎聘爬虫共用，便于登录态与爬取上下文一致
BROWSER_USER_DATA_DIR = "./browser_data"


def get_browser(p, headless: bool = False, storage_state: str | None = None):
    """获取持久化浏览器上下文（保持登录状态）。可选合并 Playwright storage_state JSON（如 liepin_login 写入）。"""
    kwargs: dict = {
        "user_data_dir": BROWSER_USER_DATA_DIR,
        "headless": headless,
        "slow_mo": 0 if headless else 500,
    }
    if storage_state:
        pth = Path(storage_state)
        try:
            if pth.is_file() and pth.stat().st_size > 0:
                kwargs["storage_state"] = str(pth.resolve())
        except OSError:
            pass
    return p.chromium.launch_persistent_context(**kwargs)

def wait_for_browser_close(
    url: str,
    timeout: int = 300,
    check_interval: float = 1.0,
    user_data_dir=None,
):
    """
    打开网页并等待用户关闭浏览器。

    Args:
        url: 要打开的网页链接
        timeout: 超时时间（秒），默认300秒
        check_interval: 检查间隔（秒），默认1秒
        user_data_dir: 若传入，则使用持久化用户目录（与爬虫 get_browser 一致，便于复用登录态）

    Returns:
        bool: True表示正常关闭，False表示超时
    """
    with sync_playwright() as p:
        if user_data_dir:
            context = p.chromium.launch_persistent_context(
                user_data_dir=user_data_dir,
                headless=False,
            )
            page = context.pages[0] if context.pages else context.new_page()
        else:
            browser = p.chromium.launch(headless=False)
            page = browser.new_page()
        page.goto(url)
        
        log.info(f"🔐 已打开: {url}")
        log.info(f"⏰ 超时时间: {timeout}秒")
        log.info("💡 完成后请关闭浏览器窗口")
        
        start_time = time.time()
        
        while True:
            elapsed = time.time() - start_time
            
            if elapsed > timeout:
                log.error(f"❌ 超时（{timeout}秒）")
                return False
            
            try:
                # 尝试获取页面标题，如果浏览器关闭会抛出异常
                page.title()
            except (Error, Exception) as e:
                # 任何异常都认为浏览器已关闭
                log.info(f"✅ 检测到浏览器关闭")
                try:
                    if user_data_dir:
                        context.close()
                    else:
                        browser.close()
                except Exception:
                    pass
                return True
            
            # 进度提示（每30秒）
            if int(elapsed) % 30 == 0 and int(elapsed) > 0:
                remaining = timeout - int(elapsed)
                log.info(f"⏳ 等待中... 已等待 {int(elapsed)}秒，剩余 {remaining}秒")
            time.sleep(check_interval)
        # # 关闭浏览器
        # try:
        #     browser.close()
        # except:
        #     pass