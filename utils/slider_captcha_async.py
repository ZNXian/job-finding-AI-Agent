# -*- coding: utf-8 -*-
"""Async 版常规拼图滑块（2Captcha CoordinatesTask + Playwright async_api 拖拽）。

说明：
- 保留 `utils/slider_captcha.py`（sync 版）不动，供 legacy 使用
"""

from __future__ import annotations

import asyncio
import base64
import random
from typing import List, Optional, Tuple

from playwright.async_api import Frame, Locator, Page, TimeoutError as PlaywrightTimeoutError

from utils.two_captcha_api import solve_coordinates_image


_HANDLE_SELECTORS: List[str] = [
    ".geetest_slider_button",
    "[class*='geetest_slider_button']",
    "[class*='geetest_slider'] >> [class*='button']",
    "[class*='slider-button']",
    "[class*='slider_btn']",
    "[class*='captcha'] [class*='handler']",
    "[role='slider']",
]

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


async def _first_visible_handle(frame: Frame) -> Optional[Locator]:
    for sel in _HANDLE_SELECTORS:
        loc = frame.locator(sel).first
        try:
            if await loc.is_visible(timeout=800):
                return loc
        except PlaywrightTimeoutError:
            continue
        except Exception:
            continue
    return None


async def _best_canvas_b64_and_origin(frame: Frame) -> Tuple[Optional[str], Optional[dict]]:
    canvases = frame.locator("canvas")
    try:
        n = await canvases.count()
    except Exception:
        return None, None
    best_i = -1
    best_area = 0.0
    for i in range(n):
        loc = canvases.nth(i)
        try:
            if not await loc.is_visible(timeout=400):
                continue
            bb = await loc.bounding_box()
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
        png = await loc.screenshot(type="png")
        bb = await loc.bounding_box()
    except Exception:
        return None, None
    if not bb:
        return None, None
    b64 = base64.b64encode(png).decode("ascii")
    if len(b64) < 200:
        return None, None
    return b64, dict(bb)


async def _container_screenshot_pack(frame: Frame, handle: Locator) -> Optional[Tuple[str, dict]]:
    for sel in _CONTAINER_SELECTORS:
        loc = frame.locator(sel).first
        try:
            if await loc.is_visible(timeout=500):
                bb = await loc.bounding_box()
                if not bb:
                    continue
                png = await loc.screenshot(type="png")
                return base64.b64encode(png).decode("ascii"), bb
        except Exception:
            continue
    try:
        box = await handle.bounding_box()
        if not box:
            return None
        clip = {
            "x": max(0.0, box["x"] - 320),
            "y": max(0.0, box["y"] - 120),
            "width": min(520.0, 1920.0),
            "height": min(220.0, 1080.0),
        }
        png = await frame.page.screenshot(type="png", clip=clip)
        return base64.b64encode(png).decode("ascii"), dict(clip)
    except Exception:
        return None


async def _capture_puzzle_image_and_origin(frame: Frame, handle: Locator) -> Tuple[Optional[str], Optional[dict]]:
    b64, bb = await _best_canvas_b64_and_origin(frame)
    if b64 and bb:
        return b64, bb
    cp = await _container_screenshot_pack(frame, handle)
    if cp:
        return cp
    return None, None


async def _pick_handle_and_frame(page: Page) -> Tuple[Optional[Frame], Optional[Locator]]:
    for fr in _frames(page):
        h = await _first_visible_handle(fr)
        if h is not None:
            return fr, h
    return None, None


async def slider_captcha_visible(page: Page) -> bool:
    _, h = await _pick_handle_and_frame(page)
    return h is not None


async def _drag_slider(page: Page, handle: Locator, *, delta_x: float) -> None:
    box = await handle.bounding_box()
    if not box:
        raise RuntimeError("无法获取滑块把手位置")
    sx = box["x"] + box["width"] / 2
    sy = box["y"] + box["height"] / 2
    steps = random.randint(35, 70)
    await page.mouse.move(sx, sy)
    await page.mouse.down()
    for i in range(1, steps + 1):
        t = i / steps
        ease = t * t * (3 - 2 * t)
        nx = sx + delta_x * ease + random.uniform(-0.6, 0.6)
        ny = sy + random.uniform(-1.2, 1.2)
        await page.mouse.move(nx, ny)
    await page.mouse.up()


async def solve_slider_if_present(
    page: Page,
    client_key: str,
    *,
    max_retries: int = 2,
    comment: Optional[str] = None,
) -> bool:
    if not (client_key or "").strip():
        return not (await slider_captcha_visible(page))

    text = comment or (
        "Click the center of the puzzle GAP (hollow slot) where the piece should fit. "
        "缺口中心 / 拼图应嵌入位置的中心坐标。"
    )
    attempts = max_retries + 1

    for _attempt in range(attempts):
        fr, handle = await _pick_handle_and_frame(page)
        if handle is None:
            return True

        img, origin = await _capture_puzzle_image_and_origin(fr, handle)
        if not img or not origin:
            await asyncio.sleep(1.0)
            continue

        try:
            coords = await asyncio.to_thread(solve_coordinates_image, client_key.strip(), img, text)
        except Exception:
            await asyncio.sleep(1.5)
            continue

        if not coords:
            continue
        cx = float(coords[0].get("x", 0))

        hbox = await handle.bounding_box()
        if not hbox:
            continue
        piece_half = min(36.0, max(12.0, hbox["width"] * 0.45))

        gap_center_x = origin["x"] + cx
        start_x = hbox["x"] + hbox["width"] / 2
        delta_x = gap_center_x - start_x - piece_half

        try:
            await _drag_slider(page, handle, delta_x=delta_x)
        except Exception:
            await asyncio.sleep(1.0)
            continue

        await asyncio.sleep(2.0)
        if not (await slider_captcha_visible(page)):
            return True

    return False

