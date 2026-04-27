# -*- coding: utf-8 -*-
# AI 生成
# 生成目的：常规滑块（拼图类）通用流程：定位 → 截图/画布 → 2Captcha CoordinatesTask → Playwright 拖拽；含失败重试

"""
## 如何定位滑块与背景图（通用思路）

1. **先找 iframe**  
   登录态/通行证页常把验证码放在子域 iframe 里。应对 `page.frames` 遍历（含 `main_frame`），在每个 frame 内再查元素。

2. **滑块把手（可拖块）**  
   依次尝试可见选择器（命中一个即可）：
   - `.geetest_slider_button`、`[class*='geetest_slider_button']`（极验系）
   - `[class*='slider-button']`、`[class*='slider_btn']`、`[class*='handler']`
   - `[role='slider']`  
   用 `locator.first` + `is_visible(timeout=…)`，取到后记录其 `bounding_box()` 中心作为拖拽起点。

3. **背景图 / 缺口图**  
   优先级（从稳到宽）：
   - **Canvas**：同 frame 内面积最大、宽高合理的 `canvas`，用 Playwright `locator.screenshot()` 得到 PNG（与 2Captcha 坐标同原点；避免在 iframe 内用 `getBoundingClientRect` 与顶层坐标混用）。
   - **整块容器截图**：无合适 canvas 时，对 `.geetest_box` 等容器 `locator.screenshot()`，并以该元素 `bounding_box()` 左上角为坐标原点。
   - **裁剪兜底**：再失败则按滑块附近 `page.screenshot(clip=…)`，以 clip 左上角为原点。

4. **坐标含义与拖拽**  
   2Captcha `CoordinatesTask` 返回缺口中心在**提交图片**坐标系下的 (x, y)。若图片左缘与轨道左缘对齐，水平拖拽量近似为 `x - piece_half_width`（拼图块半宽可用把手宽度比例估算，默认约 20px）。用 `mouse.move → down → move(steps=…) → up` 模拟人手，带少量竖直抖动。

5. **不适用场景**  
   极验 v4 / 无影链、纯行为风控、非拼图类旋转验证码等需其它 2Captcha 任务类型，本模块不覆盖。
"""

from __future__ import annotations

import base64
import random
import time
from typing import List, Optional, Tuple

from playwright.sync_api import Frame, Locator, Page, TimeoutError as PlaywrightTimeoutError

from utils.two_captcha_api import solve_coordinates_image

# 滑块把手候选（按常见程度排序）
_HANDLE_SELECTORS: List[str] = [
    ".geetest_slider_button",
    "[class*='geetest_slider_button']",
    "[class*='geetest_slider'] >> [class*='button']",
    "[class*='slider-button']",
    "[class*='slider_btn']",
    "[class*='captcha'] [class*='handler']",
    "[role='slider']",
]

# 验证码整体容器（用于截图兜底）
_CONTAINER_SELECTORS: List[str] = [
    ".geetest_box",
    ".geetest_widget",
    "[class*='geetest_box']",
    "[class*='geetest_widget']",
    "[class*='captcha-content']",
    "[class*='slider-captcha']",
    "[class*='verifybox']",
]


def _frames(page: Page) -> List[Frame]:
    seen = set()
    out: List[Frame] = []
    for fr in [page.main_frame, *page.frames]:
        if fr in seen:
            continue
        seen.add(fr)
        out.append(fr)
    return out


def _first_visible_handle(frame: Frame) -> Optional[Locator]:
    for sel in _HANDLE_SELECTORS:
        loc = frame.locator(sel).first
        try:
            if loc.is_visible(timeout=800):
                return loc
        except PlaywrightTimeoutError:
            continue
        except Exception:
            continue
    return None


def _best_canvas_b64_and_origin(frame: Frame) -> Tuple[Optional[str], Optional[dict]]:
    # AI 生成
    # 生成目的：最大可见 canvas 的 PNG base64 + Playwright bounding_box（与截图像素、鼠标坐标一致）
    canvases = frame.locator("canvas")
    try:
        n = canvases.count()
    except Exception:
        return None, None
    best_i = -1
    best_area = 0.0
    for i in range(n):
        loc = canvases.nth(i)
        try:
            if not loc.is_visible(timeout=400):
                continue
            bb = loc.bounding_box()
            if not bb or bb["width"] < 80 or bb["height"] < 24:
                continue
            area = bb["width"] * bb["height"]
            if area > best_area:
                best_area = area
                best_i = i
        except Exception:
            continue
    if best_i < 0:
        return None, None
    loc = canvases.nth(best_i)
    try:
        png = loc.screenshot(type="png")
        bb = loc.bounding_box()
    except Exception:
        return None, None
    if not bb:
        return None, None
    b64 = base64.b64encode(png).decode("ascii")
    if len(b64) < 200:
        return None, None
    return b64, dict(bb)


def _container_screenshot_pack(
    frame: Frame, handle: Locator
) -> Optional[Tuple[str, dict]]:
    # AI 生成
    # 生成目的：对验证码容器截图，并返回该元素 bounding_box 作为坐标原点
    for sel in _CONTAINER_SELECTORS:
        loc = frame.locator(sel).first
        try:
            if loc.is_visible(timeout=500):
                bb = loc.bounding_box()
                if not bb:
                    continue
                png = loc.screenshot(type="png")
                return base64.b64encode(png).decode("ascii"), bb
        except Exception:
            continue
    try:
        box = handle.bounding_box()
        if not box:
            return None
        clip = {
            "x": max(0.0, box["x"] - 320),
            "y": max(0.0, box["y"] - 120),
            "width": min(520.0, 1920.0),
            "height": min(220.0, 1080.0),
        }
        png = frame.page.screenshot(type="png", clip=clip)
        return base64.b64encode(png).decode("ascii"), dict(clip)
    except Exception:
        return None


def _capture_puzzle_image_and_origin(
    frame: Frame, handle: Locator,
) -> Tuple[Optional[str], Optional[dict]]:
    # AI 生成
    # 生成目的：(图片 base64 或 data URL, 图片左上角在页面中的 bbox：x,y,width,height)
    b64, bb = _best_canvas_b64_and_origin(frame)
    if b64 and bb:
        return b64, bb
    cp = _container_screenshot_pack(frame, handle)
    if cp:
        b64, bb = cp
        return b64, bb
    return None, None


def _pick_handle_and_frame(page: Page) -> Tuple[Optional[Frame], Optional[Locator]]:
    for fr in _frames(page):
        h = _first_visible_handle(fr)
        if h is not None:
            return fr, h
    return None, None


def slider_captcha_visible(page: Page) -> bool:
    _, h = _pick_handle_and_frame(page)
    return h is not None


def _drag_slider(page: Page, handle: Locator, *, delta_x: float) -> None:
    # AI 生成
    # 生成目的：从把手中心按下，水平拖移 delta_x，带随机步数与轻微抖动
    box = handle.bounding_box()
    if not box:
        raise RuntimeError("无法获取滑块把手位置")
    sx = box["x"] + box["width"] / 2
    sy = box["y"] + box["height"] / 2
    steps = random.randint(35, 70)
    page.mouse.move(sx, sy)
    page.mouse.down()
    for i in range(1, steps + 1):
        t = i / steps
        ease = t * t * (3 - 2 * t)
        nx = sx + delta_x * ease + random.uniform(-0.6, 0.6)
        ny = sy + random.uniform(-1.2, 1.2)
        page.mouse.move(nx, ny)
    page.mouse.up()


def solve_slider_if_present(
    page: Page,
    client_key: str,
    *,
    max_retries: int = 2,
    comment: Optional[str] = None,
) -> bool:
    # AI 生成
    # 生成目的：若存在常规滑块则调用 2Captcha Coordinates + 拖拽；失败最多重试 max_retries 次（共 1+max_retries 次尝试）
    if not (client_key or "").strip():
        return not slider_captcha_visible(page)

    text = comment or (
        "Click the center of the puzzle GAP (hollow slot) where the piece should fit. "
        "缺口中心 / 拼图应嵌入位置的中心坐标。"
    )
    attempts = max_retries + 1

    for attempt in range(attempts):
        fr, handle = _pick_handle_and_frame(page)
        if handle is None:
            return True

        img, origin = _capture_puzzle_image_and_origin(fr, handle)
        if not img or not origin:
            time.sleep(1.0)
            continue

        try:
            coords = solve_coordinates_image(client_key.strip(), img, text)
        except Exception:
            time.sleep(1.5)
            continue

        if not coords:
            continue
        cx = float(coords[0].get("x", 0))

        hbox = handle.bounding_box()
        if not hbox:
            continue
        piece_half = min(36.0, max(12.0, hbox["width"] * 0.45))

        gap_center_x = origin["x"] + cx
        start_x = hbox["x"] + hbox["width"] / 2
        delta_x = gap_center_x - start_x - piece_half

        try:
            _drag_slider(page, handle, delta_x=delta_x)
        except Exception:
            time.sleep(1.0)
            continue

        time.sleep(1.2)
        if not slider_captcha_visible(page):
            return True

    return not slider_captcha_visible(page)
