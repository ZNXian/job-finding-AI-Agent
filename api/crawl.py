# -*- coding: utf-8 -*-
"""猎聘爬取、初筛、精筛相关路由。"""
from typing import Annotated

from fastapi import APIRouter, Query

import config as cfg
from config import dynamic_jobconfig
from crawlers.liepin import crawl_liepin
from services.job_store import (
    get_crawl_list_jobs_for_title_prefilter,
    get_pending_crawl_list_jobs_for_llm,
    get_unprocessed_crawl_list_jobs_for_llm,
    update_crawl_list_llm_fields,
)
from services.llm_services import (
    LLM_JOB_FILTER_BATCH_MAX,
    llm_process_jobs_batch,
    llm_title_prefilter_jobs_batch,
)
from services.scences import scene_manager
from utils.files import write_to_csv
from utils.wrapper import handle_api_exception_async

router = APIRouter(tags=["crawl"])


@router.post("/crawl_liepin")
@handle_api_exception_async
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
    dynamic_jobconfig.set(scene_manager.get_dynamic_jobconfig(scene_id))
    jobs = await crawl_liepin(scene_id=scene_id, reset_checkpoint=reset_checkpoint)
    if not crawl_only:
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


@router.post("/crawl_liepin_crawl_only")
@handle_api_exception_async
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


@router.post("/submit_llm_for_scene")
@handle_api_exception_async
async def submit_llm_for_scene(scene_id: int):
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


@router.post("/prefilter_titles_for_scene")
@handle_api_exception_async
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
