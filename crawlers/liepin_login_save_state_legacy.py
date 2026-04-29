# -*- coding: utf-8 -*-
# AI 生成
# 生成目的：猎聘账号密码自动登录；将 Playwright storage_state 写入 config.LIEPIN_STORAGE_STATE_PATH；优先复用已有文件。
#
# =============================================================================
# 当前自动登录：逻辑与步骤（与代码一致，函数 liepin_login）
# =============================================================================
#
# 零、总览
#   - 每次调用先打一条 cfg.log.info「开始自动登录」（脱敏账号、路径、slider_wait_sec、force_full_login）。
#   - 步骤1：是否存在可读 storage_state → 无则匿名 context，有则带 storage 打开 https://www.liepin.com/ ；
#     取页面上方正文片段，若不含「登录/注册」则视为已登录 → 刷新 storage_state 后安全 return。
#   - 步骤2：若含「登录/注册」→ 沿用原账号密码、协议、登录按钮、腾讯云/滑块、落盘 storage_state 流程。
#
# 一、步骤1（与 _liepin_page_has_login_register_text 一致）
#   - _liepin_attach_init_scripts → goto 首页 → install_tencent_show_hijack → sleep(1.2)。
#   - 正文前约 4000 字内检索「登录/注册」（与爬虫列表未登录顶栏文案一致）。
#
# 二、步骤2（表单登录，同一 BrowserContext / Page，不另起 context）
#   - 步骤与 debug 前缀 [2/5]…[5/5] 对应：
#       1）已在首页；2）点击「密码登录」（超时则视为已在密码表单）；3）填账号、密码；4）协议勾选兜底；
#       5）点击 #home-banner-login-container .login-content form > button（文案含「登录」）。
#
# 三、验证码（仅步骤2，在点击「登录」之后）
#   - --slider-wait SEC > 0：在 SEC 秒内轮询直至 tencent_captcha_visible 或 slider_captcha_visible；否则 sleep(1)。
#   - 若需腾讯云或极验之一且未配置 captcha_api_key：return False。
#   - 腾讯云：solve_tencent_if_present（2Captcha TencentTaskProxyless；各 frame __captchaResolve/_aq_*；iframe 内
#     parent.postMessage(JSON 字符串 type=3)）。细节见 utils/tencent_captcha.py。
#   - 极验类滑块：solve_slider_if_present（CoordinatesTask），见 utils/slider_captcha.py。
#
# 四、步骤2 成功判定与落盘
#   - wait_for_load_state("load")，sleep(2)；若 _still_on_chinese_account_password_login 仍为 True → 失败返回。
#   - 否则 context.storage_state(path=…) 写入 LIEPIN_STORAGE_STATE_PATH；cfg.log.info「自动登录成功」；return True。
#
# 五、依赖配置（config.py）
#   - LOGIN_USERNAME / LOGIN_PASSWORD；LIEPIN_STORAGE_STATE_PATH；captcha_api_key（出现验证码时必填）；
#     TENCENT_CAPTCHA_APP_ID（可选，留空由 tencent_captcha 从 iframe src 解析 aid）。
#
# 六、日志
#   - INFO：开始自动登录；步骤1 打开方式；已登录安全退出 / 检测到「登录/注册」进入步骤2；自动登录成功。
#   - DEBUG：各步骤细节、[captcha/tencent] 等；根 logger 为 INFO 时默认不显示，排查时调至 DEBUG。
#   - ERROR：腾讯云验证码未通过等。
#
# 七、脚本入口 main()
#   - 读取 config 账号密码与路径，调用 liepin_login(..., slider_wait_sec=--slider-wait)；进程退出码 0/1 表示成功/失败。
#
# 页面表单相关逻辑最初由智谱生成，后经人工分析+DeepSeek分析+cursor参考迭代。
# =============================================================================

from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

import config as cfg
from utils.slider_captcha import solve_slider_if_present, slider_captcha_visible
from utils.tencent_captcha import (
    TENCENT_SHOW_HIJACK_INIT_JS,
    install_tencent_show_hijack,
    solve_tencent_if_present,
    tencent_captcha_visible,
)


def _liepin_storage_state_path_ready(path: Path) -> bool:
    # AI 生成
    # 生成目的：判断是否存在可交给 Playwright 加载的 storage_state 文件
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


def _liepin_browser_context_kwargs(*, storage_state_path: str | None = None) -> dict:
    # AI 生成
    # 生成目的：统一 viewport / UA，可选传入已有 storage_state 路径
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


def _liepin_attach_init_scripts(context) -> None:
    # AI 生成
    # 生成目的：每个 context 创建后注入 webdriver 隐藏与腾讯云劫持
    context.add_init_script(
        """
        Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
        """
    )
    context.add_init_script(TENCENT_SHOW_HIJACK_INIT_JS)


def _liepin_page_top_text_snippet(page, limit: int = 4000) -> str:
    # AI 生成
    # 生成目的：步骤1 取页面正文前若干字符，用于判断是否出现顶栏「登录/注册」
    try:
        return (
            page.evaluate(
                "(lim) => { const b = document.body; "
                "if (!b || !b.innerText) return ''; return b.innerText.slice(0, lim); }",
                limit,
            )
            or ""
        )
    except Exception:
        return ""


def _liepin_page_has_login_register_text(page) -> bool:
    return "登录/注册" in _liepin_page_top_text_snippet(page)


def _still_on_chinese_account_password_login(page) -> bool:
    # AI 生成
    # 生成目的：判断是否仍停留在「中文账号 + 密码」登录态（URL 或页面上账号/密码/登录按钮仍同时可见）
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
        pwd = page.locator(
            "input[type='password'], input[placeholder*='密码']"
        ).first
        btn = page.locator(
            ".ant-btn.ant-btn-danger, "
            ".ant-btn.ant-btn-dangerous, "
            ".ant-btn.ant-btn-dangerous.ant-btn-primary"
        ).filter(has_text=re.compile(r"登\s*录")).first
        if (
            acc.is_visible(timeout=2000)
            and pwd.is_visible(timeout=2000)
            and btn.is_visible(timeout=800)
        ):
            return True
    except Exception:
        pass
    return False


_CKID_COOKIE_NAMES = ("ckId", "ck_id")


def _liepin_report_ckid_after_login(context) -> None:
    # AI 生成
    # 生成目的：登录成功后从 BrowserContext 读取 ckId / ck_id，便于联调与校验会话
    try:
        cookies = context.cookies()
        ck_name = None
        ck_val = None
        for c in cookies:
            n = c.get("name") or ""
            if n in _CKID_COOKIE_NAMES:
                ck_name = n
                ck_val = c.get("value")
                break
        if ck_val:
            print(f"获取到 {ck_name}: {ck_val}")
            cfg.log.info("[liepin_login] 获取到 %s: %s", ck_name, ck_val)
        else:
            names = [c.get("name") for c in cookies if c.get("name")]
            preview = ", ".join(sorted(set(names))[:40])
            print("未找到 ckId / ck_id cookie")
            cfg.log.warning(
                "[liepin_login] 未找到 ckId / ck_id；当前 context 约 %s 个 cookie，name 抽样: %s",
                len(cookies),
                preview or "(无)",
            )
    except Exception as e:
        cfg.log.debug("[liepin_login] 读取 ckId 时列举 cookies 失败: %s", e)


def liepin_login(
    account: str,
    password: str,
    storage_state_path: str,
    *,
    slider_wait_sec: float = 0.0,
    force_full_login: bool = False,
) -> tuple[bool, str]:
    """
    猎聘网自动登录（Playwright），成功后写入 storage_state JSON。

    步骤1：按是否存在可读 storage_state 打开猎聘首页；若页面上方正文不含「登录/注册」则
    视为已登录，刷新 storage_state 后安全返回。若含「登录/注册」则进入步骤2（账号密码与验证码），
    成功后保存新的 storage_state。

    参数:
        account            登录账号（手机号/邮箱）
        password           登录密码
        storage_state_path Playwright context.storage_state 保存路径
        slider_wait_sec    点击「登录」后，最长等待滑块出现的秒数；>0 时轮询便于与 2Captcha 联调（默认 0 仅短暂检测）
        force_full_login   为 True 时步骤1 始终不带 storage_state 打开（便于强制进入步骤2）

    返回:
        (success, message)
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

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=False,
                args=["--disable-blink-features=AutomationControlled"],
            )
            context = None
            state_str = str(out)
            use_storage_step1 = (not force_full_login) and _liepin_storage_state_path_ready(out)
            if force_full_login and _liepin_storage_state_path_ready(out):
                cfg.log.info(
                    "[liepin_login] force_full_login=True，步骤1 不按已有 storage 打开 path=%s",
                    state_str,
                )
            try:
                kw = _liepin_browser_context_kwargs(
                    storage_state_path=state_str if use_storage_step1 else None,
                )
                context = browser.new_context(**kw)
                _liepin_attach_init_scripts(context)
                page = context.new_page()
                cfg.log.info(
                    "[liepin_login] 步骤1：打开猎聘首页 https://www.liepin.com/（%s）",
                    "带 storage_state" if use_storage_step1 else "无 storage_state",
                )
                page.goto("https://www.liepin.com/", wait_until="domcontentloaded")
                install_tencent_show_hijack(page)
                time.sleep(1.2)
                top_preview = (
                    (_liepin_page_top_text_snippet(page)[:240] or "").replace("\n", "\\n")
                )
                cfg.log.debug("[liepin_login] 步骤1：正文开头预览: %s…", top_preview[:200])

                if not _liepin_page_has_login_register_text(page):
                    cfg.log.info(
                        "[liepin_login] 步骤1：页面上方正文不含「登录/注册」，判定已登录，"
                        "安全退出并刷新 storage_state",
                    )
                    try:
                        context.storage_state(path=state_str)
                    except Exception as se:
                        cfg.log.debug(
                            "[liepin_login] 步骤1 刷新 storage_state 失败（可忽略）: %s",
                            se,
                        )
                    _liepin_report_ckid_after_login(context)
                    return (
                        True,
                        "步骤1：未检测到「登录/注册」，已在登录态；已刷新 storage_state（若可写）",
                    )

                cfg.log.info(
                    "[liepin_login] 步骤1：检测到「登录/注册」，进入步骤2（账号密码与验证码）",
                )
                cfg.log.debug("[liepin_login] [步骤2/5] 已在猎聘网")

                try:
                    password_login_el = page.locator("text=密码登录")
                    password_login_el.wait_for(state="visible", timeout=10000)
                    password_login_el.click()
                    cfg.log.debug("[liepin_login] [2/5] 已点击「密码登录」")
                except PlaywrightTimeoutError:
                    cfg.log.debug("[liepin_login] [2/5] 未找到「密码登录」，可能已在密码登录表单")

                time.sleep(1.5)

                account_input = page.locator(
                    "input[placeholder*='手机'], "
                    "input[placeholder*='邮箱'], "
                    "input[placeholder*='账号']"
                )
                account_input.first.wait_for(state="visible", timeout=10000)
                account_input.first.fill(account)
                masked = account[:3] + "****" if len(account) > 3 else "****"
                cfg.log.debug("[liepin_login] [3/5] 已输入账号: %s", masked)

                password_input = page.locator(
                    "input[type='password'], input[placeholder*='密码']"
                )
                password_input.first.wait_for(state="visible", timeout=10000)
                password_input.first.fill(password)
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
                            if el.is_visible(timeout=1500):
                                tag = el.evaluate("node => node.tagName")
                                if tag == "INPUT":
                                    if not el.is_checked():
                                        el.click()
                                    agreed = True
                                    break
                                el.click()
                                agreed = True
                                break
                        except Exception:
                            continue
                    if agreed:
                        cfg.log.debug("[liepin_login] [4/5] 已处理猎聘协议勾选")
                    else:
                        cfg.log.debug("[liepin_login] [4/5] 未显式勾选协议（可能已默认同意），继续")
                except Exception as e:
                    cfg.log.debug("[liepin_login] [4/5] 协议步骤异常（可忽略）: %s", e)

                time.sleep(0.8)
                install_tencent_show_hijack(page)

                # ====== 第5步：点击「登录」按钮 ======
                try:
                    # 根据提供的 DOM 结构，使用精确且健壮的 CSS 选择器定位按钮
                    # 去除中间脆弱的逐层 > div 依赖，保留核心锚点
                    btn_css = "#home-banner-login-container .login-content form > button"
                    login_btn = page.locator(btn_css).filter(
                        has_text=re.compile(r"登\s*录")
                    )
                    btn_count = login_btn.count()
                    cfg.log.debug("[liepin_login] [5/5] 定位到 %s 个符合条件的登录按钮", btn_count)
                    if btn_count == 0:
                        raise Exception(
                            "未找到符合条件的登录按钮，请检查 DOM 结构是否变化"
                        )
                    login_btn.first.scroll_into_view_if_needed()
                    login_btn.first.wait_for(state="visible", timeout=15000)
                    login_btn.first.click()
                    cfg.log.debug("[liepin_login] [5/5] 已点击登录按钮")
                except PlaywrightTimeoutError:
                    page.screenshot(path=str(_ROOT / "login_btn_timeout_debug.png"))
                    cfg.log.debug(
                        "[liepin_login] 登录按钮等待超时，已保存截图: %s",
                        _ROOT / "login_btn_timeout_debug.png",
                    )
                    raise
                except Exception as e:
                    cfg.log.debug("[liepin_login] 第5步出错: %s", e)
                    raise

                captcha_key = (getattr(cfg, "captcha_api_key", None) or "").strip()
                if slider_wait_sec and slider_wait_sec > 0:
                    cfg.log.debug(
                        "[liepin_login] 联调模式：%ss 内轮询腾讯云/极验类滑块…",
                        int(slider_wait_sec),
                    )
                    deadline = time.monotonic() + float(slider_wait_sec)
                    while time.monotonic() < deadline:
                        if tencent_captcha_visible(page) or slider_captcha_visible(page):
                            break
                        time.sleep(1.0)
                else:
                    time.sleep(1.0)

                need_tencent = tencent_captcha_visible(page)
                need_slider = slider_captcha_visible(page)
                if need_tencent or need_slider:
                    if not captcha_key:
                        return (
                            False,
                            "检测到验证码（腾讯云或滑块），请在 config.py 填写 captcha_api_key（2Captcha）",
                        )
                if need_tencent:
                    cfg.log.debug(
                        "[liepin_login] 检测到腾讯云验证码，使用 2Captcha TencentTaskProxyless…"
                    )
                    app_id_o = (getattr(cfg, "TENCENT_CAPTCHA_APP_ID", None) or "").strip() or None
                    ok_tencent = solve_tencent_if_present(
                        page, captcha_key, app_id_override=app_id_o
                    )
                    cfg.log.debug("[liepin_login] solve_tencent_if_present -> %s", ok_tencent)
                    if not ok_tencent:
                        cfg.log.error(
                            "[liepin_login] 腾讯云验证码未通过，将 logging 调至 DEBUG 查看 [captcha/tencent]"
                        )
                        return (
                            False,
                            "腾讯云验证码未通过；可配置 TENCENT_CAPTCHA_APP_ID 或检查 2Captcha 余额/任务",
                        )
                    cfg.log.debug("[liepin_login] 腾讯云验证码已处理")
                if slider_captcha_visible(page):
                    cfg.log.debug(
                        "[liepin_login] 检测到极验类滑块，使用 2Captcha CoordinatesTask…"
                    )
                    if not solve_slider_if_present(page, captcha_key, max_retries=2):
                        return False, "滑块验证失败（含最多 2 次重试）"
                    cfg.log.debug("[liepin_login] 极验类滑块已通过或已关闭")
                elif (
                    slider_wait_sec
                    and slider_wait_sec > 0
                    and not need_tencent
                    and not need_slider
                ):
                    cfg.log.debug(
                        "[liepin_login] 等待窗口内未出现腾讯云/极验控件，继续后续流程",
                    )

                # 猎聘等站点长连接多，networkidle 常永久达不到导致脚本挂死
                try:
                    page.wait_for_load_state("load", timeout=12000)
                except PlaywrightTimeoutError:
                    pass

                time.sleep(2)
                if _still_on_chinese_account_password_login(page):
                    return (
                        False,
                        "仍在中文账号密码登录流程（passport/openlogin 等，或仍可见账号/密码/登录按钮），"
                        "请检查账号密码、滑块/验证码或风控",
                    )

                try:
                    cfg.log.debug(
                        "[liepin_login] 登录成功后访问招聘列表页，便于业务侧 cookie（如 ckId）写入当前 context",
                    )
                    page.goto(
                        "https://www.liepin.com/zhaopin/",
                        wait_until="domcontentloaded",
                        timeout=45000,
                    )
                    time.sleep(2)
                except Exception as e:
                    cfg.log.debug("[liepin_login] 登录后访问列表页（可忽略）: %s", e)

                context.storage_state(path=str(out))
                cfg.log.debug("[liepin_login] 已保存 storageState: %s", out)
                cfg.log.info(
                    "[liepin_login] 自动登录成功，已写入 storage_state_path=%s",
                    str(out),
                )
                # 登录成功后（完整表单登录）
                _liepin_report_ckid_after_login(context)
                return True, "登录成功并已保存 storageState"
            finally:
                if context is not None:
                    try:
                        context.close()
                    except Exception:
                        pass
                browser.close()

    except PlaywrightTimeoutError:
        return False, "页面元素加载超时"
    except Exception as e:
        return False, str(e)


def main() -> int:
    parser = argparse.ArgumentParser(description="猎聘自动登录并保存 Playwright storageState")
    parser.add_argument(
        "--slider-wait",
        type=float,
        default=0.0,
        metavar="SEC",
        help="点击登录后最长等待滑块出现的秒数（>0 用于与 2Captcha 滑块联调；需配置 captcha_api_key）",
    )
    parser.add_argument(
        "--full-login",
        action="store_true",
        help="步骤1 始终不带 storage_state 打开首页，从而强制进入步骤2（表单登录）",
    )
    args = parser.parse_args()

    user = (getattr(cfg, "LOGIN_USERNAME", None) or "").strip()
    pwd = (getattr(cfg, "LOGIN_PASSWORD", None) or "").strip()
    path = getattr(cfg, "LIEPIN_STORAGE_STATE_PATH", None) or str(
        _ROOT / "browser_data" / "liepin_storage_state.json"
    )
    ok, msg = liepin_login(
        user,
        pwd,
        path,
        slider_wait_sec=float(args.slider_wait or 0.0),
        force_full_login=bool(args.full_login),
    )
    cfg.log.debug("[liepin_login] 结果: ok=%s msg=%s", ok, msg)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
