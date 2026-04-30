# -*- coding: utf-8 -*-
"""LangGraph 编排：可选「场景准备」→ 登录 → 爬取 → 初筛 → 精筛（后续步骤通过 requests 调本服务）。

调用约定（与对话/文档一致，维护者以本仓库代码为准；聊天说明不会自动进入运行时）：
- user_file_path strip 后非空：先走 prepare_scene（进程内调 scene_prepare；路径支持 txt/md/图/pdf，见 resume_document_ingest），
  再按序 requests 调本机 /api/liepin_login 等；须先启动 uvicorn；默认基址与 config.PORT 一致（见 config.AGENT_API_BASE_URL）。
- 不传 user_file_path：必须在初始 state 中带 scene_id，从 login 起跑。
- run_pipeline 要求 scene_id 与 user_file_path 二选一；编排图不用百炼做「下一步」分支，条件边为硬编码；
  百炼/qwen 仅在业务接口与 llm_prepare_scene_decision 等 LLM 调用里使用（见 .env 的 LLM_CHAT_MODEL）。

环境变量：AGENT_API_BASE_URL（未设置时由 config 按 PORT 生成）、
AGENT_TIMEOUT_LOGIN_S / AGENT_TIMEOUT_CRAWL_S / AGENT_TIMEOUT_PREFILTER_S / AGENT_TIMEOUT_SUBMIT_S（秒）。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Literal, NotRequired, TypedDict

import config as cfg
import requests
from langgraph.graph import END, START, StateGraph

try:
    from pathlib import Path

    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parent / ".env")
except ImportError:
    pass


def _env_float(name: str, default: float) -> float:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class AgentHttpConfig:
    api_base: str
    timeout_login: float = 600.0
    timeout_crawl: float = 900.0
    timeout_prefilter: float = 600.0
    timeout_submit: float = 600.0

    @classmethod
    def from_env(cls, api_base: str | None = None) -> AgentHttpConfig:
        if api_base is not None:
            base = str(api_base).strip().rstrip("/")
        else:
            base = str(getattr(cfg, "AGENT_API_BASE_URL", "") or "").strip().rstrip("/") or (
                f"http://127.0.0.1:{int(getattr(cfg, 'PORT', 8000) or 8000)}"
            )
        return cls(
            api_base=base,
            timeout_login=_env_float("AGENT_TIMEOUT_LOGIN_S", 600.0),
            timeout_crawl=_env_float("AGENT_TIMEOUT_CRAWL_S", 900.0),
            timeout_prefilter=_env_float("AGENT_TIMEOUT_PREFILTER_S", 600.0),
            timeout_submit=_env_float("AGENT_TIMEOUT_SUBMIT_S", 600.0),
        )


class JobAgentState(TypedDict, total=False):
    """LangGraph 状态。

    scene_id：跳过文件准备时由调用方传入；走 user_file_path 时由 prepare_scene 节点写入。
    user_file_path：空串表示不走准备节点，与「二选一」约定一致。
    """

    scene_id: NotRequired[int]
    user_file_path: str
    logged_in: bool
    crawled: bool
    prefiltered: bool
    submitted: bool
    error: str | None
    message: str
    reset_checkpoint: bool
    include_company: bool
    include_location: bool
    include_salary: bool


def _fmt_body_for_error(body: Any) -> str:
    if isinstance(body, dict):
        parts = [
            str(body.get("msg") or ""),
            str(body.get("message") or ""),
            str(body.get("status") or ""),
        ]
        s = " | ".join(p for p in parts if p)
        if s:
            return s[:800]
        try:
            return json.dumps(body, ensure_ascii=False)[:800]
        except Exception:
            return str(body)[:800]
    return str(body)[:800]


def _append_message(state: JobAgentState, fragment: str) -> str:
    prev = (state.get("message") or "").strip()
    if not prev:
        return fragment
    return f"{prev}；{fragment}"


def _post_json(
    cfg: AgentHttpConfig,
    path: str,
    *,
    params: dict[str, Any],
    timeout: float,
) -> tuple[bool, Any]:
    url = f"{cfg.api_base}{path}"
    try:
        r = requests.post(url, params=params, timeout=timeout)
    except requests.RequestException as e:
        return False, {"code": 500, "msg": f"HTTP 请求异常: {e}"}
    try:
        body = r.json()
    except ValueError:
        body = {
            "code": 500 if not r.ok else 200,
            "msg": (r.text or "")[:500],
        }
    if not isinstance(body, dict):
        return False, {"code": 500, "msg": "响应非 JSON 对象"}
    code = body.get("code", 200 if r.ok else 500)
    try:
        code_ok = int(code) == 200
    except (TypeError, ValueError):
        code_ok = False
    ok = bool(r.ok and code_ok)
    return ok, body


def prepare_scene_node(state: JobAgentState) -> dict[str, Any]:
    # 与 /api/start_from_txt 同源逻辑见 services.scene_prepare；此处不 HTTP 自调用，避免环路与重复开销。
    if state.get("error"):
        return {}
    path = (state.get("user_file_path") or "").strip()
    if not path:
        return {}
    from services.scene_prepare import prepare_scene_from_txt_file

    r = prepare_scene_from_txt_file(path)
    if not r.get("ok"):
        err = str(r.get("error") or "场景准备失败")
        return {
            "error": err,
            "message": _append_message(state, f"准备场景: 失败 — {err}"),
        }
    sid = int(r["scene_id"])
    reason = str(r.get("reason") or "").strip()
    tail = f"scene_id={sid}" + (f"，{reason}" if reason else "")
    return {
        "scene_id": sid,
        "error": None,
        "message": _append_message(state, f"准备场景: 成功（{tail}）"),
    }


def build_graph(cfg: AgentHttpConfig) -> Any:
    def node_login(state: JobAgentState) -> dict[str, Any]:
        if state.get("error"):
            return {}
        sid = state.get("scene_id")
        if sid is None:
            return {
                "error": "缺少 scene_id（请先完成场景准备或传入 scene_id）",
                "message": _append_message(state, "登录: 跳过（无 scene_id）"),
            }
        ok, body = _post_json(cfg, "/api/liepin_login", params={}, timeout=cfg.timeout_login)
        frag = "登录: 成功" if ok else "登录: 失败"
        return {
            "logged_in": ok,
            "error": None if ok else _fmt_body_for_error(body),
            "message": _append_message(state, frag),
        }

    def node_crawl(state: JobAgentState) -> dict[str, Any]:
        if state.get("error"):
            return {}
        sid = int(state["scene_id"])
        params: dict[str, Any] = {"scene_id": sid}
        if state.get("reset_checkpoint"):
            params["reset_checkpoint"] = True
        ok, body = _post_json(
            cfg,
            "/api/crawl_liepin_crawl_only",
            params=params,
            timeout=cfg.timeout_crawl,
        )
        frag = "爬取: 成功" if ok else "爬取: 失败"
        return {
            "crawled": ok,
            "error": None if ok else _fmt_body_for_error(body),
            "message": _append_message(state, frag),
        }

    def node_prefilter(state: JobAgentState) -> dict[str, Any]:
        if state.get("error"):
            return {}
        sid = int(state["scene_id"])
        params: dict[str, Any] = {"scene_id": sid}
        if state.get("include_company"):
            params["include_company"] = True
        if state.get("include_location"):
            params["include_location"] = True
        if state.get("include_salary"):
            params["include_salary"] = True
        ok, body = _post_json(
            cfg,
            "/api/prefilter_titles_for_scene",
            params=params,
            timeout=cfg.timeout_prefilter,
        )
        frag = "初筛: 成功" if ok else "初筛: 失败"
        return {
            "prefiltered": ok,
            "error": None if ok else _fmt_body_for_error(body),
            "message": _append_message(state, frag),
        }

    def node_submit(state: JobAgentState) -> dict[str, Any]:
        if state.get("error"):
            return {}
        sid = int(state["scene_id"])
        ok, body = _post_json(
            cfg,
            "/api/submit_llm_for_scene",
            params={"scene_id": sid},
            timeout=cfg.timeout_submit,
        )
        frag = "精筛: 成功" if ok else "精筛: 失败"
        return {
            "submitted": ok,
            "error": None if ok else _fmt_body_for_error(body),
            "message": _append_message(state, frag),
        }

    def route_entry(state: JobAgentState) -> Literal["prepare", "login"]:
        if (state.get("user_file_path") or "").strip():
            return "prepare"
        return "login"

    def route_after_prepare(state: JobAgentState) -> Literal["login", "end"]:
        return "end" if state.get("error") else "login"

    def route_after_login(state: JobAgentState) -> Literal["crawl", "end"]:
        return "end" if state.get("error") else "crawl"

    def route_after_crawl(state: JobAgentState) -> Literal["prefilter", "end"]:
        return "end" if state.get("error") else "prefilter"

    def route_after_prefilter(state: JobAgentState) -> Literal["submit", "end"]:
        return "end" if state.get("error") else "submit"

    g: StateGraph[JobAgentState] = StateGraph(JobAgentState)
    g.add_node("prepare_scene", prepare_scene_node)
    g.add_node("login", node_login)
    g.add_node("crawl", node_crawl)
    g.add_node("prefilter", node_prefilter)
    g.add_node("submit", node_submit)

    g.add_conditional_edges(
        START,
        route_entry,
        {"prepare": "prepare_scene", "login": "login"},
    )
    g.add_conditional_edges(
        "prepare_scene",
        route_after_prepare,
        {"login": "login", "end": END},
    )
    g.add_conditional_edges(
        "login",
        route_after_login,
        {"crawl": "crawl", "end": END},
    )
    g.add_conditional_edges(
        "crawl",
        route_after_crawl,
        {"prefilter": "prefilter", "end": END},
    )
    g.add_conditional_edges(
        "prefilter",
        route_after_prefilter,
        {"submit": "submit", "end": END},
    )
    g.add_edge("submit", END)
    return g.compile()


def run_pipeline(
    *,
    scene_id: int | None = None,
    user_file_path: str | None = None,
    reset_checkpoint: bool = False,
    include_company: bool = False,
    include_location: bool = False,
    include_salary: bool = False,
    api_base: str | None = None,
) -> JobAgentState:
    """执行完整图。须 scene_id 与 user_file_path（strip 后）二选一，否则 ValueError。

    HTTP 封装见 main.py POST /api/agent/run（内部 asyncio.to_thread 防同进程 requests 死锁）。
    """
    uf = (user_file_path or "").strip()
    if not uf and scene_id is None:
        raise ValueError("需要 scene_id 或 user_file_path 之一")
    cfg = AgentHttpConfig.from_env(api_base)
    app = build_graph(cfg)
    initial: JobAgentState = {
        "user_file_path": uf,
        "logged_in": False,
        "crawled": False,
        "prefiltered": False,
        "submitted": False,
        "error": None,
        "message": "",
        "reset_checkpoint": bool(reset_checkpoint),
        "include_company": bool(include_company),
        "include_location": bool(include_location),
        "include_salary": bool(include_salary),
    }
    if not uf:
        initial["scene_id"] = int(scene_id)
    out: JobAgentState = app.invoke(initial)
    return out
