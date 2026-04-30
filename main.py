# -*- coding:utf-8 -*-
# @CreatTime : 2026 19:06
# @Author : XZN

# API 目录（实现见 api/ 包；除特别标注外均为 POST）：
# - /api/liepin_login：打开猎聘并等待人工登录（保存 storage_state）。当前 LangGraph 已移除登录节点，爬虫会自行检查/恢复登录态。
# - /api/start_from_txt：Body file_path（服务器本地路径）；支持 txt/md/图/pdf，见 resume_document_ingest。与 prepare_scene 同源。
# - /api/start_from_text：Body text（直贴文本）；写入 data/scene_temp.<timestamp>.txt 后同上。
# - /api/start_from_upload：multipart 上传单文件（≤MAX_SCENE_UPLOAD_BYTES），写临时文件后同上。
# - /api/scenes/recognize_fields：从文本抽取标准场景字段（search_keywords≤3、city、远程、薪资、requirements），仅回填表单，不创建场景。
# - /api/scenes/{scene_id}/resume/upload：仅更新 data/resume/{scene_id}.txt（不触发场景匹配）。
# - /api/scenes/list：GET，返回所有场景（结构化字段）。
# - /api/scenes/runtime_status：GET，返回某 scene 的 agent/crawl/prefilter/submit 是否运行中 + 最近 agent 任务摘要。
#
# - /api/crawl_liepin：爬虫 +（可选）LLM + 写 CSV；Query：scene_id，crawl_only，reset_checkpoint，caller(内部调度标记)。
# - /api/crawl_liepin_crawl_only：只爬取并写 SQLite（list_jobs），不提交 LLM/不写 CSV；Query：scene_id，reset_checkpoint，caller。
# - /api/prefilter_titles_for_scene：标题初筛写回 SQLite（reject→低/否/标题预判；其余→pending）；Query：scene_id，include_*，caller。
# - /api/submit_llm_for_scene：对 pending 做详情精筛并写回 SQLite/CSV；Query：scene_id，caller。
#
# - /api/jobs/matched：GET，分页/排序读取中高匹配岗位；Query：scene_id，page/page_size，sort_by(fetch_timestamp|apply|match_level)，sort_dir。
# - /api/jobs/manual_reject：人工标注不投递原因（只接受含中英文的 reason），写入 manual_apply/manual_reason。
# - /api/feedback：人工反馈后更新记忆（memory）；Query：scene_id。
#
# - /api/agent/run：LangGraph 流水线。须 scene_id 与 user_file_path 二选一；线程池执行 run_pipeline 避免同进程 requests 自调死锁。
# - /api/agent/run_async：异步启动，立即返回 task_id；配合 GET /api/agent/task/{task_id} 轮询状态。

import uvicorn
from fastapi import FastAPI
from fastapi import HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from api import register_routes
from config import HOST, PORT
from config import log

from pathlib import Path

app = FastAPI(title="job finding AI Agent", version="1.0")
register_routes(app)


def _ensure_localhost(request: Request) -> None:
    host = (getattr(request.client, "host", None) or "").strip().lower()
    if host not in {"127.0.0.1", "::1", "localhost"}:
        raise HTTPException(status_code=403, detail="仅允许本机访问（localhost/127.0.0.1）")


# 静态前端：配置生成器
_STATIC_DIR = Path(__file__).resolve().parent / "static"
if _STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")
else:
    log.info("static dir not found: %s", _STATIC_DIR)


@app.get("/config", include_in_schema=False)
def config_page(request: Request) -> RedirectResponse:
    _ensure_localhost(request)
    return RedirectResponse(url="/static/config/index.html")

@app.get("/", include_in_schema=False)
def root_page(request: Request) -> RedirectResponse:
    _ensure_localhost(request)
    return RedirectResponse(url="/static/workspace/index.html")

@app.get("/workspace", include_in_schema=False)
def workspace_page(request: Request) -> RedirectResponse:
    _ensure_localhost(request)
    return RedirectResponse(url="/static/workspace/workspace.html")

@app.get("/workspace/scene/{scene_id}", include_in_schema=False)
def workspace_scene_page(request: Request, scene_id: int) -> RedirectResponse:
    _ensure_localhost(request)
    return RedirectResponse(url=f"/static/workspace/scene.html?scene_id={int(scene_id)}")


@app.get("/scenes", include_in_schema=False)
def scenes_create_page(request: Request) -> RedirectResponse:
    _ensure_localhost(request)
    return RedirectResponse(url="/static/scenes/index.html")


@app.get("/scenes/list", include_in_schema=False)
def scenes_list_page(request: Request) -> RedirectResponse:
    _ensure_localhost(request)
    return RedirectResponse(url="/static/scenes/list.html")


if __name__ == "__main__":
    # uvicorn.run("main:app", host=HOST, port=PORT, reload=DEBUG)
    uvicorn.run("main:app", host=HOST, port=PORT)
