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

def get_browser(p):
    """获取持久化浏览器上下文（保持登录状态）"""
    # ✅ 永久保存登录状态！一次登录，永远不掉线
    return p.chromium.launch_persistent_context(
        user_data_dir="./browser_data",  # 脚本自己创建的独立浏览器数据
        headless=False,
        slow_mo=500
    )

def wait_for_browser_close(url: str, timeout: int = 300, check_interval: float = 1.0):
    """
    打开网页并等待用户关闭浏览器
    
    Args:
        url: 要打开的网页链接
        timeout: 超时时间（秒），默认300秒
        check_interval: 检查间隔（秒），默认1秒
    
    Returns:
        bool: True表示正常关闭，False表示超时
    """
    with sync_playwright() as p:
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