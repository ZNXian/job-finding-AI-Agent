# -*- coding:utf-8 -*-
# @CreatTime : 2026 19:06
# @Author : XZN

# API 目录（均为 POST）：
# - /api/liepin_login：打开猎聘并等待人工登录（保存 storage_state），用于后续自动登录复用
# - /api/start_from_txt：从本地 txt 读取“需求+简历”，用 LLM 判断匹配已有场景或新建场景
# - /api/crawl_liepin：兼容老接口；爬虫 +（可选）LLM + 写 CSV
#   - query：scene_id，crawl_only=true 时只爬不提交 LLM；reset_checkpoint=true 清断点重爬
# - /api/crawl_liepin_crawl_only：只爬取并写入 SQLite（list_jobs），不提交 LLM、不写 CSV
#   - query：scene_id，reset_checkpoint
# - /api/prefilter_titles_for_scene：标题初筛（只发关键词+标题，批量大）；不合适直接写回 SQLite=低/否/标题预判；其余写 pending
#   - query：scene_id，include_company/include_location/include_salary（默认 false）
# - /api/submit_llm_for_scene：二阶段详情精筛（只处理 match_level='pending'），写 CSV 并回写 SQLite（match_level/reason/apply/hr_greeting）
#   - query：scene_id
# - /api/feedback：人工反馈后更新记忆（memory）
#   - query：scene_id

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
    llm_title_prefilter_jobs_batch,
)
from services.job_store import (
    get_crawl_list_jobs_for_title_prefilter,
    get_pending_crawl_list_jobs_for_llm,
    get_unprocessed_crawl_list_jobs_for_llm,
    update_crawl_list_llm_fields,
)
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
    submitted_jobs = get_pending_crawl_list_jobs_for_llm("liepin", scene_id)
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
# 接口 7：标题初步筛选（写回 SQLite）
# ==========================
@handle_api_exception_async
@app.post("/api/prefilter_titles_for_scene")
async def prefilter_titles_for_scene(
    scene_id: int,
    include_company: Annotated[bool, Query(description="是否在初筛中带上公司字段")] = False,
    include_location: Annotated[bool, Query(description="是否在初筛中带上地点字段")] = False,
    include_salary: Annotated[bool, Query(description="是否在初筛中带上薪资字段")] = False,
):
    dynamic_jobconfig.set(scene_manager.get_dynamic_jobconfig(scene_id))
    jobs = get_crawl_list_jobs_for_title_prefilter("liepin", scene_id, include_parse_failed=True)
    if not jobs:
        return {"code": 200, "status": "success", "scene_id": scene_id, "job_count": 0}

    rejected = 0
    pending = 0
    for i in range(0, len(jobs), 200):
        batch = jobs[i : i + 200]
        outs = llm_title_prefilter_jobs_batch(
            batch,
            scene_id=scene_id,
            include_company=include_company,
            include_location=include_location,
            include_salary=include_salary,
        )
        for j, job in enumerate(batch):
            out = outs[j] if isinstance(outs, list) and j < len(outs) else {}
            verdict = str(out.get("verdict") or "keep").strip().lower()
            r = str(out.get("reason") or "").strip()
            pid = str(job.get("platform_job_id") or "")
            if not pid:
                continue
            if verdict == "reject":
                # reason 总长 <= 20（含“标题预判：”前缀）
                reason = ("标题预判：" + r)[:20]
                update_crawl_list_llm_fields(
                    "liepin",
                    int(scene_id),
                    pid,
                    match_level="低",
                    apply="否",
                    reason=reason,
                    hr_greeting="",
                )
                rejected += 1
            else:
                update_crawl_list_llm_fields(
                    "liepin",
                    int(scene_id),
                    pid,
                    match_level="pending",
                    apply="",
                    reason="",
                    hr_greeting="",
                )
                pending += 1
    return {
        "code": 200,
        "status": "success",
        "scene_id": scene_id,
        "job_count": len(jobs),
        "rejected": rejected,
        "pending": pending,
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