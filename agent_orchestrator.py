# -*- coding: utf-8 -*-
"""LangGraph 编排（规则 Planner 版）：可选「场景准备」→ observe → plan&act 循环 → END。

该模块的目标不是“固定流水线”，而是让 Agent **基于规则**自行决定本次需要调用哪些接口。

调用约定（维护者以本仓库代码为准；聊天说明不会自动进入运行时）：
- `run_pipeline` 要求 `scene_id` 与 `user_file_path`（strip 后）二选一，否则 ValueError。
- 若传 `user_file_path`：先在进程内执行 `prepare_scene_node`（复用 services.scene_prepare），得到 scene_id 后再进入 planner。
- 若只传 `scene_id`：直接进入 planner。

规则 Planner（不使用 LLM）：
- 每轮先 `observe_scene_node` 读取“世界状态”（SQLite + checkpoint）：
  - job_count / last_fetch_timestamp（services.job_store.get_crawl_scene_stats）
  - pending/unprocessed 计数（services.job_store.get_crawl_scene_match_counts）
  - 是否存在断点（utils.crawl_checkpoint.has_liepin_scene_checkpoint）
- 再 `plan_and_act_node` 按规则选择并执行 **一个动作**，然后回到 observe：
  - crawl: POST /api/crawl_liepin_crawl_only（内部调用带 caller=agent；爬虫自检/恢复登录态）
  - prefilter: POST /api/prefilter_titles_for_scene（caller=agent）
  - submit: POST /api/submit_llm_for_scene（caller=agent）
  - stop: 无需动作则结束
- 循环步数上限：环境变量 `AGENT_PLANNER_MAX_STEPS`（默认 6），防止异常状态下无限循环。

HTTP 自调用说明：
- planner 的 act 阶段通过 requests 调本服务（默认基址 config.AGENT_API_BASE_URL，与 PORT 对齐）。
- 超时（秒）：`AGENT_TIMEOUT_CRAWL_S` / `AGENT_TIMEOUT_PREFILTER_S` / `AGENT_TIMEOUT_SUBMIT_S`。
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
    # 规则 planner
    planner_step: int
    planner_max_steps: int
    planner_action: str
    planner_reason: str
    last_action_ok: bool


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
    def _call_crawl(state: JobAgentState) -> tuple[bool, Any]:
        if state.get("error"):
            return False, {"code": 500, "msg": "prior error"}
        sid = int(state["scene_id"])
        params: dict[str, Any] = {"scene_id": sid, "caller": "agent"}
        if state.get("reset_checkpoint"):
            params["reset_checkpoint"] = True
        ok, body = _post_json(
            cfg,
            "/api/crawl_liepin_crawl_only",
            params=params,
            timeout=cfg.timeout_crawl,
        )
        return ok, body

    def _call_prefilter(state: JobAgentState) -> tuple[bool, Any]:
        if state.get("error"):
            return False, {"code": 500, "msg": "prior error"}
        sid = int(state["scene_id"])
        params: dict[str, Any] = {"scene_id": sid, "caller": "agent"}
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
        return ok, body

    def _call_submit(state: JobAgentState) -> tuple[bool, Any]:
        if state.get("error"):
            return False, {"code": 500, "msg": "prior error"}
        sid = int(state["scene_id"])
        ok, body = _post_json(
            cfg,
            "/api/submit_llm_for_scene",
            params={"scene_id": sid, "caller": "agent"},
            timeout=cfg.timeout_submit,
        )
        return ok, body

    def observe_scene_node(state: JobAgentState) -> dict[str, Any]:
        """规则 planner：观测场景当前状态（不调用 LLM）。"""
        if state.get("error"):
            log.info("agent node observe: skip (prior error)")
            return {}
        sid = state.get("scene_id")
        if sid is None:
            return {
                "error": "缺少 scene_id（请先完成场景准备或传入 scene_id）",
                "message": _append_message(state, "observe: 缺少 scene_id"),
            }
        scene_id = int(sid)
        try:
            from services.job_store import get_crawl_scene_match_counts, get_crawl_scene_stats
            from utils.crawl_checkpoint import has_liepin_scene_checkpoint

            stats = get_crawl_scene_stats(platform="liepin", scene_id=scene_id)
            counts = get_crawl_scene_match_counts(platform="liepin", scene_id=scene_id)
            has_cp = bool(has_liepin_scene_checkpoint(scene_id))
            job_count = int(stats.get("job_count") or 0)
            last_ts = str(stats.get("last_fetch_timestamp") or "").strip()
            pending_count = int(counts.get("pending_count") or 0)
            unprocessed_count = int(counts.get("unprocessed_count") or 0)
        except Exception as e:
            err = f"observe 失败: {e}"
            return {"error": err, "message": _append_message(state, err)}

        # need_crawl 规则与旧 decide_crawl 一致
        reasons: list[str] = []
        need_crawl = False
        if has_cp:
            need_crawl = True
            reasons.append("存在断点")
        if job_count < 10:
            need_crawl = True
            reasons.append(f"job_count={job_count}<10")
        if not last_ts:
            need_crawl = True
            reasons.append("last_fetch_timestamp 为空")
        else:
            try:
                from datetime import datetime

                ts_norm = last_ts.replace("Z", "+00:00")
                dt = datetime.fromisoformat(ts_norm)
                age_h = (datetime.now(dt.tzinfo) - dt).total_seconds() / 3600.0
                if age_h > 48.0:
                    need_crawl = True
                    reasons.append(f"距上次爬取 {age_h:.1f}h>48h")
            except Exception:
                need_crawl = True
                reasons.append("last_fetch_timestamp 无法解析")
        crawl_reason = "；".join(reasons) if reasons else f"数据新且完整（job_count={job_count} last={last_ts}）"
        msg = (
            f"observe: job_count={job_count} pending={pending_count} unprocessed={unprocessed_count} "
            f"checkpoint={has_cp} need_crawl={need_crawl}"
        )
        log.info("agent node observe: %s", msg)
        return {
            "need_crawl": bool(need_crawl),
            "crawl_reason": crawl_reason,
            "message": _append_message(state, msg),
            # 将观测值写入 message 即可；真正决策在 act 节点
        }

    def plan_and_act_node(state: JobAgentState) -> dict[str, Any]:
        """规则 planner：根据观测选择一个动作并执行一遍（单步）。"""
        if state.get("error"):
            log.info("agent node plan_act: skip (prior error)")
            return {}
        sid = state.get("scene_id")
        if sid is None:
            return {"error": "缺少 scene_id", "message": _append_message(state, "plan_act: 缺少 scene_id")}
        scene_id = int(sid)
        step = int(state.get("planner_step") or 0)
        max_steps = int(state.get("planner_max_steps") or 6)
        if step >= max_steps:
            return {
                "planner_action": "stop",
                "planner_reason": f"达到最大步数 {max_steps}",
                "message": _append_message(state, f"planner: stop（达到最大步数 {max_steps}）"),
            }

        # 重新读取一次 counts，避免仅靠 state（state 里不存 counts，避免污染）
        try:
            from services.job_store import get_crawl_scene_match_counts
            from utils.crawl_checkpoint import has_liepin_scene_checkpoint

            counts = get_crawl_scene_match_counts(platform="liepin", scene_id=scene_id)
            pending_count = int(counts.get("pending_count") or 0)
            unprocessed_count = int(counts.get("unprocessed_count") or 0)
            has_cp = bool(has_liepin_scene_checkpoint(scene_id))
        except Exception:
            pending_count = 0
            unprocessed_count = 0
            has_cp = False

        need_crawl = bool(state.get("need_crawl"))

        action: Literal["crawl", "prefilter", "submit", "stop"]
        reason: str
        if has_cp or need_crawl:
            action = "crawl"
            reason = str(state.get("crawl_reason") or "need_crawl")
        elif unprocessed_count > 0:
            action = "prefilter"
            reason = f"unprocessed_count={unprocessed_count} > 0"
        elif pending_count > 0:
            action = "submit"
            reason = f"pending_count={pending_count} > 0"
        else:
            action = "stop"
            reason = "无需要执行的动作"

        log.info("agent node plan_act: step=%s action=%s reason=%s", step, action, reason[:300])
        if action == "stop":
            return {
                "planner_step": step + 1,
                "planner_action": "stop",
                "planner_reason": reason,
                "last_action_ok": True,
                "message": _append_message(state, f"planner: stop（{reason}）"),
            }

        if action == "crawl":
            ok, body = _call_crawl(state)
            frag = "crawl: ok" if ok else f"crawl: fail { _fmt_body_for_error(body) }"
            return {
                "planner_step": step + 1,
                "planner_action": "crawl",
                "planner_reason": reason,
                "last_action_ok": bool(ok),
                "crawled": bool(ok),
                "error": None if ok else _fmt_body_for_error(body),
                "message": _append_message(state, frag),
            }
        if action == "prefilter":
            ok, body = _call_prefilter(state)
            frag = "prefilter: ok" if ok else f"prefilter: fail { _fmt_body_for_error(body) }"
            return {
                "planner_step": step + 1,
                "planner_action": "prefilter",
                "planner_reason": reason,
                "last_action_ok": bool(ok),
                "prefiltered": bool(ok),
                "error": None if ok else _fmt_body_for_error(body),
                "message": _append_message(state, frag),
            }
        # submit
        ok, body = _call_submit(state)
        frag = "submit: ok" if ok else f"submit: fail { _fmt_body_for_error(body) }"
        return {
            "planner_step": step + 1,
            "planner_action": "submit",
            "planner_reason": reason,
            "last_action_ok": bool(ok),
            "submitted": bool(ok),
            "error": None if ok else _fmt_body_for_error(body),
            "message": _append_message(state, frag),
        }

    def route_entry(state: JobAgentState) -> Literal["prepare", "observe"]:
        if (state.get("user_file_path") or "").strip():
            return "prepare"
        return "observe"

    def route_after_prepare(state: JobAgentState) -> Literal["observe", "end"]:
        return "end" if state.get("error") else "observe"

    def route_after_act(state: JobAgentState) -> Literal["observe", "end"]:
        if state.get("error"):
            return "end"
        if str(state.get("planner_action") or "") == "stop":
            return "end"
        # 继续循环
        return "observe"

    g: StateGraph[JobAgentState] = StateGraph(JobAgentState)
    g.add_node("prepare_scene", prepare_scene_node)
    g.add_node("observe", observe_scene_node)
    g.add_node("plan_act", plan_and_act_node)

    g.add_conditional_edges(
        START,
        route_entry,
        {"prepare": "prepare_scene", "observe": "observe"},
    )
    g.add_conditional_edges(
        "prepare_scene",
        route_after_prepare,
        {"observe": "observe", "end": END},
    )
    g.add_edge("observe", "plan_act")
    g.add_conditional_edges(
        "plan_act",
        route_after_act,
        {"observe": "observe", "end": END},
    )
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
        "planner_step": 0,
        "planner_max_steps": int(_env_float("AGENT_PLANNER_MAX_STEPS", 6.0) or 6),
        "planner_action": "",
        "planner_reason": "",
        "last_action_ok": True,
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
