# -*- coding: utf-8 -*-
"""LangGraph 编排：可选「场景准备」→（爬取决策→可选爬取）→ 初筛 → 精筛（后续步骤通过 requests 调本服务）。

调用约定（与对话/文档一致，维护者以本仓库代码为准；聊天说明不会自动进入运行时）：
- user_file_path strip 后非空：先走 prepare_scene（进程内调 scene_prepare；路径支持 txt/md/图/pdf，见 resume_document_ingest），
  再按序 requests 调本机 /api/liepin_login 等；须先启动 uvicorn；默认基址与 config.PORT 一致（见 config.AGENT_API_BASE_URL）。
- 不传 user_file_path：必须在初始 state 中带 scene_id，从登录决策起跑。
- run_pipeline 要求 scene_id 与 user_file_path 二选一；编排图不用百炼做「下一步」分支，条件边为硬编码；
  百炼/qwen 仅在业务接口与 llm_prepare_scene_decision 等 LLM 调用里使用（见 .env 的 LLM_CHAT_MODEL）。

规则决策节点（不使用 LLM）：
- decide_crawl：根据 SQLite list_jobs（job_count、last_fetch_timestamp）、以及 checkpoint.json 是否存在该 scene 的断点判断是否需要重新爬取。
  need_crawl=false 时会跳过 /api/crawl_liepin_crawl_only，直接进入标题初筛。

环境变量：AGENT_API_BASE_URL（未设置时由 config 按 PORT 生成）、
AGENT_TIMEOUT_LOGIN_S / AGENT_TIMEOUT_CRAWL_S / AGENT_TIMEOUT_PREFILTER_S / AGENT_TIMEOUT_SUBMIT_S（秒）。
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from typing import Any, Literal, NotRequired, TypedDict

import config as cfg
from config import log
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
    need_login: bool
    login_reason: str
    need_crawl: bool
    crawl_reason: str


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
        log.info("agent node prepare_scene: skip (prior error)")
        return {}
    path = (state.get("user_file_path") or "").strip()
    if not path:
        log.info("agent node prepare_scene: skip (empty user_file_path)")
        return {}
    from services.scene_prepare import prepare_scene_from_txt_file

    log.info("agent node prepare_scene: start path=%s", path[:200] + ("..." if len(path) > 200 else ""))
    r = prepare_scene_from_txt_file(path)
    if not r.get("ok"):
        err = str(r.get("error") or "场景准备失败")
        log.info("agent node prepare_scene: failed error=%s", err[:300])
        return {
            "error": err,
            "message": _append_message(state, f"准备场景: 失败 — {err}"),
        }
    sid = int(r["scene_id"])
    reason = str(r.get("reason") or "").strip()
    tail = f"scene_id={sid}" + (f"，{reason}" if reason else "")
    log.info("agent node prepare_scene: ok scene_id=%s", sid)
    return {
        "scene_id": sid,
        "error": None,
        "message": _append_message(state, f"准备场景: 成功（{tail}）"),
    }

def decide_if_need_login_node(state: JobAgentState) -> dict[str, Any]:
    """规则判断是否需要重新登录（不调用 LLM）。"""
    if state.get("error"):
        log.info("agent node decide_login: skip (prior error)")
        return {}
    path = str(getattr(cfg, "LIEPIN_STORAGE_STATE_PATH", "") or "").strip()
    if not path:
        reason = "未配置 LIEPIN_STORAGE_STATE_PATH"
        log.info("agent node decide_login: need_login=true reason=%s", reason)
        return {
            "need_login": True,
            "login_reason": reason,
            "message": _append_message(state, f"登录决策: 需要登录（{reason}）"),
        }
    try:
        st = os.stat(path)
        size_ok = st.st_size > 50
        age_s = max(0.0, time.time() - float(st.st_mtime))
        age_days = age_s / (24 * 3600)
        if not size_ok:
            reason = "storage_state 为空/过小"
            need = True
        elif age_s > 2 * 24 * 3600:
            reason = f"距上次登录态刷新已 {age_days:.1f} 天 > 2 天"
            need = True
        else:
            reason = f"登录态有效（距刷新 {age_days:.1f} 天）"
            need = False
    except FileNotFoundError:
        need = True
        reason = "storage_state 文件不存在"
    except Exception as e:
        need = True
        reason = f"storage_state 检查失败: {e}"
    log.info("agent node decide_login: need_login=%s reason=%s", need, reason[:300])
    frag = "登录: 将执行" if need else "登录: 跳过"
    # 跳过登录时标记 logged_in=True，表示可复用登录态（爬虫会加载 storage_state）
    patch: dict[str, Any] = {
        "need_login": bool(need),
        "login_reason": str(reason),
        "message": _append_message(state, f"登录决策: {frag}（{reason}）"),
    }
    if not need:
        patch["logged_in"] = True
    return patch


def build_graph(cfg: AgentHttpConfig) -> Any:
    def decide_if_need_crawl_node(state: JobAgentState) -> dict[str, Any]:
        """规则判断是否需要重新爬取（不调用 LLM）。"""
        if state.get("error"):
            log.info("agent node decide_crawl: skip (prior error)")
            return {}
        sid = state.get("scene_id")
        if sid is None:
            reason = "缺少 scene_id"
            log.info("agent node decide_crawl: need_crawl=true reason=%s", reason)
            return {
                "need_crawl": True,
                "crawl_reason": reason,
                "message": _append_message(state, f"爬取决策: 需要爬取（{reason}）"),
            }
        scene_id = int(sid)
        try:
            from services.job_store import get_crawl_scene_stats
            from utils.crawl_checkpoint import has_liepin_scene_checkpoint

            stats = get_crawl_scene_stats(platform="liepin", scene_id=scene_id)
            job_count = int(stats.get("job_count") or 0)
            last_ts = str(stats.get("last_fetch_timestamp") or "").strip()
            has_cp = bool(has_liepin_scene_checkpoint(scene_id))
        except Exception as e:
            reason = f"读取爬取统计/断点失败: {e}"
            log.info("agent node decide_crawl: need_crawl=true reason=%s", reason[:300])
            return {
                "need_crawl": True,
                "crawl_reason": reason,
                "message": _append_message(state, f"爬取决策: 需要爬取（{reason}）"),
            }

        reasons: list[str] = []
        need = False
        if has_cp:
            need = True
            reasons.append("存在断点（未完成/需续爬）")
        if job_count < 10:
            need = True
            reasons.append(f"job_count={job_count} < 10")
        # last_fetch_timestamp 可能为空或非 ISO；这里用宽松解析：优先 datetime.fromisoformat，失败就按“需要爬”
        if not last_ts:
            need = True
            reasons.append("从未爬取（last_fetch_timestamp 为空）")
        else:
            try:
                from datetime import datetime

                # 兼容 "YYYY-MM-DD HH:MM:SS" 与 ISO
                ts_norm = last_ts.replace("Z", "+00:00")
                dt = datetime.fromisoformat(ts_norm)
                age_h = (datetime.now(dt.tzinfo) - dt).total_seconds() / 3600.0
                if age_h > 48.0:
                    need = True
                    reasons.append(f"距上次爬取 {age_h:.1f} 小时 > 48 小时")
            except Exception:
                need = True
                reasons.append("last_fetch_timestamp 无法解析")

        reason = "；".join(reasons) if reasons else f"数据新且完整（job_count={job_count} last={last_ts}）"
        log.info("agent node decide_crawl: need_crawl=%s reason=%s", need, reason[:300])
        frag = "爬取: 将执行" if need else "爬取: 跳过"
        patch: dict[str, Any] = {
            "need_crawl": bool(need),
            "crawl_reason": str(reason),
            "message": _append_message(state, f"爬取决策: {frag}（{reason}）"),
        }
        if not need:
            patch["crawled"] = True
        return patch

    def node_crawl(state: JobAgentState) -> dict[str, Any]:
        if state.get("error"):
            log.info("agent node crawl: skip (prior error)")
            return {}
        sid = int(state["scene_id"])
        params: dict[str, Any] = {"scene_id": sid, "caller": "agent"}
        if state.get("reset_checkpoint"):
            params["reset_checkpoint"] = True
        log.info(
            "agent node crawl: start scene_id=%s reset_checkpoint=%s POST /api/crawl_liepin_crawl_only",
            sid,
            params.get("reset_checkpoint", False),
        )
        ok, body = _post_json(
            cfg,
            "/api/crawl_liepin_crawl_only",
            params=params,
            timeout=cfg.timeout_crawl,
        )
        frag = "爬取: 成功" if ok else "爬取: 失败"
        log.info("agent node crawl: done scene_id=%s ok=%s", sid, ok)
        return {
            "crawled": ok,
            "error": None if ok else _fmt_body_for_error(body),
            "message": _append_message(state, frag),
        }

    def node_prefilter(state: JobAgentState) -> dict[str, Any]:
        if state.get("error"):
            log.info("agent node prefilter: skip (prior error)")
            return {}
        sid = int(state["scene_id"])
        params: dict[str, Any] = {"scene_id": sid, "caller": "agent"}
        if state.get("include_company"):
            params["include_company"] = True
        if state.get("include_location"):
            params["include_location"] = True
        if state.get("include_salary"):
            params["include_salary"] = True
        log.info(
            "agent node prefilter: start scene_id=%s include_company=%s include_location=%s include_salary=%s",
            sid,
            params.get("include_company", False),
            params.get("include_location", False),
            params.get("include_salary", False),
        )
        ok, body = _post_json(
            cfg,
            "/api/prefilter_titles_for_scene",
            params=params,
            timeout=cfg.timeout_prefilter,
        )
        frag = "初筛: 成功" if ok else "初筛: 失败"
        log.info("agent node prefilter: done scene_id=%s ok=%s", sid, ok)
        return {
            "prefiltered": ok,
            "error": None if ok else _fmt_body_for_error(body),
            "message": _append_message(state, frag),
        }

    def node_submit(state: JobAgentState) -> dict[str, Any]:
        if state.get("error"):
            log.info("agent node submit: skip (prior error)")
            return {}
        sid = int(state["scene_id"])
        log.info("agent node submit: start scene_id=%s POST /api/submit_llm_for_scene", sid)
        ok, body = _post_json(
            cfg,
            "/api/submit_llm_for_scene",
            params={"scene_id": sid, "caller": "agent"},
            timeout=cfg.timeout_submit,
        )
        frag = "精筛: 成功" if ok else "精筛: 失败"
        log.info("agent node submit: done scene_id=%s ok=%s", sid, ok)
        return {
            "submitted": ok,
            "error": None if ok else _fmt_body_for_error(body),
            "message": _append_message(state, frag),
        }

    def route_entry(state: JobAgentState) -> Literal["prepare", "crawl_decide"]:
        if (state.get("user_file_path") or "").strip():
            return "prepare"
        return "crawl_decide"

    def route_after_prepare(state: JobAgentState) -> Literal["crawl_decide", "end"]:
        return "end" if state.get("error") else "crawl_decide"

    def route_after_crawl(state: JobAgentState) -> Literal["prefilter", "end"]:
        return "end" if state.get("error") else "prefilter"

    def route_after_prefilter(state: JobAgentState) -> Literal["submit", "end"]:
        return "end" if state.get("error") else "submit"

    g: StateGraph[JobAgentState] = StateGraph(JobAgentState)
    g.add_node("prepare_scene", prepare_scene_node)
    g.add_node("decide_crawl", decide_if_need_crawl_node)
    g.add_node("crawl", node_crawl)
    g.add_node("prefilter", node_prefilter)
    g.add_node("submit", node_submit)

    g.add_conditional_edges(
        START,
        route_entry,
        {"prepare": "prepare_scene", "crawl_decide": "decide_crawl"},
    )
    g.add_conditional_edges(
        "prepare_scene",
        route_after_prepare,
        {"crawl_decide": "decide_crawl", "end": END},
    )
    # 决策：是否需要爬取
    g.add_conditional_edges(
        "decide_crawl",
        lambda s: "crawl" if bool(s.get("need_crawl")) else "prefilter",
        {"crawl": "crawl", "prefilter": "prefilter"},
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
        "need_login": False,
        "login_reason": "已移除登录节点（由爬虫内部检查/恢复登录态）",
        "need_crawl": True,
        "crawl_reason": "",
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
