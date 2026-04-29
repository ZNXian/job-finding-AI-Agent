# -*- coding:utf-8 -*-
# @CreatTime : 2026 19:14
# @Author : XZN

"""浏览器相关工具（Async Playwright）。

注意：本模块为异步版本（async_playwright）。
老版本已保存在 `utils/browser_legacy.py`。
"""

import json
import logging
import random
import time
import asyncio
from pathlib import Path
from typing import Optional

from playwright.async_api import Error, async_playwright


log = logging.getLogger(__name__)

# 与猎聘爬虫共用，便于登录态与爬取上下文一致
BROWSER_USER_DATA_DIR = "./browser_data"

# 更真实的 UA / 视口（用于 headless 下减少明显特征）
DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
DEFAULT_VIEWPORT = {"width": 1920, "height": 1080}


async def apply_anti_detect_init_scripts(page) -> None:
    """对新建 Page 注入通用反检测 init scripts（必须在 goto 前调用）。"""
    try:
        await page.add_init_script(
            """
            const originalQuery = window.navigator.permissions.query;
            window.navigator.permissions.query = (parameters) => (
                parameters && parameters.name === 'notifications'
                    ? Promise.resolve({ state: Notification.permission })
                    : originalQuery(parameters)
            );
            """
        )
    except Exception:
        return


async def random_mouse_move(page) -> None:
    """在视口范围内随机移动鼠标若干次。"""
    try:
        vp = page.viewport_size or {"width": 1280, "height": 720}
        w = int(vp.get("width") or 1280)
        h = int(vp.get("height") or 720)
        moves = random.randint(2, 6)
        for _ in range(moves):
            x = random.randint(10, max(11, w - 10))
            y = random.randint(10, max(11, h - 10))
            await page.mouse.move(x, y, steps=random.randint(5, 18))
            await page.wait_for_timeout(int(random.uniform(0.05, 0.18) * 1000))
    except Exception:
        return


async def random_scroll(page) -> None:
    """随机滚动若干次（上下都可能），鼠标跟随移动。"""
    try:
        times = random.randint(1, 3)
        for _ in range(times):
            # 决定滚动方向和距离
            dy = random.randint(200, 900) * (1 if random.random() < 0.85 else -1)
            # 1. 滚动前，获取当前鼠标位置（或随机初始位置）
            try:
                current_mouse = await page.evaluate("() => ({ x: window.scrollX + 200, y: window.scrollY + 300 })")
            except:
                viewport = page.viewport_size
                current_mouse = {
                    'x': random.randint(100, viewport['width'] - 100),
                    'y': random.randint(100, viewport['height'] - 100)
                }
            # 2. 执行滚动
            await page.mouse.wheel(0, dy)
            # 3. 鼠标跟随移动（向滚动方向移动一段距离）
            follow_y = dy + random.randint(-50, 50)  # 跟随滚动距离，加一点随机偏移
            follow_x = random.randint(-30, 30)       # 轻微水平抖动
            await page.mouse.move(
                current_mouse['x'] + follow_x,
                current_mouse['y'] + follow_y,
                steps=random.randint(5, 12)
            )
            # 4. 滚动后的随机停顿
            await page.wait_for_timeout(int(random.uniform(0.2, 0.5) * 1000))
    except Exception:
        return


# async def random_click_blank(page) -> None:
#     """随机点击页面空白区域（尽量不点到链接/按钮）。"""
#     try:
#         vp = page.viewport_size or {"width": 1280, "height": 720}
#         w = int(vp.get("width") or 1280)
#         h = int(vp.get("height") or 720)
#         x = random.randint(int(w * 0.1), max(int(w * 0.1) + 1, int(w * 0.9)))
#         y = random.randint(int(h * 0.1), max(int(h * 0.1) + 1, int(h * 0.9)))
#         await page.mouse.click(x, y, delay=random.randint(20, 80))
#     except Exception:
#         return

async def random_click_blank(page) -> None:
    """随机点击页面空白区域（确保不点到任何链接/按钮）。"""
    try:
        vp = page.viewport_size or {"width": 1280, "height": 720}
        w = int(vp.get("width") or 1280)
        h = int(vp.get("height") or 720)
        
        # 获取页面上所有可点击元素的位置（a, button, [onclick] 等）
        clickable_bounds = await page.evaluate("""
            () => {
                const clickable = document.querySelectorAll('a, button, [onclick], [role="button"], .job-card-pc-container');
                const bounds = [];
                clickable.forEach(el => {
                    const rect = el.getBoundingClientRect();
                    if (rect.width > 0 && rect.height > 0) {
                        bounds.push({
                            x1: rect.left,
                            y1: rect.top,
                            x2: rect.right,
                            y2: rect.bottom
                        });
                    }
                });
                return bounds;
            }
        """)
        
        # 在空白区域找一个点击点（最多尝试 20 次）
        for _ in range(20):
            x = random.randint(int(w * 0.05), int(w * 0.95))
            y = random.randint(int(h * 0.1), int(h * 0.9))

            # 检查是否与任何可点击元素重叠
            is_blank = True
            for bounds in clickable_bounds:
                if bounds['x1'] <= x <= bounds['x2'] and bounds['y1'] <= y <= bounds['y2']:
                    is_blank = False
                    break
            if is_blank:
                await page.mouse.click(x, y, delay=random.randint(20, 80))
                return
        # 保底：点击 body 的最边缘角落
        await page.mouse.click(5, 5, delay=random.randint(20, 80))
    except Exception:
        return


async def human_behavior(page,d_long_use=True) -> None:
    """模拟人类行为（完全随机化版本）"""
    if d_long_use:
        # 1. 偶尔的长休息（5%概率）
        if random.random() < 0.05:
            d_long = random.uniform(20.0, 60.0)
            log.debug(f"长休息 {d_long:.1f}秒")
            await asyncio.sleep(d_long)
            
            return  # 长休息后直接返回，不再执行其他动作
    # 2. 随机决定本次要执行的动作数量和顺序
    actions = []
    
    # 可用的动作列表
    if random.random() < 0.6:
        actions.append(("鼠标移动", random_mouse_move))
    if random.random() < 0.8:
        actions.append(("滚动", random_scroll))
    if random.random() < 0.9:
        actions.append(("空白点击", random_click_blank))
    
    # 随机打乱顺序
    random.shuffle(actions)
    
    # 执行动作（1-3个）
    # for name, action in actions[:random.randint(1, 3)]:
    for name, action in actions[:]:
        await action(page)
        # 动作间随机停顿 0.3-1.5 秒
        await asyncio.sleep(random.uniform(0.3, 1.5))
    
    # 3. 最终随机停顿（0.3-1秒）
    final_wait = random.uniform(0.3, 1.0)
    await asyncio.sleep(final_wait)
    # log.debug(f"最终随机停顿 {final_wait:.1f}秒")


async def is_trap_job_card(card) -> bool:
    """判断卡片是否不可见（陷阱卡片）；True 表示应跳过。"""
    style = ((await card.get_attribute("style")) or "").lower()
    if "display:none" in style or "display: none" in style:
        return True
    if "visibility:hidden" in style or "visibility: hidden" in style:
        return True
    try:
        is_visible = await card.evaluate(
            """(el) => {
                const s = window.getComputedStyle(el);
                return s.display !== 'none' && s.visibility !== 'hidden';
            }"""
        )
        return not bool(is_visible)
    except Exception:
        return False


async def get_browser(p, headless: bool = False, storage_state: str | None = None):
    """获取持久化浏览器上下文（保持登录状态）。可选合并 Playwright storage_state JSON（如 liepin_login 写入）。

    说明：多数 Playwright 版本下 ``launch_persistent_context`` 不支持 ``storage_state`` 参数，
    因此在启动后读取 JSON 中的 ``cookies`` 并调用 ``context.add_cookies`` 合并（与官方导出格式一致）。
    """
    kwargs: dict = {
        "user_data_dir": BROWSER_USER_DATA_DIR,
        "headless": headless,
        "slow_mo": 0 if headless else 500,
    }
    if headless:
        kwargs["args"] = [
            "--disable-blink-features=AutomationControlled",
            "--disable-dev-shm-usage",
            "--no-sandbox",
            "--disable-web-security",
            "--disable-features=IsolateOrigins,site-per-process",
            "--disable-site-isolation-trials",
            "--window-size=1920,1080",
        ]
        kwargs["user_agent"] = DEFAULT_UA
        kwargs["viewport"] = dict(DEFAULT_VIEWPORT)
    context = await p.chromium.launch_persistent_context(**kwargs)
    if not storage_state:
        return context
    pth = Path(storage_state)
    try:
        if not (pth.is_file() and pth.stat().st_size > 0):
            return context
        data = json.loads(pth.read_text(encoding="utf-8"))
        cookies = data.get("cookies") or []
        if not cookies:
            return context
        if not context.pages:
            await context.new_page()
        await context.add_cookies(cookies)
    except Exception as e:
        log.debug("合并 storage_state 中的 cookies 失败（可忽略）: %s", e)
    return context

async def wait_for_browser_close(
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
    async with async_playwright() as p:
        if user_data_dir:
            context = await p.chromium.launch_persistent_context(
                user_data_dir=user_data_dir,
                headless=False,
            )
            page = context.pages[0] if context.pages else await context.new_page()
        else:
            browser = await p.chromium.launch(headless=False)
            page = await browser.new_page()
        await apply_anti_detect_init_scripts(page)
        await page.goto(url)
        
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
                await page.title()
            except (Error, Exception) as e:
                # 任何异常都认为浏览器已关闭
                log.info(f"✅ 检测到浏览器关闭")
                try:
                    if user_data_dir:
                        await context.close()
                    else:
                        await browser.close()
                except Exception:
                    pass
                return True
            
            # 进度提示（每30秒）
            if int(elapsed) % 30 == 0 and int(elapsed) > 0:
                remaining = timeout - int(elapsed)
                log.info(f"⏳ 等待中... 已等待 {int(elapsed)}秒，剩余 {remaining}秒")
            await page.wait_for_timeout(int(check_interval * 1000))
        # # 关闭浏览器
        # try:
        #     browser.close()
        # except:
        #     pass