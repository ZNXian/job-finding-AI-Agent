# -*- coding: utf-8 -*-
"""LangGraph 一键流水线路由。"""
import asyncio
from typing import Annotated

from fastapi import APIRouter, Query

from agent_orchestrator import run_pipeline
from utils.wrapper import handle_api_exception_async

router = APIRouter(tags=["agent"])


@router.post("/agent/run")
@handle_api_exception_async
async def agent_run(
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
    uf = (user_file_path or "").strip()
    if not uf and scene_id is None:
        return {
            "code": 400,
            "status": "error",
            "msg": "需要 scene_id 或 user_file_path 之一",
        }

    try:
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

    return {
        "code": 200,
        "status": "success",
        "pipeline": pipeline,
    }
