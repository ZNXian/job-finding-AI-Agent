from __future__ import annotations

import threading
import time
from typing import Any, Literal

Stage = Literal["agent", "crawl", "prefilter", "submit"]

_LOCK = threading.RLock()
# {scene_id: {stage: {"running": bool, "started_at": float, "meta": dict}}}
_RUNNING: dict[int, dict[str, dict[str, Any]]] = {}


def _now() -> float:
    return time.time()


def mark_start(scene_id: int, stage: Stage, *, meta: dict[str, Any] | None = None) -> bool:
    """标记某 stage 开始运行。若该 stage 已 running 则返回 False（同 stage 防重入）。"""
    sid = int(scene_id)
    if sid <= 0:
        return False
    with _LOCK:
        m = _RUNNING.setdefault(sid, {})
        cur = m.get(stage) or {}
        if cur.get("running"):
            return False
        m[stage] = {"running": True, "started_at": _now(), "meta": dict(meta or {})}
        return True


def mark_end(scene_id: int, stage: Stage) -> None:
    sid = int(scene_id)
    if sid <= 0:
        return
    with _LOCK:
        m = _RUNNING.get(sid)
        if not m or stage not in m:
            return
        cur = m.get(stage) or {}
        cur["running"] = False
        cur["finished_at"] = _now()
        m[stage] = cur


def is_running(scene_id: int, stage: Stage) -> bool:
    sid = int(scene_id)
    if sid <= 0:
        return False
    with _LOCK:
        cur = (_RUNNING.get(sid) or {}).get(stage) or {}
        return bool(cur.get("running"))


def snapshot(scene_id: int) -> dict[str, Any]:
    sid = int(scene_id)
    with _LOCK:
        m = _RUNNING.get(sid) or {}
        # shallow copy for safety
        out: dict[str, Any] = {}
        for k, v in m.items():
            out[k] = dict(v)
        return out

