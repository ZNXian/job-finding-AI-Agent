# -*- coding: utf-8 -*-
# AI 生成
# 生成目的：腾讯云 Turing 验证码（turing.captcha.qcloud.com）检测、解析 appId、2Captcha TencentTaskProxyless 取 ticket/randstr 并注入页面
# 参考：https://2captcha.com/api-docs/tencent

from __future__ import annotations

import re
import time
from typing import List, Optional
from urllib.parse import urlparse

from playwright.sync_api import Frame, Page, TimeoutError as PlaywrightTimeoutError

from config import log
from utils.two_captcha_api import create_tencent_task_proxyless, wait_task_solution_dict

# AI 生成
# 生成目的：作为 context.add_init_script 注入；轮询直到 window.TencentCaptcha 出现再 patch（避免仅 page.evaluate 主 frame 且早于 SDK 加载导致劫持未生效）
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


def install_tencent_show_hijack(page: Page) -> None:
    # AI 生成
    # 生成目的：在每个 frame 执行劫持安装（验证码 SDK 可能在子 frame，仅主 frame 的 page.evaluate 会漏掉）
    any_scheduled = False
    for fr in _frames(page):
        try:
            fr.evaluate(TENCENT_SHOW_HIJACK_INIT_JS)
            any_scheduled = True
        except Exception as e:
            log.debug("[captcha/tencent] install hijack frame 跳过: %s", e)
    if any_scheduled:
        log.debug(
            "[captcha/tencent] 已在所有可达 frame 调度 TencentCaptcha.prototype.show 劫持（轮询至 SDK 出现）"
        )
    else:
        log.debug("[captcha/tencent] install_tencent_show_hijack: 无可用 frame")


def _page_document_origin(page: Page) -> str:
    # AI 生成
    # 生成目的：postMessage 的 targetOrigin 须为当前页 origin（猎聘多子域）；无法用 page.url 时回退 www
    try:
        u = urlparse(page.url or "")
        if u.scheme and u.netloc:
            return f"{u.scheme}://{u.netloc}"
    except Exception:
        pass
    return "https://www.liepin.com"


def _tcaptcha_iframe_frame(page: Page) -> Optional[Frame]:
    # AI 生成
    # 生成目的：定位 #tcaptcha_iframe 对应子 frame，便于在其内执行 window.parent.postMessage
    try:
        loc = page.locator("#tcaptcha_iframe").first
        if loc.count() == 0:
            log.debug("[captcha/tencent] 未找到 #tcaptcha_iframe（count=0）")
            return None
        h = loc.element_handle(timeout=8000)
        if h is None:
            log.debug("[captcha/tencent] #tcaptcha_iframe element_handle 为空")
            return None
        cf = h.content_frame()
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
            log.debug(
                "[captcha/tencent] 回退：按 URL 匹配 tcaptcha 子 frame url=%s",
                (fr.url or "")[:120],
            )
            return fr
    log.debug("[captcha/tencent] 无法解析 tcaptcha 子 frame")
    return None


def emit_tencent_cap_postmessage(page: Page, ticket: str, randstr: str) -> bool:
    # AI 生成
    # 生成目的：在验证码 iframe 内调用 window.parent.postMessage(JSON, 父页 origin)；仅 type=3；返回是否 evaluate 成功
    t = (ticket or "").strip()
    r = (randstr or "").strip()
    if not t or not r:
        log.debug(
            "[captcha/tencent] postMessage: 跳过（ticket/randstr 未就绪 ticket_len=%s randstr_len=%s）",
            len(t),
            len(r),
        )
        return False
    parent_origin = _page_document_origin(page)
    child = _tcaptcha_iframe_frame(page)
    if child is None:
        log.debug("[captcha/tencent] postMessage: 无 tcaptcha iframe frame，跳过")
        return False
    try:
        diag = child.evaluate(
            """([ticket, randstr, parentOrigin]) => {
                const t = (ticket || '').trim();
                const r = (randstr || '').trim();
                if (!t || !r) {
                    return { ok: false, reason: 'empty_in_js' };
                }
                const messageStr = JSON.stringify({
                    message: { type: 3, ticket: t, randstr: r }
                });
                try {
                    window.parent.postMessage(messageStr, parentOrigin);
                    return {
                        ok: true,
                        parentOrigin: parentOrigin,
                        messageStrLen: messageStr.length,
                        iframeHref: typeof location !== 'undefined'
                            ? String(location.href).slice(0, 160)
                            : ''
                    };
                } catch (e) {
                    return { ok: false, reason: String(e), parentOrigin: parentOrigin };
                }
            }""",
            [t, r, parent_origin],
        )
        ok = isinstance(diag, dict) and diag.get("ok") is True
        log.debug(
            "[captcha/tencent] postMessage(type=3) iframe->parent parent_origin=%s ok=%s 诊断=%s",
            parent_origin,
            ok,
            diag,
        )
        return ok
    except Exception as e:
        log.debug("[captcha/tencent] postMessage 执行异常: %s", e, exc_info=True)
        return False


def apply_tencent_hijack_and_aq_injection(
    page: Page, ticket: str, randstr: str
) -> bool:
    # AI 生成
    # 生成目的：优先通过劫持留下的 __captchaResolve 与 window._aq_* 回调注入 2Captcha 的 ticket/randstr
    any_hit = False
    for fr in _frames(page):
        try:
            hit = fr.evaluate(
                """([t, r]) => {
                    const o = { ret: 0, ticket: t, randstr: r };
                    let hit = false;
                    if (typeof window.__captchaResolve === 'function') {
                        try {
                            window.__captchaResolve(o);
                            hit = true;
                        } catch (e) {}
                    }
                    const keys = Object.keys(window).filter(
                        (k) => k.startsWith('_aq_') && typeof window[k] === 'function'
                    );
                    for (let i = 0; i < keys.length; i++) {
                        try {
                            window[keys[i]](o);
                            hit = true;
                        } catch (e) {}
                    }
                    return hit;
                }""",
                [ticket, randstr],
            )
            if hit:
                any_hit = True
                log.debug(
                    "[captcha/tencent] __captchaResolve/_aq_ 注入命中 frame name=%s",
                    fr.name,
                )
        except Exception as e:
            log.debug("[captcha/tencent] apply hijack frame 异常: %s", e, exc_info=True)
    return any_hit


def tencent_captcha_visible(page: Page) -> bool:
    # AI 生成
    # 生成目的：是否出现腾讯云验证码（猎聘常见 #tcaptcha_transform / #tcaptcha_iframe、turing iframe 等）
    selectors_outer = (
        "#tcaptcha_transform",
        "#tcaptcha_transform_dy",
        ".tcaptcha-transform",
        "#tcaptcha",
    )
    for sel in selectors_outer:
        try:
            loc = page.locator(sel).first
            if loc.count() > 0 and loc.is_visible(timeout=1200):
                return True
        except Exception:
            continue
    iframe_selectors = ("#tcaptcha_iframe", "iframe.tcaptcha-iframe", "iframe#tcaptcha_iframe")
    for sel in iframe_selectors:
        try:
            loc = page.locator(sel).first
            if loc.count() > 0 and loc.is_visible(timeout=1200):
                return True
        except Exception:
            continue
    try:
        if page.locator('iframe[src*="turing.captcha.qcloud.com"]').first.is_visible(
            timeout=800
        ):
            return True
    except PlaywrightTimeoutError:
        pass
    except Exception:
        pass
    try:
        if page.locator('iframe[src*="captcha.tencent"]').first.is_visible(timeout=500):
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


def _aid_from_tcaptcha_iframe(page: Page) -> Optional[str]:
    # AI 生成
    # 生成目的：从 #tcaptcha_iframe 的 src 中解析 aid=（与 cap_union_new_show?aid= 一致，即 2Captcha 所需 appId）
    for sel in ("#tcaptcha_iframe", "iframe.tcaptcha-iframe", "iframe#tcaptcha_iframe"):
        try:
            loc = page.locator(sel).first
            if loc.count() == 0:
                continue
            src = (loc.get_attribute("src") or "").strip()
            if not src:
                continue
            m = re.search(r"[?&]aid=(\d+)", src, re.I)
            if m:
                return m.group(1)
        except Exception:
            continue
    return None


def extract_tencent_app_id(page: Page) -> Optional[str]:
    # AI 生成
    # 生成目的：优先从 tcaptcha iframe src 的 aid= 解析；再从 HTML / 脚本解析 TencentCaptcha(appId) 等
    aid = _aid_from_tcaptcha_iframe(page)
    if aid:
        log.debug("[captcha/tencent] appId 来自 #tcaptcha_iframe src aid=%s", aid)
        return aid
    try:
        html = page.content()
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
        m = re.search(p, html, re.I | re.M)
        if m:
            got = m.group(1)
            log.debug("[captcha/tencent] appId 来自页面 HTML 正则 pattern=%s value=%s", p[:40], got)
            return got
    for fr in _frames(page):
        try:
            raw = fr.evaluate("() => document.documentElement.outerHTML.slice(0, 500000)")
            if not raw or not isinstance(raw, str):
                continue
            for p in patterns:
                m = re.search(p, raw, re.I | re.M)
                if m:
                    got = m.group(1)
                    log.debug("[captcha/tencent] appId 来自 frame 正则 value=%s", got)
                    return got
        except Exception:
            continue
    log.debug("[captcha/tencent] extract_tencent_app_id: 未解析到 appId")
    return None


def solve_tencent_if_present(
    page: Page,
    client_key: str,
    *,
    app_id_override: Optional[str] = None,
    max_retries: int = 2,
) -> bool:
    # AI 生成
    # 生成目的：若存在腾讯云验证码则走 TencentTaskProxyless；失败最多重试 max_retries 次
    if not tencent_captcha_visible(page):
        return True
    if not (client_key or "").strip():
        log.debug("[captcha/tencent] captcha_key 为空，跳过 2Captcha")
        return False

    website_url = page.url or "https://www.liepin.com/"
    log.debug(
        "[captcha/tencent] 开始 solve_tencent_if_present url=%s override_app_id=%s",
        website_url,
        (app_id_override or "(none)")[:32],
    )

    app_id = (app_id_override or "").strip() or extract_tencent_app_id(page)
    if not app_id:
        log.error(
            "[captcha/tencent] 未解析到 appId，请在 config 设置 TENCENT_CAPTCHA_APP_ID"
        )
        log.debug(
            "[captcha/tencent] 未解析到腾讯云 appId，请在 config.py 设置 TENCENT_CAPTCHA_APP_ID"
        )
        return False

    log.debug("[captcha/tencent] 使用 appId=%s 调用 TencentTaskProxyless", app_id)
    attempts = max_retries + 1

    for attempt in range(attempts):
        try:
            install_tencent_show_hijack(page)
            log.debug(
                "[captcha/tencent] 尝试 %s/%s: createTask …",
                attempt + 1,
                attempts,
            )
            tid = create_tencent_task_proxyless(
                client_key.strip(), website_url, app_id
            )
            log.debug("[captcha/tencent] taskId=%s，轮询 getTaskResult …", tid)
            sol = wait_task_solution_dict(client_key, tid, max_wait=180.0)
            ticket = (sol.get("ticket") or "").strip()
            randstr = (sol.get("randstr") or "").strip()
            log.debug(
                "[captcha/tencent] solution ret=%s keys=%s ticket_len=%s randstr_preview=%s",
                sol.get("ret"),
                list(sol.keys()),
                len(ticket),
                repr(randstr[:12]) if randstr else "",
            )
            if not ticket:
                log.debug(
                    "[captcha/tencent] 本次 solution 无 ticket，2s 后重试。原始 solution=%s",
                    sol,
                )
                time.sleep(2.0)
                continue
            hij_hit = apply_tencent_hijack_and_aq_injection(page, ticket, randstr)
            log.debug("[captcha/tencent] apply_tencent_hijack_and_aq_injection -> %s", hij_hit)
            emit_ok = emit_tencent_cap_postmessage(page, ticket, randstr)
            log.debug("[captcha/tencent] emit_tencent_cap_postmessage -> %s", emit_ok)
            confirmed = bool(hij_hit or emit_ok)
            log.debug(
                "[captcha/tencent] 注入汇总 confirmed=%s hijack=%s emit_postMessage=%s",
                confirmed,
                hij_hit,
                emit_ok,
            )
            log.debug("[captcha/tencent] 等待 3.0s 后检测验证码层…")
            time.sleep(3.0)
            still = tencent_captcha_visible(page)
            log.debug("[captcha/tencent] 注入后 tencent_captcha_visible=%s", still)
            if not still:
                if confirmed:
                    log.debug("[captcha/tencent] 验证码层已消失且回调/postMessage 命中，判定成功")
                    return True
                log.debug(
                    "[captcha/tencent] 验证码层已消失但未命中回调/postMessage（继续重试）"
                )
                time.sleep(2.0)
                continue
        except Exception as e:
            log.debug(
                "[captcha/tencent] 尝试 %s/%s 异常: %s",
                attempt + 1,
                attempts,
                e,
                exc_info=True,
            )
            time.sleep(2.0)
            continue

    final_vis = tencent_captcha_visible(page)
    log.debug(
        "[captcha/tencent] 重试耗尽，最终 tencent_captcha_visible=%s，返回 False",
        final_vis,
    )
    return False
