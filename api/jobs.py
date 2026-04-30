from __future__ import annotations

import re
from typing import Annotated, Any

from fastapi import APIRouter, Body, HTTPException, Query, Request

from services.job_store import (
    get_crawl_list_jobs_for_ui,
    get_crawl_list_jobs_by_match_levels,
    update_crawl_list_manual_fields,
)
from utils.wrapper import handle_api_exception_async

router = APIRouter(tags=["jobs"])

_MANUAL_REASON_VALID_RE = re.compile(r"[A-Za-z\u4e00-\u9fff]")


def _ensure_localhost(request: Request) -> None:
    host = (getattr(request.client, "host", None) or "").strip().lower()
    if host not in {"127.0.0.1", "::1", "localhost"}:
        raise HTTPException(status_code=403, detail="仅允许本机访问（localhost/127.0.0.1）")


@router.get("/jobs/matched")
@handle_api_exception_async
async def jobs_matched(
    request: Request,
    scene_id: Annotated[int, Query(description="场景 id")],
    match_levels: Annotated[str | None, Query(description="逗号分隔，默认 高,中")] = None,
    page: Annotated[int, Query(ge=1, description="页码，从 1 开始")] = 1,
    page_size: Annotated[int, Query(ge=1, le=200, description="每页条数（建议 25）")] = 25,
    sort_by: Annotated[str, Query(description="排序字段：fetch_timestamp/apply/match_level")] = "fetch_timestamp",
    sort_dir: Annotated[str, Query(description="排序方向：asc/desc")] = "desc",
    # 兼容旧参数（若显式传入 limit/offset，则优先使用）
    limit: Annotated[int | None, Query(ge=1, le=200, description="兼容旧参数：最大返回条数")] = None,
    offset: Annotated[int | None, Query(ge=0, description="兼容旧参数：偏移")] = None,
):
    _ensure_localhost(request)
    levels = [x.strip() for x in (match_levels or "高,中").split(",") if x.strip()]
    if limit is not None or offset is not None:
        rows = get_crawl_list_jobs_by_match_levels(
            platform="liepin",
            scene_id=int(scene_id),
            match_levels=levels,
            limit=int(limit or 50),
            offset=int(offset or 0),
        )
        total = len(rows)
    else:
        ps = int(page_size or 25)
        off = (int(page or 1) - 1) * ps
        out = get_crawl_list_jobs_for_ui(
            platform="liepin",
            scene_id=int(scene_id),
            match_levels=levels,
            limit=ps,
            offset=off,
            sort_by=sort_by,  # type: ignore[arg-type]
            sort_dir=sort_dir,  # type: ignore[arg-type]
            hide_manual_rejected=True,
        )
        total = int(out.get("total") or 0)
        rows = out.get("items") or []

    jobs: list[dict[str, Any]] = []
    for r in rows:
        jobs.append(
            {
                "platform": r.get("platform", ""),
                "platform_job_id": r.get("platform_job_id", ""),
                "title": r.get("title", ""),
                "company": r.get("company", ""),
                "location": r.get("location", ""),
                "salary": r.get("salary", ""),
                "url": r.get("url", ""),
                "match_level": r.get("match_level", ""),
                "reason": r.get("reason", ""),
                "apply": r.get("apply", ""),
                "hr_greeting": r.get("hr_greeting", ""),
                "fetch_timestamp": r.get("fetch_timestamp", ""),
            }
        )
    return {
        "code": 200,
        "status": "success",
        "scene_id": int(scene_id),
        "jobs": jobs,
        "total": int(total),
        "page": int(page),
        "page_size": int(page_size),
    }


@router.post("/jobs/manual_reject")
@handle_api_exception_async
async def jobs_manual_reject(
    request: Request,
    payload: dict[str, Any] = Body(...),
):
    _ensure_localhost(request)
    scene_id = int(payload.get("scene_id") or 0)
    platform = str(payload.get("platform") or "liepin").strip() or "liepin"
    platform_job_id = str(payload.get("platform_job_id") or "").strip()
    reason = str(payload.get("reason") or "").strip()
    if scene_id <= 0 or not platform_job_id:
        return {"code": 400, "status": "error", "msg": "scene_id/platform_job_id 不能为空"}
    if not reason or not _MANUAL_REASON_VALID_RE.search(reason):
        return {
            "code": 400,
            "status": "error",
            "msg": "reason 无效：需至少包含 1 个中文或英文字母（纯空格/数字/标点不提交）",
        }
    update_crawl_list_manual_fields(
        platform,
        scene_id,
        platform_job_id,
        manual_apply="否",
        manual_reason=reason,
    )
    return {"code": 200, "status": "success", "msg": "已记录人工不投递原因"}

