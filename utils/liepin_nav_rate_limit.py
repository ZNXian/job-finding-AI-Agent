# -*- coding: utf-8 -*-
"""猎聘 page.goto 滑动窗口限速（单进程内串行爬取场景）。

任意连续 ``LIEPIN_NAV_WINDOW_SEC`` 秒内，对猎聘发起的 ``page.goto`` 不超过
``LIEPIN_MAX_NAV_PER_HOUR`` 次；触顶时在 ``await_liepin_navigation_slot`` 内
``asyncio.sleep`` 等待至窗口内腾出名额再继续。

将 ``LIEPIN_MAX_NAV_PER_HOUR`` 设为 0 或负数可关闭限速。
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from typing import Deque

import config as cfg
from config import log

_lock = asyncio.Lock()
_timestamps: Deque[float] = deque()


async def await_liepin_navigation_slot() -> None:
    """在每次对猎聘执行 ``page.goto`` 之前调用（列表 / 详情 / 登录恢复列表）。"""
    max_n = int(getattr(cfg, "LIEPIN_MAX_NAV_PER_HOUR", 100) or 0)
    window = float(getattr(cfg, "LIEPIN_NAV_WINDOW_SEC", 3600) or 3600)
    if max_n <= 0 or window <= 0:
        return

    while True:
        async with _lock:
            now = time.monotonic()
            while _timestamps and _timestamps[0] <= now - window:
                _timestamps.popleft()
            if len(_timestamps) < max_n:
                _timestamps.append(now)
                return
            oldest = float(_timestamps[0])
            wait = oldest + window - now + 0.05
            n_in_window = len(_timestamps)

        sleep_sec = max(wait, 0.05)
        log.info(
            "猎聘导航限速触顶等待：%.0f 秒滑动窗口内已 %s 次 page.goto（上限 %s），"
            "将 sleep %.1f 秒后再继续",
            window,
            n_in_window,
            max_n,
            sleep_sec,
        )
        await asyncio.sleep(sleep_sec)
