# -*- coding: utf-8 -*-
"""猎聘登录相关路由。"""
from fastapi import APIRouter

from crawlers.liepin import login_liepin
from utils.wrapper import handle_api_exception_async

router = APIRouter(tags=["liepin"])


@router.post("/liepin_login")
@handle_api_exception_async
async def liepin_login():
    await login_liepin()
    return {
        "code": 200,
        "status": "success",
        "msg": "登录成功，已关闭浏览器",
    }
