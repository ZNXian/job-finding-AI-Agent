# -*- coding:utf-8 -*-
# @CreatTime : 2026 19:06
# @Author : XZN

from typing import Annotated

from fastapi import FastAPI, Body, File, Query, UploadFile
import uvicorn
# import asyncio

from crawlers.liepin import crawl_liepin, login_liepin
from services.llm_services import llm_identify_scene
from services.llm_services import (
    LLM_JOB_FILTER_BATCH_MAX,
    llm_process_job,
    llm_process_jobs_batch,
)
from services.job_store import get_unprocessed_crawl_list_jobs_for_llm
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
@handle_api_exception_async
async def liepin_login():
    await login_liepin()
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
@handle_api_exception_async
@app.post("/api/crawl_liepin")
async def run_crawl_and_ai(
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
    # 旧接口：为兼容保留，但内部统一走「crawl-only + submit」流程
    dynamic_jobconfig.set(scene_manager.get_dynamic_jobconfig(scene_id))
    jobs = await crawl_liepin(scene_id=scene_id, reset_checkpoint=reset_checkpoint)
    if not crawl_only:
        # 从 SQLite 读取未处理岗位行（match_level=''），避免依赖 crawl 返回值与缓存
        submitted_jobs = get_unprocessed_crawl_list_jobs_for_llm(
            "liepin",
            scene_id,
            match_level_empty_only=True,
        )
        for i in range(0, len(submitted_jobs), LLM_JOB_FILTER_BATCH_MAX):
            batch = submitted_jobs[i : i + LLM_JOB_FILTER_BATCH_MAX]
            outs = llm_process_jobs_batch(batch, scene_id=scene_id)
            for j, job in enumerate(batch):
                out = outs[j] if isinstance(outs, list) and j < len(outs) else {}
                write_to_csv(job, out, scene_id=scene_id)

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
# 接口 5：爬取只写入 SQLite
# ==========================
@handle_api_exception_async
@app.post("/api/crawl_liepin_crawl_only")
async def crawl_liepin_crawl_only(
    scene_id: int,
    reset_checkpoint: Annotated[
        bool,
        Query(
            description="为 True 时清除当前 scene_id 在 checkpoint.json 中的断点，从第 1 页（索引 0）重新爬",
        ),
    ] = False,
):
    dynamic_jobconfig.set(scene_manager.get_dynamic_jobconfig(scene_id))
    jobs = await crawl_liepin(scene_id=scene_id, reset_checkpoint=reset_checkpoint)
    return {
        "code": 200,
        "status": "success",
        "scene_id": scene_id,
        "job_count": len(jobs),
        "csv_file": None,
    }


# ==========================
# 接口 6：从 SQLite 提交 LLM
# ==========================
@handle_api_exception_async
@app.post("/api/submit_llm_for_scene")
async def submit_llm_for_scene(
    scene_id: int,
):
    dynamic_jobconfig.set(scene_manager.get_dynamic_jobconfig(scene_id))
    submitted_jobs = get_unprocessed_crawl_list_jobs_for_llm(
        "liepin",
        scene_id,
        match_level_empty_only=True,
    )
    if not submitted_jobs:
        return {
            "code": 200,
            "status": "success",
            "scene_id": scene_id,
            "job_count": 0,
            "csv_file": cfg.CSV_FILE,
        }

    for i in range(0, len(submitted_jobs), LLM_JOB_FILTER_BATCH_MAX):
        batch = submitted_jobs[i : i + LLM_JOB_FILTER_BATCH_MAX]
        outs = llm_process_jobs_batch(batch, scene_id=scene_id)
        for j, job in enumerate(batch):
            out = outs[j] if isinstance(outs, list) and j < len(outs) else {}
            write_to_csv(job, out, scene_id=scene_id)

    return {
        "code": 200,
        "status": "success",
        "scene_id": scene_id,
        "job_count": len(submitted_jobs),
        "csv_file": cfg.CSV_FILE,
    }

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