# -*- coding: utf-8 -*-
# AI 生成
# 生成目的：调用 2Captcha v2 API（createTask / getTaskResult）：CoordinatesTask、TencentTaskProxyless 等

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

CREATE_URL = "https://api.2captcha.com/createTask"
RESULT_URL = "https://api.2captcha.com/getTaskResult"


def _post_json(url: str, payload: Dict[str, Any], timeout: int = 120) -> Dict[str, Any]:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def wait_task_solution_dict(
    client_key: str,
    task_id: int,
    *,
    poll_interval: float = 3.0,
    max_wait: float = 180.0,
) -> Dict[str, Any]:
    # AI 生成
    # 生成目的：通用轮询 getTaskResult，返回 solution 字典（Tencent / 其它任务）
    deadline = time.monotonic() + max_wait
    payload = {"clientKey": client_key, "taskId": task_id}
    while time.monotonic() < deadline:
        try:
            r = _post_json(RESULT_URL, payload, timeout=60)
        except urllib.error.HTTPError:
            time.sleep(poll_interval)
            continue
        status = (r.get("status") or "").lower()
        err = int(r.get("errorId", 0))
        if status == "ready":
            if err != 0:
                raise RuntimeError(
                    r.get("errorDescription") or r.get("errorCode") or str(r)
                )
            sol = r.get("solution")
            if sol is None:
                raise RuntimeError(f"2Captcha 无 solution: {r}")
            return sol if isinstance(sol, dict) else {}
        if status == "processing":
            time.sleep(poll_interval)
            continue
        if err in (11, 12) or "NOT_READY" in (r.get("errorDescription") or "").upper():
            time.sleep(poll_interval)
            continue
        if err != 0:
            raise RuntimeError(
                r.get("errorDescription") or r.get("errorCode") or str(r)
            )
        time.sleep(poll_interval)
    raise TimeoutError("2Captcha 等待结果超时")


def create_tencent_task_proxyless(
    client_key: str,
    website_url: str,
    app_id: str,
    *,
    captcha_script: Optional[str] = None,
) -> int:
    # AI 生成
    # 生成目的：TencentTaskProxyless，见 https://2captcha.com/api-docs/tencent
    task: Dict[str, Any] = {
        "type": "TencentTaskProxyless",
        "websiteURL": website_url,
        "appId": str(app_id),
    }
    if captcha_script:
        task["captchaScript"] = captcha_script
    payload: Dict[str, Any] = {"clientKey": client_key, "task": task}
    r = _post_json(CREATE_URL, payload, timeout=60)
    if r.get("errorId"):
        raise RuntimeError(
            r.get("errorDescription") or r.get("errorCode") or str(r)
        )
    tid = r.get("taskId")
    if not tid:
        raise RuntimeError(f"2Captcha 无 taskId: {r}")
    return int(tid)


def create_coordinates_task(
    client_key: str,
    image_base64: str,
    comment: str,
    *,
    min_clicks: int = 1,
    max_clicks: int = 1,
) -> int:
    # AI 生成
    # 生成目的：提交 CoordinatesTask，返回 taskId
    body = image_base64.strip()
    if body.startswith("data:"):
        comma = body.find(",")
        if comma != -1:
            body = body[comma + 1 :]

    payload: Dict[str, Any] = {
        "clientKey": client_key,
        "task": {
            "type": "CoordinatesTask",
            "body": body,
            "comment": comment,
            "minClicks": min_clicks,
            "maxClicks": max_clicks,
        },
    }
    r = _post_json(CREATE_URL, payload, timeout=60)
    if r.get("errorId"):
        raise RuntimeError(
            r.get("errorDescription") or r.get("errorCode") or str(r)
        )
    tid = r.get("taskId")
    if not tid:
        raise RuntimeError(f"2Captcha 无 taskId: {r}")
    return int(tid)


def wait_coordinates_solution(
    client_key: str,
    task_id: int,
    *,
    poll_interval: float = 3.0,
    max_wait: float = 120.0,
) -> List[Dict[str, Any]]:
    # AI 生成
    # 生成目的：轮询直到 ready，返回 solution.coordinates
    sol = wait_task_solution_dict(
        client_key,
        task_id,
        poll_interval=poll_interval,
        max_wait=max_wait,
    )
    coords = sol.get("coordinates") or []
    if not coords:
        raise RuntimeError(f"2Captcha 无 coordinates: {sol!r}")
    return coords


def solve_coordinates_image(
    client_key: str,
    image_base64: str,
    comment: str,
) -> List[Dict[str, Any]]:
    # AI 生成
    # 生成目的：一步：创建任务并等到坐标列表 [{"x":..,"y":..}, ...]
    tid = create_coordinates_task(client_key, image_base64, comment)
    return wait_coordinates_solution(client_key, tid)
