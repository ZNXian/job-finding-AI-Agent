# -*- coding: utf-8 -*-
"""人工反馈与记忆更新路由。"""
from fastapi import APIRouter

from config import dynamic_jobconfig
from services.memory_services import update_scene_memory
from services.scences import scene_manager
from utils.wrapper import handle_api_exception_async

router = APIRouter(tags=["feedback"])


@router.post("/feedback")
@handle_api_exception_async
async def feedback(scene_id: int):
    dynamic_jobconfig.set(scene_manager.get_dynamic_jobconfig(scene_id))
    update_scene_memory()
    return {
        "status": "success",
        "msg": f"场景{scene_id}记忆已更新",
    }
