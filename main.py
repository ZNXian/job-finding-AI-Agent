# -*- coding:utf-8 -*-
# @CreatTime : 2026 19:06
# @Author : XZN

from fastapi import FastAPI, Body,UploadFile,File
import uvicorn
# import asyncio

from crawlers.liepin import crawl_liepin, login_liepin
from services.llm_services import llm_process_job,llm_identify_scene
from services.scences import scene_manager
from services.memory_services import update_scene_memory
# import config
from utils.files import *
from utils.wrapper import *
from config import dynamic_jobconfig
import config as cfg
from config import HOST,PORT,DEBUG

app = FastAPI(title="job finding AI Agent", version="1.0")

# ==========================
# 猎聘登录接口
# ==========================
@app.post("/api/liepin_login")
@handle_api_exception
def liepin_login():
    login_liepin()
    return {
        "code": 200,
        "status": "success",
        "msg": "登录成功，已关闭浏览器"
    }
# ==========================
# 接口 0：自然语言匹配岗位场景
# ==========================
@app.post("/api/start_from_txt")
@handle_api_exception_async
async def create_scene_from_txt(file: UploadFile = File(...)):
    # 1. 保存临时txt
    # 1. 保存临时文件 → 读取并清洗文本
    user_text = read_and_clean_txt(file)
    # 2. 从 SceneManager 获取所有场景（内存读取，不读文件）
    scenes = scene_manager.get_all_scenes()
    # 3. 调用你已写好的 LLM 函数，判断是否新场景
    is_new, scene_result = llm_identify_scene(user_text, scenes)
    # 4. 调用你已写好的类方法，存储/更新场景
    scene_id = scene_manager.update_scene_from_ai(is_new, scene_result)
    return {
        "code": 200,
        "is_new_scene": is_new,
        "scene_id": scene_id,
        "msg": "场景匹配完成"
    }

# ==========================
# 接口 1：爬取 + AI 判断 → 输出CSV
# ==========================
@handle_api_exception
@app.post("/api/crawl_liepin")
def run_crawl_and_ai(scene_id: int):  # 这里接收场景ID
    # 加载当前场景的动态配置
    dynamic_jobconfig.set(scene_manager.get_dynamic_jobconfig(scene_id))
    # 1. 爬取岗位
    jobs = crawl_liepin()
    # 2. 调用 LLM + VLM 判断 并写入csv
    for job in jobs:
        write_to_csv(job,llm_process_job(job))
    #
    return {
        "code": 200,
        "status": "success",
        "csv_file": cfg.CSV_FILE
    }

# ==========================
# 接口 2：人工反馈 → 更新记忆
# # ==========================
@app.post("/api/feedback")
@handle_api_exception_async
async def feedback(scene_id: int):
    dynamic_jobconfig.set(scene_manager.get_dynamic_jobconfig(scene_id))
    update_scene_memory()
    return {
        "status": "success",
        "msg": f"场景{scene_id}记忆已更新"
    }

if __name__ == "__main__":
    # uvicorn.run("main:app", host=HOST, port=PORT, reload=DEBUG)
    uvicorn.run("main:app", host=HOST, port=PORT)