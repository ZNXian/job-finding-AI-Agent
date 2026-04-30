# -*- coding: utf-8 -*-
"""场景准备：本地路径与 multipart 上传。"""
import os
import tempfile
from pathlib import Path

from fastapi import APIRouter, Body, File, UploadFile

from config import MAX_SCENE_UPLOAD_BYTES
from services.resume_document_ingest import ALLOWED_SCENE_UPLOAD_SUFFIXES
from services.scene_prepare import prepare_scene_from_txt_file
from utils.wrapper import handle_api_exception_async

router = APIRouter(tags=["scenes"])


@router.post("/start_from_txt")
@handle_api_exception_async
async def create_scene_from_txt(file_path: str = Body(..., embed=True)):
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
    file: UploadFile = File(..., description="求职需求+简历，支持 txt/md/pdf/png/jpg/webp"),
):
    """浏览器上传：落盘临时路径 → prepare_scene_from_txt_file → 删除临时文件。"""
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
