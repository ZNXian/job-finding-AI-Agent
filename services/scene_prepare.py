# -*- coding: utf-8 -*-
"""从本地文件准备求职场景：解析为纯文本 → LLM 结构化决策 → 复用或新建场景。

支持扩展名见 services.resume_document_ingest.ingest_user_document_to_text（txt/md/图/pdf 等）。

约定：
- 供 /api/start_from_txt、/api/start_from_upload 与 agent_orchestrator.prepare_scene_node 共用；调用方勿再通过 HTTP 包一层本逻辑。
- LLM 须返回合法 JSON（见 llm_prepare_scene_decision）；若模型偶发缺键/多键，以 llm_services 内校验为准，
  排障时可调 prompt 或放宽校验，聊天里的提示不会自动进模型。
- reuse 且仅给 scene_name 时，resolve_scene_name_to_id 须在已有场景中唯一命中，否则报错。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from config import log
from services.llm_services import NEW_SCENE_BODY_KEYS, llm_prepare_scene_decision
from services.scences import scene_manager
from services.resume_document_ingest import ingest_user_document_to_text


def _city_blob(scene: Dict[str, Any]) -> str:
    c = scene.get("city")
    if isinstance(c, list):
        return " ".join(str(x) for x in c).lower()
    return str(c or "").lower()


def resolve_scene_name_to_id(scenes: List[Dict[str, Any]], name: str) -> int:
    """将 scene_name 解析为唯一 scene_id；无法唯一匹配则抛 ValueError。"""
    n = (name or "").strip().lower()
    if not n:
        raise ValueError("scene_name 为空")
    hits: List[int] = []
    for s in scenes:
        sid = int(s.get("scene_id", -1))
        kws = s.get("search_keywords") or []
        if isinstance(kws, str):
            kws = [kws]
        kw_blob = " ".join(str(x) for x in kws).lower()
        city_blob = _city_blob(s)
        hay = f"{sid} {kw_blob} {city_blob}"
        if n == str(sid) or n in kw_blob or n in city_blob or kw_blob in n or n in hay:
            hits.append(sid)
    if len(hits) == 1:
        return int(hits[0])
    raise ValueError(f"无法根据 scene_name 唯一匹配场景: {name!r}（命中 {len(hits)} 条）")


def _apply_decision(decision: Dict[str, Any], scenes: List[Dict[str, Any]]) -> tuple[int, bool]:
    action = decision["action"]
    if action == "create_new":
        raw = decision["new_scene"]
        ns = {k: raw[k] for k in NEW_SCENE_BODY_KEYS}
        sid = scene_manager.create_new_scene(ns)
        return sid, True
    sid_opt: Optional[int] = decision.get("scene_id")
    if sid_opt is not None:
        sid = int(sid_opt)
        if not scene_manager.get_scene_by_id(sid):
            raise ValueError(f"scene_id={sid} 不存在")
        scene_manager.update_scene_from_ai(False, sid)
        return sid, False
    name = decision.get("scene_name")
    if name:
        sid = resolve_scene_name_to_id(scenes, str(name))
        scene_manager.update_scene_from_ai(False, sid)
        return sid, False
    raise ValueError("reuse_existing 缺少可用的 scene_id / scene_name")


def prepare_scene_from_txt_file(file_path: str) -> Dict[str, Any]:
    """
    读取本地 txt，经 LLM 决策后创建或复用场景。

    返回:
      ok: bool
      scene_id: int | None
      is_new_scene: bool
      reason: str
      error: str | None

    失败时 ok=False，HTTP 层（如 start_from_txt）可将 error 映射为 400 等，便于客户端区分。
    """
    path = (file_path or "").strip()
    if not path:
        return {
            "ok": False,
            "scene_id": None,
            "is_new_scene": False,
            "reason": "",
            "error": "file_path 为空",
        }
    try:
        user_text = ingest_user_document_to_text(path)
    except ValueError as e:
        return {
            "ok": False,
            "scene_id": None,
            "is_new_scene": False,
            "reason": "",
            "error": str(e),
        }
    except Exception as e:
        log.exception("prepare_scene: 解析文件失败")
        return {
            "ok": False,
            "scene_id": None,
            "is_new_scene": False,
            "reason": "",
            "error": f"解析文件失败: {e}",
        }
    if not (user_text or "").strip():
        return {
            "ok": False,
            "scene_id": None,
            "is_new_scene": False,
            "reason": "",
            "error": "文件内容为空或读取失败",
        }
    scenes = scene_manager.get_all_scenes()
    try:
        decision = llm_prepare_scene_decision(user_text, scenes)
        sid, is_new = _apply_decision(decision, scenes)
    except Exception as e:
        log.warning("prepare_scene: %s", e)
        return {
            "ok": False,
            "scene_id": None,
            "is_new_scene": False,
            "reason": "",
            "error": str(e),
        }
    return {
        "ok": True,
        "scene_id": sid,
        "is_new_scene": is_new,
        "reason": str(decision.get("reason") or ""),
        "error": None,
    }
