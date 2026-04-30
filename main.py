# -*- coding:utf-8 -*-
# @CreatTime : 2026 19:06
# @Author : XZN

# API 目录（均为 POST，实现见 api/ 包内各模块）：
# - /api/liepin_login：打开猎聘并等待人工登录（保存 storage_state），用于后续自动登录复用
# - /api/start_from_txt：Body file_path（服务器本地路径）；支持 txt/md/图/pdf，见 resume_document_ingest。
#   成功 code=200；业务失败 code=400。与 LangGraph prepare_scene 同源。
# - /api/start_from_upload：multipart 上传单文件（≤15MB），写临时文件后同上；便于浏览器端上传。
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
# - /api/agent/run：LangGraph 流水线。须 scene_id 与 user_file_path（Query，strip 后）二选一，否则 400。
#   有 user_file_path 时先进程内准备场景再 HTTP 爬筛链；仅 scene_id 时等同原「从登录起」流程。
#   须本服务已监听且 AGENT_API_BASE_URL 指向该地址；线程池执行 run_pipeline 以免 requests 自调死锁。
#   - query：scene_id、user_file_path、reset_checkpoint、include_*

import uvicorn
from fastapi import FastAPI

from api import register_routes
from config import HOST, PORT

app = FastAPI(title="job finding AI Agent", version="1.0")
register_routes(app)

if __name__ == "__main__":
    # uvicorn.run("main:app", host=HOST, port=PORT, reload=DEBUG)
    uvicorn.run("main:app", host=HOST, port=PORT)
