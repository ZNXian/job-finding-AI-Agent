# -*- coding: utf-8 -*-
"""异步版猎聘登录与 storage_state 刷新（完整迁移版）。

说明：
- 完整的旧版（含账号密码 + 验证码）在 `crawlers/liepin_login_save_state_legacy.py`（不修改）
- 本文件迁移 legacy 的核心能力：复用 storage_state 检测 → 账号密码登录 → 腾讯云/滑块验证码 → 落盘 storage_state
- 强制 `await stealth_async(page)`：在访问任何页面之前执行
"""

from __future__ import annotations

import re
import time
from pathlib import Path

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError
from playwright_stealth import stealth_async

import config as cfg
from utils.browser import apply_anti_detect_init_scripts
from utils.slider_captcha_async import slider_captcha_visible, solve_slider_if_present
from utils.tencent_captcha_async import (
    TENCENT_SHOW_HIJACK_INIT_JS,
    install_tencent_show_hijack,
    solve_tencent_if_present,
    tencent_captcha_visible,
)


def _liepin_storage_state_path_ready(path: Path) -> bool:
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


def _liepin_browser_context_kwargs(*, storage_state_path: str | None = None) -> dict:
    kw: dict = {
        "viewport": {"width": 1920, "height": 1080},
        "user_agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
        ),
    }
    if storage_state_path:
        kw["storage_state"] = storage_state_path
    return kw


async def _liepin_attach_init_scripts(context) -> None:
    await context.add_init_script(
        """
        Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
        """
    )
    await context.add_init_script(TENCENT_SHOW_HIJACK_INIT_JS)


async def _liepin_page_top_text_snippet(page, limit: int = 4000) -> str:
    try:
        txt = await page.evaluate(
            "(lim) => { const b = document.body; "
            "if (!b || !b.innerText) return ''; return b.innerText.slice(0, lim); }",
            limit,
        )
        return txt or ""
    except Exception:
        return ""


async def _liepin_page_has_login_register_text(page) -> bool:
    return "登录/注册" in (await _liepin_page_top_text_snippet(page))


async def _still_on_chinese_account_password_login(page) -> bool:
    u = (page.url or "").lower()
    if any(
        x in u
        for x in (
            "passport.",
            "openlogin",
            "/signin",
            "signin.",
            "passport.liepin",
        )
    ):
        return True
    try:
        acc = page.locator(
            "input[placeholder*='手机'], "
            "input[placeholder*='邮箱'], "
            "input[placeholder*='账号']"
        ).first
        pwd = page.locator("input[type='password'], input[placeholder*='密码']").first
        btn = (
            page.locator(
                ".ant-btn.ant-btn-danger, "
                ".ant-btn.ant-btn-dangerous, "
                ".ant-btn.ant-btn-dangerous.ant-btn-primary"
            )
            .filter(has_text=re.compile(r"登\s*录"))
            .first
        )
        if (
            await acc.is_visible(timeout=2000)
            and await pwd.is_visible(timeout=2000)
            and await btn.is_visible(timeout=800)
        ):
            return True
    except Exception:
        pass
    return False


async def liepin_login(
    account: str,
    password: str,
    storage_state_path: str,
    *,
    slider_wait_sec: float = 0.0,
    force_full_login: bool = False,
) -> tuple[bool, str]:
    """
    猎聘网自动登录（Async Playwright），成功后写入 storage_state JSON。

    - 步骤1：按是否存在可读 storage_state 打开猎聘首页；若正文不含「登录/注册」视为已登录并刷新 storage_state
    - 步骤2：若含「登录/注册」→ 账号密码登录 + 腾讯云/滑块验证码（2Captcha）→ 落盘 storage_state
    """
    account = (account or "").strip()
    password = (password or "").strip()
    if not account or not password:
        return False, "账号或密码为空，请在 config.py 填写 LOGIN_USERNAME / LOGIN_PASSWORD"

    out = Path(storage_state_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    acc_masked = account[:3] + "****" if len(account) > 3 else "****"
    cfg.log.info(
        "[liepin_login] 开始自动登录 account=%s storage_state_path=%s slider_wait_sec=%s force_full_login=%s",
        acc_masked,
        str(out),
        slider_wait_sec,
        force_full_login,
    )

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=False,
            args=["--disable-blink-features=AutomationControlled"],
        )
        try:
            state_str = str(out)
            use_storage_step1 = (not force_full_login) and _liepin_storage_state_path_ready(out)
            if force_full_login and _liepin_storage_state_path_ready(out):
                cfg.log.info(
                    "[liepin_login] force_full_login=True，步骤1 不按已有 storage 打开 path=%s",
                    state_str,
                )

            context = await browser.new_context(
                **_liepin_browser_context_kwargs(
                    storage_state_path=state_str if use_storage_step1 else None,
                )
            )
            await _liepin_attach_init_scripts(context)
            page = await context.new_page()
            await apply_anti_detect_init_scripts(page)
            await stealth_async(page)  # 在访问任何页面之前执行
            cfg.log.info(
                "[liepin_login] 步骤1：打开猎聘首页 https://www.liepin.com/（%s）",
                "带 storage_state" if use_storage_step1 else "无 storage_state",
            )
            await page.goto("https://c.liepin.com/", wait_until="domcontentloaded")
   
                
            await install_tencent_show_hijack(page)
    
                
            await page.wait_for_timeout(1200)

            if not await _liepin_page_has_login_register_text(page):
                cfg.log.info(
                    "[liepin_login] 步骤1：未检测到「登录/注册」，判定已登录；刷新 storage_state",
                )
  
                
                try:
                    await context.storage_state(path=state_str)
                except Exception as se:
                    cfg.log.debug("[liepin_login] 步骤1 刷新 storage_state 失败（可忽略）: %s", se)
                return True, "步骤1：已在登录态；已刷新 storage_state"

            cfg.log.info("[liepin_login] 步骤1：检测到「登录/注册」，进入步骤2（账号密码与验证码）")

            try:
                password_login_el = page.locator("text=密码登录")
                await password_login_el.wait_for(state="visible", timeout=10000)
                await password_login_el.click()
                cfg.log.debug("[liepin_login] [2/5] 已点击「密码登录」")
            except PlaywrightTimeoutError:
                cfg.log.debug("[liepin_login] [2/5] 未找到「密码登录」，可能已在密码登录表单")

            await page.wait_for_timeout(1500)

            account_input = page.locator(
                "input[placeholder*='手机'], "
                "input[placeholder*='邮箱'], "
                "input[placeholder*='账号']"
            )
            await account_input.first.wait_for(state="visible", timeout=10000)
            await account_input.first.fill(account)
            cfg.log.debug("[liepin_login] [3/5] 已输入账号: %s", acc_masked)

            password_input = page.locator("input[type='password'], input[placeholder*='密码']")
            await password_input.first.wait_for(state="visible", timeout=10000)
            await password_input.first.fill(password)
            cfg.log.debug("[liepin_login] [3/5] 已输入密码")

            try:
                agree_selectors = [
                    "text=同意猎聘",
                    "[class*='agree'], [class*='protocol']",
                    "input[type='checkbox']",
                ]
                agreed = False
                for selector in agree_selectors:
                    try:
                        el = page.locator(selector).first
                        if await el.is_visible(timeout=1500):
                            tag = await el.evaluate("node => node.tagName")
                            if tag == "INPUT":
                                if not await el.is_checked():
                                    await el.click()
                                agreed = True
                                break
                            await el.click()
                            agreed = True
                            break
                    except Exception:
                        continue
                if agreed:
                    cfg.log.debug("[liepin_login] [4/5] 已处理协议勾选")
            except Exception as e:
                cfg.log.debug("[liepin_login] [4/5] 协议步骤异常（可忽略）: %s", e)

            await page.wait_for_timeout(800)
            await install_tencent_show_hijack(page)

            try:
                btn_css = "#home-banner-login-container .login-content form > button"
                login_btn = page.locator(btn_css).filter(has_text=re.compile(r"登\s*录"))
                if await login_btn.count() == 0:
                    raise RuntimeError("未找到符合条件的登录按钮，请检查 DOM 结构是否变化")
                await login_btn.first.scroll_into_view_if_needed()
                await login_btn.first.wait_for(state="visible", timeout=15000)
                await login_btn.first.click()
                cfg.log.debug("[liepin_login] [5/5] 已点击登录按钮")
            except PlaywrightTimeoutError:
                raise

            captcha_key = (getattr(cfg, "captcha_api_key", None) or "").strip()
            if slider_wait_sec and slider_wait_sec > 0:
                deadline = time.monotonic() + float(slider_wait_sec)
                while time.monotonic() < deadline:
                    if (await tencent_captcha_visible(page)) or (await slider_captcha_visible(page)):
                        break
                    await page.wait_for_timeout(1000)
            else:
                await page.wait_for_timeout(1000)

            need_tencent = await tencent_captcha_visible(page)
            need_slider = await slider_captcha_visible(page)
            if need_tencent or need_slider:
                if not captcha_key:
                    return False, "检测到验证码（腾讯云或滑块），请在 config.py 填写 captcha_api_key（2Captcha）"

            if need_tencent:
                app_id_o = (getattr(cfg, "TENCENT_CAPTCHA_APP_ID", None) or "").strip() or None
                ok_tencent = await solve_tencent_if_present(
                    page, captcha_key, app_id_override=app_id_o
                )
                if not ok_tencent:
                    return False, "腾讯云验证码未通过；可配置 TENCENT_CAPTCHA_APP_ID 或检查 2Captcha 余额/任务"

            if await slider_captcha_visible(page):
                if not (await solve_slider_if_present(page, captcha_key, max_retries=2)):
                    return False, "滑块验证失败（含最多 2 次重试）"

            try:
                await page.wait_for_load_state("load", timeout=12000)
            except PlaywrightTimeoutError:
                pass

            await page.wait_for_timeout(2000)
            if await _still_on_chinese_account_password_login(page):
                return False, "仍在账号密码登录流程（可能账号/密码/验证码/风控未通过）"

            try:
                cfg.log.debug("[liepin_login] 登录成功后访问列表页，便于 cookie 写入")
                await page.goto(
                    "https://www.liepin.com/zhaopin/",
                    wait_until="domcontentloaded",
                    timeout=45000,
                )
                await page.wait_for_timeout(2000)
            except Exception as e:
                cfg.log.debug("[liepin_login] 登录后访问列表页（可忽略）: %s", e)

            await context.storage_state(path=str(out))
            cfg.log.info("[liepin_login] 自动登录成功，已写入 storage_state_path=%s", str(out))
            return True, "登录成功并已保存 storageState"
        finally:
            try:
                await browser.close()
            except Exception:
                pass

