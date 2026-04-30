# -*- coding: utf-8 -*-
"""HTTP API 路由（按业务拆分）。挂载后路径仍为 /api/...，与拆分前一致。

完整目录见各子模块 docstring 或 main 模块顶部说明。
"""
from fastapi import FastAPI

from api.agent import router as agent_router
from api.crawl import router as crawl_router
from api.feedback import router as feedback_router
from api.liepin import router as liepin_router
from api.scenes import router as scenes_router


def register_routes(app: FastAPI) -> None:
    """将所有 APIRouter 挂到 app，统一前缀 /api。"""
    app.include_router(liepin_router, prefix="/api")
    app.include_router(scenes_router, prefix="/api")
    app.include_router(crawl_router, prefix="/api")
    app.include_router(agent_router, prefix="/api")
    app.include_router(feedback_router, prefix="/api")
