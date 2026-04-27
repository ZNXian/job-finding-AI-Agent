# -*- coding:utf-8 -*-
# @CreatTime : 2026 19:06
# @Author : XZN

from typing import Annotated

from fastapi import FastAPI, Body, File, Query, UploadFile
import uvicorn
# import asyncio

from crawlers.liepin import crawl_liepin, login_liepin
from services.llm_services import llm_process_job,llm_identify_scene
from services.scences import scene_manager
from services.memory_services import update_scene_memory
# import config
from utils.files import *
from utils.wrapper import *
from config import dynamic_jobconfig
import config as cfg
from config import HOST,PORT,DEBUG

app = FastAPI(title="job finding AI Agent", version="1.0")

# ==========================
# 接口 1：猎聘登录接口
# ==========================
@app.post("/api/liepin_login")
@handle_api_exception
def liepin_login():
    login_liepin()
    return {
        "code": 200,
        "status": "success",
        "msg": "登录成功，已关闭浏览器"
    }
# ==========================
# 接口 2：自然语言匹配岗位场景
# ==========================
@app.post("/api/start_from_txt")
@handle_api_exception_async
async def create_scene_from_txt( file_path: str = Body(..., embed=True)):
    user_text = read_and_clean_txt(file_path)
    # 2. 从 SceneManager 获取所有场景（内存读取，不读文件）
    scenes = scene_manager.get_all_scenes()
    # 3. 调用你已写好的 LLM 函数，判断是否新场景
    is_new, scene_result = llm_identify_scene(user_text, scenes)
    # 4. 调用你已写好的类方法，存储/更新场景
    scene_id = scene_manager.update_scene_from_ai(is_new, scene_result)
    return {
        "code": 200,
        "is_new_scene": is_new,
        "scene_id": scene_id,
        "msg": "场景匹配完成"
    }

# ==========================
# 接口 3：爬取 + AI 判断 → 输出CSV
# ==========================
@handle_api_exception
@app.post("/api/crawl_liepin")
def run_crawl_and_ai(
    scene_id: int,
    crawl_only: Annotated[
        bool,
        Query(
            description="为 True 时只执行 crawl_liepin（列表+详情循环），不调 LLM、不写 CSV",
        ),
    ] = False,
    reset_checkpoint: Annotated[
        bool,
        Query(
            description="为 True 时清除当前 scene_id 在 checkpoint.json 中的断点，从第 1 页（索引 0）重新爬",
        ),
    ] = False,
):
    # 加载当前场景的动态配置
    dynamic_jobconfig.set(scene_manager.get_dynamic_jobconfig(scene_id))
    # 1. 爬取岗位（内部按列表页 → 本页详情 → 下一列表页循环；支持断点续爬）
    jobs = crawl_liepin(scene_id=scene_id, reset_checkpoint=reset_checkpoint)
    # 2. 调用 LLM + VLM 判断 并写入csv（crawl_only 时跳过）
    if not crawl_only:
        for job in jobs:
            write_to_csv(job, llm_process_job(job))
    out = {
        "code": 200,
        "status": "success",
        "scene_id": scene_id,
        "crawl_only": crawl_only,
        "job_count": len(jobs),
    }
    if crawl_only:
        out["jobs_preview"] = [
            {"标题": j.get("标题"), "公司": j.get("公司"), "链接": j.get("链接")}
            for j in jobs[:10]
        ]
    else:
        out["csv_file"] = cfg.CSV_FILE
    return out

# ==========================
# 接口 4：人工反馈 → 更新记忆
# # ==========================
@app.post("/api/feedback")
@handle_api_exception_async
async def feedback(scene_id: int):
    dynamic_jobconfig.set(scene_manager.get_dynamic_jobconfig(scene_id))
    update_scene_memory()
    return {
        "status": "success",
        "msg": f"场景{scene_id}记忆已更新"
    }

if __name__ == "__main__":
    # uvicorn.run("main:app", host=HOST, port=PORT, reload=DEBUG)
    uvicorn.run("main:app", host=HOST, port=PORT)