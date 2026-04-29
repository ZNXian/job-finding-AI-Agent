# -*- coding: utf-8 -*-
"""Async 版腾讯云 Turing 验证码处理（Playwright async_api）。

说明：
- 保留 `utils/tencent_captcha.py`（sync 版）不动，供 legacy 脚本使用
- 本模块供异步爬虫/异步登录流程使用
"""

from __future__ import annotations

import asyncio
import re
from typing import List, Optional
from urllib.parse import urlparse

from playwright.async_api import Frame, Page, TimeoutError as PlaywrightTimeoutError

from config import log
from utils.two_captcha_api import create_tencent_task_proxyless, wait_task_solution_dict


TENCENT_SHOW_HIJACK_INIT_JS = r"""
(() => {
    if (window.__TencentHijackScheduled) return;
    window.__TencentHijackScheduled = true;
    function tryPatch() {
        if (window.__TencentShowAlreadyPatched) return true;
        const TC = window.TencentCaptcha;
        if (!TC || !TC.prototype || typeof TC.prototype.show !== 'function') return false;
        window.__TencentShowAlreadyPatched = true;
        const origShow = TC.prototype.show;
        TC.prototype.show = function(options, callback) {
            window.__captchaPromise = new Promise(function(resolve) {
                window.__captchaResolve = resolve;
            });
            const wrapped = function(res) {
                if (typeof callback === 'function') {
                    try { callback(res); } catch (e) {}
                }
            };
            return origShow.call(this, options, wrapped);
        };
        return true;
    }
    if (tryPatch()) return;
    const iv = setInterval(function() {
        try {
            if (tryPatch()) clearInterval(iv);
        } catch (e) {}
    }, 50);
    setTimeout(function() {
        try { clearInterval(iv); } catch (e) {}
    }, 120000);
})();
"""


async def install_tencent_show_hijack(page: Page) -> None:
    any_scheduled = False
    for fr in _frames(page):
        try:
            await fr.evaluate(TENCENT_SHOW_HIJACK_INIT_JS)
            any_scheduled = True
        except Exception as e:
            log.debug("[captcha/tencent] install hijack frame 跳过: %s", e)
    if any_scheduled:
        log.debug("[captcha/tencent] 已在所有可达 frame 调度 show 劫持（轮询至 SDK 出现）")
    else:
        log.debug("[captcha/tencent] install_tencent_show_hijack: 无可用 frame")


def _page_document_origin(page: Page) -> str:
    try:
        u = urlparse(page.url or "")
        if u.scheme and u.netloc:
            return f"{u.scheme}://{u.netloc}"
    except Exception:
        pass
    return "https://www.liepin.com"


async def _tcaptcha_iframe_frame(page: Page) -> Optional[Frame]:
    try:
        loc = page.locator("#tcaptcha_iframe").first
        if await loc.count() == 0:
            return None
        h = await loc.element_handle(timeout=8000)
        if h is None:
            return None
        cf = await h.content_frame()
        if cf is not None:
            log.debug("[captcha/tencent] 已解析 #tcaptcha_iframe -> frame url=%s", (cf.url or "")[:120])
            return cf
    except Exception as e:
        log.debug("[captcha/tencent] #tcaptcha_iframe content_frame 异常: %s", e)

    for fr in page.frames:
        if fr == page.main_frame:
            continue
        u = (fr.url or "").lower()
        if "turing.captcha.qcloud.com" in u or "captcha.qcloud.com" in u:
            log.debug("[captcha/tencent] 回退：按 URL 匹配 tcaptcha 子 frame url=%s", (fr.url or "")[:120])
            return fr
    return None


async def emit_tencent_cap_postmessage(page: Page, ticket: str, randstr: str) -> bool:
    t = (ticket or "").strip()
    r = (randstr or "").strip()
    if not t or not r:
        return False
    parent_origin = _page_document_origin(page)
    child = await _tcaptcha_iframe_frame(page)
    if child is None:
        return False
    try:
        diag = await child.evaluate(
            """([ticket, randstr, parentOrigin]) => {
                const t = (ticket || '').trim();
                const r = (randstr || '').trim();
                if (!t || !r) return { ok: false, reason: 'empty_in_js' };
                const messageStr = JSON.stringify({ message: { type: 3, ticket: t, randstr: r } });
                try {
                    window.parent.postMessage(messageStr, parentOrigin);
                    return { ok: true, parentOrigin: parentOrigin, messageStrLen: messageStr.length };
                } catch (e) {
                    return { ok: false, reason: String(e), parentOrigin: parentOrigin };
                }
            }""",
            [t, r, parent_origin],
        )
        ok = isinstance(diag, dict) and diag.get("ok") is True
        log.debug("[captcha/tencent] postMessage(type=3) ok=%s diag=%s", ok, diag)
        return ok
    except Exception as e:
        log.debug("[captcha/tencent] postMessage 执行异常: %s", e, exc_info=True)
        return False


async def apply_tencent_hijack_and_aq_injection(page: Page, ticket: str, randstr: str) -> bool:
    any_hit = False
    for fr in _frames(page):
        try:
            hit = await fr.evaluate(
                """([t, r]) => {
                    const o = { ret: 0, ticket: t, randstr: r };
                    let hit = false;
                    if (typeof window.__captchaResolve === 'function') {
                        try { window.__captchaResolve(o); hit = true; } catch (e) {}
                    }
                    const keys = Object.keys(window).filter((k) => k.startsWith('_aq_') && typeof window[k] === 'function');
                    for (let i = 0; i < keys.length; i++) {
                        try { window[keys[i]](o); hit = true; } catch (e) {}
                    }
                    return hit;
                }""",
                [ticket, randstr],
            )
            if hit:
                any_hit = True
        except Exception as e:
            log.debug("[captcha/tencent] apply hijack frame 异常: %s", e, exc_info=True)
    return any_hit


async def tencent_captcha_visible(page: Page) -> bool:
    selectors_outer = (
        "#tcaptcha_transform",
        "#tcaptcha_transform_dy",
        ".tcaptcha-transform",
        "#tcaptcha",
    )
    for sel in selectors_outer:
        try:
            loc = page.locator(sel).first
            if await loc.count() > 0 and await loc.is_visible(timeout=1200):
                return True
        except Exception:
            continue
    iframe_selectors = ("#tcaptcha_iframe", "iframe.tcaptcha-iframe", "iframe#tcaptcha_iframe")
    for sel in iframe_selectors:
        try:
            loc = page.locator(sel).first
            if await loc.count() > 0 and await loc.is_visible(timeout=1200):
                return True
        except Exception:
            continue
    try:
        if await page.locator('iframe[src*="turing.captcha.qcloud.com"]').first.is_visible(timeout=800):
            return True
    except PlaywrightTimeoutError:
        pass
    except Exception:
        pass
    try:
        if await page.locator('iframe[src*="captcha.tencent"]').first.is_visible(timeout=500):
            return True
    except Exception:
        pass
    return False


def _frames(page: Page) -> List[Frame]:
    seen = set()
    out: List[Frame] = []
    for fr in [page.main_frame, *page.frames]:
        if fr in seen:
            continue
        seen.add(fr)
        out.append(fr)
    return out


async def extract_tencent_app_id(page: Page) -> Optional[str]:
    try:
        html = await page.content()
    except Exception:
        html = ""
    patterns = [
        r"TencentCaptcha\s*\(\s*['\"](\d{5,})['\"]",
        r"new\s+TencentCaptcha\s*\(\s*['\"](\d{5,})['\"]",
        r"CapAppId\s*[=:]\s*['\"]?(\d{5,})",
        r"capAppId\s*[=:]\s*['\"]?(\d{5,})",
        r"['\"]appId['\"]\s*:\s*['\"](\d{5,})['\"]",
        r"aid\s*[=:]\s*['\"]?(\d{5,})",
        r"[?&]aid=(\d{5,})\b",
    ]
    for p in patterns:
        m = re.search(p, html or "", re.I | re.M)
        if m:
            return m.group(1)
    for fr in _frames(page):
        try:
            raw = await fr.evaluate("() => document.documentElement.outerHTML.slice(0, 500000)")
            if not raw or not isinstance(raw, str):
                continue
            for p in patterns:
                m = re.search(p, raw, re.I | re.M)
                if m:
                    return m.group(1)
        except Exception:
            continue
    return None


async def solve_tencent_if_present(
    page: Page,
    client_key: str,
    *,
    app_id_override: Optional[str] = None,
    max_retries: int = 2,
) -> bool:
    if not await tencent_captcha_visible(page):
        return True
    if not (client_key or "").strip():
        return False

    website_url = page.url or "https://www.liepin.com/"
    app_id = (app_id_override or "").strip() or (await extract_tencent_app_id(page))
    if not app_id:
        log.error("[captcha/tencent] 未解析到 appId，请在 config 设置 TENCENT_CAPTCHA_APP_ID")
        return False

    attempts = max_retries + 1
    for attempt in range(attempts):
        try:
            await install_tencent_show_hijack(page)
            tid = await asyncio.to_thread(
                create_tencent_task_proxyless,
                client_key.strip(),
                website_url,
                app_id,
            )
            sol = await asyncio.to_thread(
                wait_task_solution_dict,
                client_key,
                tid,
                3.0,
                180.0,
            )
            ticket = (sol.get("ticket") or "").strip() if isinstance(sol, dict) else ""
            randstr = (sol.get("randstr") or "").strip() if isinstance(sol, dict) else ""
            if not ticket:
                await asyncio.sleep(2.0)
                continue

            hij_hit = await apply_tencent_hijack_and_aq_injection(page, ticket, randstr)
            emit_ok = await emit_tencent_cap_postmessage(page, ticket, randstr)
            confirmed = bool(hij_hit or emit_ok)
            await asyncio.sleep(3.0)
            still = await tencent_captcha_visible(page)
            if not still and confirmed:
                return True
            await asyncio.sleep(2.0)
        except Exception as e:
            log.debug(
                "[captcha/tencent] 尝试 %s/%s 异常: %s",
                attempt + 1,
                attempts,
                e,
                exc_info=True,
            )
            await asyncio.sleep(2.0)
            continue
    return False

