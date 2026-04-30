# -*- coding: utf-8 -*-
"""LangGraph 一键流水线路由。"""
import asyncio
import threading
import time
import uuid
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request

from agent_orchestrator import run_pipeline
from config import log
from services.scene_runtime import mark_end, mark_start
from utils.wrapper import handle_api_exception_async

router = APIRouter(tags=["agent"])

_TASKS_LOCK = threading.RLock()
_TASKS: dict[str, dict] = {}


def _ensure_localhost(request: Request) -> None:
    host = (getattr(request.client, "host", None) or "").strip().lower()
    if host not in {"127.0.0.1", "::1", "localhost"}:
        raise HTTPException(status_code=403, detail="仅允许本机访问（localhost/127.0.0.1）")


def _task_set(task_id: str, patch: dict) -> None:
    with _TASKS_LOCK:
        cur = _TASKS.get(task_id) or {}
        cur.update(patch)
        _TASKS[task_id] = cur


def _task_get(task_id: str) -> dict | None:
    with _TASKS_LOCK:
        return _TASKS.get(task_id)


def _run_pipeline_task(task_id: str, kwargs: dict) -> None:
    _task_set(task_id, {"status": "running", "started_at": time.time()})
    try:
        out = run_pipeline(**kwargs)
        msg = str(out.get("message") or "").strip()
        _task_set(
            task_id,
            {
                "status": "success",
                "finished_at": time.time(),
                "progress_message": msg,
                "result": out,
            },
        )
    except Exception as e:
        log.warning("agent run_async task failed: %s", e)
        _task_set(
            task_id,
            {
                "status": "error",
                "finished_at": time.time(),
                "progress_message": str(e),
                "error": str(e),
            },
        )
    finally:
        try:
            sid = int(kwargs.get("scene_id") or 0)
        except Exception:
            sid = 0
        if sid > 0:
            mark_end(sid, "agent")


@router.post("/agent/run")
@handle_api_exception_async
async def agent_run(
    request: Request,
    scene_id: Annotated[
        int | None,
        Query(description="已有场景 id；与 user_file_path 二选一"),
    ] = None,
    user_file_path: Annotated[
        str | None,
        Query(description="本地 txt 路径；传入时先执行场景准备再跑后续步骤"),
    ] = None,
    reset_checkpoint: Annotated[
        bool,
        Query(description="为 True 时清除当前 scene_id 爬取断点，从第 1 页重爬"),
    ] = False,
    include_company: Annotated[bool, Query(description="初筛是否带上公司字段")] = False,
    include_location: Annotated[bool, Query(description="初筛是否带上地点字段")] = False,
    include_salary: Annotated[bool, Query(description="初筛是否带上薪资字段")] = False,
):
    """在线程池中执行 run_pipeline，避免同进程 requests 阻塞事件循环导致自调用死锁。"""
    _ensure_localhost(request)
    uf = (user_file_path or "").strip()
    if not uf and scene_id is None:
        return {
            "code": 400,
            "status": "error",
            "msg": "需要 scene_id 或 user_file_path 之一",
        }

    try:
        sid = int(scene_id or 0) if not uf else 0
        if sid > 0:
            if not mark_start(sid, "agent", meta={"mode": "sync"}):
                return {"code": 409, "status": "error", "msg": f"scene_id={sid} 的 agent 正在运行中"}
        pipeline = await asyncio.to_thread(
            lambda: run_pipeline(
                scene_id=scene_id,
                user_file_path=uf or None,
                reset_checkpoint=reset_checkpoint,
                include_company=include_company,
                include_location=include_location,
                include_salary=include_salary,
            )
        )
    except ValueError as e:
        return {"code": 400, "status": "error", "msg": str(e)}
    finally:
        if not uf:
            try:
                sid = int(scene_id or 0)
            except Exception:
                sid = 0
            if sid > 0:
                mark_end(sid, "agent")

    return {
        "code": 200,
        "status": "success",
        "pipeline": pipeline,
    }


@router.post("/agent/run_async")
@handle_api_exception_async
async def agent_run_async(
    request: Request,
    scene_id: Annotated[
        int | None,
        Query(description="已有场景 id；与 user_file_path 二选一"),
    ] = None,
    user_file_path: Annotated[
        str | None,
        Query(description="本地 txt 路径；传入时先执行场景准备再跑后续步骤"),
    ] = None,
    reset_checkpoint: Annotated[
        bool,
        Query(description="为 True 时清除当前 scene_id 爬取断点，从第 1 页重爬"),
    ] = False,
    include_company: Annotated[bool, Query(description="初筛是否带上公司字段")] = False,
    include_location: Annotated[bool, Query(description="初筛是否带上地点字段")] = False,
    include_salary: Annotated[bool, Query(description="初筛是否带上薪资字段")] = False,
):
    _ensure_localhost(request)
    uf = (user_file_path or "").strip()
    if not uf and scene_id is None:
        return {"code": 400, "status": "error", "msg": "需要 scene_id 或 user_file_path 之一"}
    try:
        sid = int(scene_id or 0) if not uf else 0
    except Exception:
        sid = 0
    if sid > 0:
        if not mark_start(sid, "agent", meta={"mode": "async"}):
            return {"code": 409, "status": "error", "msg": f"scene_id={sid} 的 agent 正在运行中"}
    task_id = str(uuid.uuid4())
    _task_set(
        task_id,
        {
            "status": "pending",
            "created_at": time.time(),
            "progress_message": "已创建任务",
            "scene_id": scene_id,
            "user_file_path": uf,
        },
    )
    kwargs = {
        "scene_id": scene_id,
        "user_file_path": uf or None,
        "reset_checkpoint": reset_checkpoint,
        "include_company": include_company,
        "include_location": include_location,
        "include_salary": include_salary,
    }
    # 后台线程运行，避免阻塞请求（不要 await）
    th = threading.Thread(
        target=_run_pipeline_task,
        args=(task_id, kwargs),
        name=f"agent_run_async_{task_id[:8]}",
        daemon=True,
    )
    th.start()
    return {"code": 200, "status": "success", "task_id": task_id}


@router.get("/agent/task/{task_id}")
@handle_api_exception_async
async def agent_task(request: Request, task_id: str):
    _ensure_localhost(request)
    t = (task_id or "").strip()
    if not t:
        return {"code": 400, "status": "error", "msg": "task_id 为空"}
    data = _task_get(t)
    if not data:
        return {"code": 404, "status": "error", "msg": "task_id 不存在"}
    # 不把内部异常堆栈暴露给前端，只回 message
    return {"code": 200, "status": "success", "task_id": t, **data}
