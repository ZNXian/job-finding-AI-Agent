# -*- coding: utf-8 -*-
"""场景准备：本地路径与 multipart 上传。"""
import os
import tempfile
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Body, File, HTTPException, Request, UploadFile

from config import MAX_SCENE_UPLOAD_BYTES
from services.resume_document_ingest import (
    ALLOWED_SCENE_UPLOAD_SUFFIXES,
    ingest_user_document_to_text,
)
from services.llm_services import llm_extract_scene_fields
from services.scene_prepare import prepare_scene_from_txt_file
from services.scences import scene_manager
from utils.wrapper import handle_api_exception_async

router = APIRouter(tags=["scenes"])

def _ensure_localhost(request: Request) -> None:
    host = (getattr(request.client, "host", None) or "").strip().lower()
    if host not in {"127.0.0.1", "::1", "localhost"}:
        raise HTTPException(status_code=403, detail="仅允许本机访问（localhost/127.0.0.1）")


def _data_dir() -> Path:
    # 对齐 config.py 的 _CONFIG_DIR：项目根目录
    return Path(__file__).resolve().parents[1] / "data"


def _resume_dir() -> Path:
    return _data_dir() / "resume"


@router.post("/start_from_text")
@handle_api_exception_async
async def start_from_text(request: Request, text: str = Body(..., embed=True)):
    """浏览器直贴文本：写入 data/scene_temp.<timestamp>.txt → prepare_scene_from_txt_file。"""
    _ensure_localhost(request)
    t = (text or "").strip()
    if not t:
        return {
            "code": 400,
            "status": "error",
            "is_new_scene": False,
            "scene_id": None,
            "msg": "text 为空",
        }
    d = _data_dir()
    d.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    tmp_path = d / f"scene_temp.{ts}.txt"
    tmp_path.write_text(t, encoding="utf-8")
    out = prepare_scene_from_txt_file(str(tmp_path))
    if not out.get("ok"):
        return {
            "code": 400,
            "status": "error",
            "is_new_scene": False,
            "scene_id": None,
            "msg": out.get("error") or "场景准备失败",
            "temp_path": str(tmp_path),
        }
    return {
        "code": 200,
        "status": "success",
        "is_new_scene": bool(out.get("is_new_scene")),
        "scene_id": out["scene_id"],
        "msg": "场景匹配完成",
        "reason": out.get("reason") or "",
        "temp_path": str(tmp_path),
    }


@router.get("/scenes/list")
@handle_api_exception_async
async def scenes_list(request: Request):
    """返回当前已有的所有场景（结构化字段）。"""
    _ensure_localhost(request)
    scenes = scene_manager.get_all_scenes()
    # update_time 格式为 YYYY-MM-DD HH:MM:SS，字符串倒序可用
    scenes = sorted(scenes, key=lambda x: str(x.get("update_time") or ""), reverse=True)
    return {"code": 200, "status": "success", "scenes": scenes}


@router.get("/scenes/runtime_status")
@handle_api_exception_async
async def scenes_runtime_status(request: Request, scene_id: int):
    """查询某 scene 当前是否正在运行（agent/crawl/prefilter/submit）。"""
    _ensure_localhost(request)
    sid = int(scene_id or 0)
    if sid <= 0:
        return {"code": 400, "status": "error", "msg": "scene_id 不能为空"}
    from services.scene_runtime import snapshot as _snapshot

    stages = _snapshot(sid)
    # agent 任务摘要：从 api.agent 的 in-memory _TASKS 中筛选
    try:
        from api import agent as agent_mod

        tasks = []
        for tid, t in (getattr(agent_mod, "_TASKS", {}) or {}).items():
            try:
                if int((t or {}).get("scene_id") or 0) != sid:
                    continue
            except Exception:
                continue
            tasks.append(
                {
                    "task_id": tid,
                    "status": (t or {}).get("status"),
                    "created_at": (t or {}).get("created_at"),
                    "started_at": (t or {}).get("started_at"),
                    "finished_at": (t or {}).get("finished_at"),
                    "progress_message": (t or {}).get("progress_message"),
                }
            )
        tasks.sort(key=lambda x: float(x.get("created_at") or 0.0), reverse=True)
        tasks = tasks[:5]
    except Exception:
        tasks = []

    # agent 运行中判定：stage 表里 running 或存在 pending/running 的 task
    agent_running = bool((stages.get("agent") or {}).get("running"))
    if not agent_running:
        for t in tasks:
            if (t.get("status") or "").lower() in {"pending", "running"}:
                agent_running = True
                break
    stages_out = {
        "agent": {**(stages.get("agent") or {}), "running": agent_running},
        "crawl": stages.get("crawl") or {"running": False},
        "prefilter": stages.get("prefilter") or {"running": False},
        "submit": stages.get("submit") or {"running": False},
    }
    return {
        "code": 200,
        "status": "success",
        "scene_id": sid,
        "stages": stages_out,
        "agent_tasks": tasks,
    }


@router.post("/scenes/recognize_fields")
@handle_api_exception_async
async def scenes_recognize_fields(request: Request, text: str = Body(..., embed=True)):
    """LLM 自动识别：从文本抽取标准场景字段（不创建/复用场景）。"""
    _ensure_localhost(request)
    t = (text or "").strip()
    if not t:
        return {"code": 400, "status": "error", "msg": "text 为空"}
    try:
        data = llm_extract_scene_fields(t)
    except Exception as e:
        return {"code": 400, "status": "error", "msg": str(e)}
    return {"code": 200, "status": "success", "data": data}


@router.post("/start_from_txt")
@handle_api_exception_async
async def create_scene_from_txt(request: Request, file_path: str = Body(..., embed=True)):
    _ensure_localhost(request)
    out = prepare_scene_from_txt_file(file_path)
    if not out.get("ok"):
        return {
            "code": 400,
            "status": "error",
            "is_new_scene": False,
            "scene_id": None,
            "msg": out.get("error") or "场景准备失败",
        }
    return {
        "code": 200,
        "status": "success",
        "is_new_scene": bool(out.get("is_new_scene")),
        "scene_id": out["scene_id"],
        "msg": "场景匹配完成",
        "reason": out.get("reason") or "",
    }


@router.post("/start_from_upload")
@handle_api_exception_async
async def start_from_upload(
    request: Request,
    file: UploadFile = File(..., description="求职需求+简历，支持 txt/md/pdf/png/jpg/webp"),
):
    """浏览器上传：落盘临时路径 → prepare_scene_from_txt_file → 删除临时文件。"""
    _ensure_localhost(request)
    name = (file.filename or "").strip() or "upload.txt"
    suf = Path(name).suffix.lower()
    if suf not in ALLOWED_SCENE_UPLOAD_SUFFIXES:
        return {
            "code": 400,
            "status": "error",
            "is_new_scene": False,
            "scene_id": None,
            "msg": f"不支持的文件扩展名 {suf!r}，允许: {', '.join(sorted(ALLOWED_SCENE_UPLOAD_SUFFIXES))}",
        }
    raw = await file.read()
    if len(raw) > MAX_SCENE_UPLOAD_BYTES:
        return {
            "code": 400,
            "status": "error",
            "is_new_scene": False,
            "scene_id": None,
            "msg": f"文件过大（>{MAX_SCENE_UPLOAD_BYTES // (1024 * 1024)}MB）",
        }
    prep: dict | None = None
    fd, tmp_path = tempfile.mkstemp(suffix=suf, prefix="scene_upload_")
    try:
        with os.fdopen(fd, "wb") as fp:
            fp.write(raw)
        prep = prepare_scene_from_txt_file(tmp_path)
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

    if not prep or not prep.get("ok"):
        return {
            "code": 400,
            "status": "error",
            "is_new_scene": False,
            "scene_id": None,
            "msg": (prep or {}).get("error") or "场景准备失败",
        }
    return {
        "code": 200,
        "status": "success",
        "is_new_scene": bool(prep.get("is_new_scene")),
        "scene_id": prep["scene_id"],
        "msg": "场景匹配完成",
        "reason": prep.get("reason") or "",
    }


@router.post("/scenes/{scene_id}/resume/upload")
@handle_api_exception_async
async def upload_scene_resume(
    request: Request,
    scene_id: int,
    file: UploadFile = File(..., description="简历文件，支持 txt/md/pdf/png/jpg/webp"),
):
    """仅更新简历：解析上传文件 → 写入 data/resume/{scene_id}.txt（不触发场景匹配）。"""
    _ensure_localhost(request)
    sid = int(scene_id)
    if sid <= 0 or not scene_manager.get_scene_by_id(sid):
        return {"code": 400, "status": "error", "msg": f"scene_id={scene_id} 不存在"}
    name = (file.filename or "").strip() or "resume.txt"
    suf = Path(name).suffix.lower()
    if suf not in ALLOWED_SCENE_UPLOAD_SUFFIXES:
        return {
            "code": 400,
            "status": "error",
            "msg": f"不支持的文件扩展名 {suf!r}，允许: {', '.join(sorted(ALLOWED_SCENE_UPLOAD_SUFFIXES))}",
        }
    raw = await file.read()
    if len(raw) > MAX_SCENE_UPLOAD_BYTES:
        return {
            "code": 400,
            "status": "error",
            "msg": f"文件过大（>{MAX_SCENE_UPLOAD_BYTES // (1024 * 1024)}MB）",
        }

    fd, tmp_path = tempfile.mkstemp(suffix=suf, prefix="scene_resume_")
    try:
        with os.fdopen(fd, "wb") as fp:
            fp.write(raw)
        text = ingest_user_document_to_text(tmp_path)
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
    t = (text or "").strip()
    if not t:
        return {"code": 400, "status": "error", "msg": "解析简历为空，请检查文件内容/格式"}

    rd = _resume_dir()
    rd.mkdir(parents=True, exist_ok=True)
    out_path = rd / f"{sid}.txt"
    out_path.write_text(t, encoding="utf-8")
    return {
        "code": 200,
        "status": "success",
        "scene_id": sid,
        "msg": "简历已更新（未触发场景匹配）",
        "resume_path": str(out_path),
        "resume_chars": len(t),
    }
