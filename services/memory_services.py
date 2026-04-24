# -*- coding:utf-8 -*-
# @CreatTime : 2026 10:14
# @Author : XZN
# 记忆文件加载/解析工具
import json
import os
import logging
import time
import csv
import config as cfg
from config import log


def get_rejected_links():
    """
    读取记忆文件，提取所有被拒绝岗位的link列表
    :return: 拒绝链接列表（list）
    """
    rejected_links = []
    # 1. 记忆文件不存在 → 返回空列表
    if not os.path.exists(cfg.MEMERY_FILE):
        log.info("ℹ️ 记忆文件不存在，无历史拒绝链接")
        return rejected_links

    # 2. 读取记忆文件
    try:
        with open(cfg.MEMERY_FILE, "r", encoding="utf-8") as f:
            memory_data = json.load(f)

        # 提取拒绝岗位的link（兼容不同字段名：link/链接）
        reject_jobs = memory_data.get("human_reject_jobs", [])
        for job in reject_jobs:
            link = job.get("link") or job.get("链接")  # 兼容两种字段名
            if link and link not in rejected_links:
                rejected_links.append(link)

        log.info(f"✅ 读取到{len(rejected_links)}条历史拒绝岗位链接")
        return rejected_links
    except Exception as e:
        log.error(f"❌ 读取记忆文件失败：{str(e)}")
        return rejected_links


def load_memory():
    """
    读取记忆文件
    :return: 原始记忆数据（dict）
    """
    # 1. 记忆文件不存在 → 返回空字典
    if not os.path.exists(cfg.MEMERY_FILE):
        log.info("✅ 记忆文件不存在")
        return {}

    # 2. 读取记忆文件
    try:
        with open(cfg.MEMERY_FILE, "r", encoding="utf-8") as f:
            memory_data = json.load(f)
        return memory_data
    except Exception as e:
        log.error(f"❌ 读取记忆失败：{str(e)}")
        return {}


def load_and_extract_memory(memory_data=None):
    """
    从记忆中提炼核心拒绝偏好（省Token版）
    :return: 精简的偏好描述文本（给AI用）
    """
    # 1. 记忆文件不存在 → 返回默认提示
    if not os.path.exists(cfg.MEMERY_FILE):
        log.info("✅ 记忆文件不存在")
        return "暂无历史拒绝偏好，按基础要求筛选即可"

    # 2. 读取记忆文件（未传入memory_data时重新读取）
    if memory_data is None:
        try:
            with open(cfg.MEMERY_FILE, "r", encoding="utf-8") as f:
                memory_data = json.load(f)
        except Exception as e:
            log.error(f"❌ 读取记忆失败：{str(e)}")
            return "暂无历史拒绝偏好，按基础要求筛选即可"

    # 3. 提炼核心拒绝偏好
    try:
        reject_jobs = memory_data.get("human_reject_jobs", [])
        if not reject_jobs:
            return "暂无历史拒绝偏好，按基础要求筛选即可"

        # 提取拒绝原因关键词（去重+精简）
        reject_reasons = []
        for job in reject_jobs:
            reason = job.get("human_reject_reason", "").strip()
            if reason and reason not in reject_reasons:
                reject_reasons.append(reason)

        # 生成精简的偏好文本（控制在100字内，省Token）
        memory_prompt = "【历史拒绝偏好】\n"
        if reject_reasons:
            memory_prompt += f"- 拒绝理由：{'; '.join(reject_reasons[:5])}（仅展示前5条）\n"
        memory_prompt += "- 筛选新岗位时，规避以上类型的岗位"

        log.info(f"✅ 成功提炼记忆偏好（共{len(reject_jobs)}条历史拒绝案例）")
        return memory_prompt

    except Exception as e:
        log.error(f"❌ 解析记忆偏好失败：{str(e)}")
        return "读取历史偏好失败，按基础要求筛选即可"
    
    # ====================== 1. 初始化记忆文件 ======================
def init_job_memory():
    if not os.path.exists(cfg.MEMERY_FILE):
        default_memory = {
            "human_reject_jobs": [],  # 你标记不合适、存入记忆的岗位
            "update_time": time.strftime("%Y-%m-%d %H:%M:%S")
        }
        with open(cfg.MEMERY_FILE, "w", encoding="utf-8") as f:
            json.dump(default_memory, f, ensure_ascii=False, indent=2)


# ====================== .扫描CSV，存【有不合适理由】的岗位到场景以及 ======================
def update_scene_memory():
    """
    遍历你的CSV表格
    仅L列「不合适理由」不为空的岗位，且未在记忆中存在的岗位，才写入Agent长期记忆
    双重去重：1. 记忆库已存在的链接 2. CSV内重复的链接
    多次执行不会重复存储
    """
    init_job_memory()

    # 第一步：读取现有记忆，提取所有已存的岗位链接（去重依据）
    with open(cfg.MEMERY_FILE, "r", encoding="utf-8") as f:
        memory_data = json.load(f)
    # 提取记忆中所有岗位链接，用于快速判断是否重复
    memory_job_urls = {item["job_url"] for item in memory_data["human_reject_jobs"]}
    # 新增：记录本次遍历中已处理的链接，避免CSV内重复行
    processed_urls_in_csv = set()

    # 第二步：读取CSV所有行
    new_add_count = 0  # 统计本次新增的记忆条数
    # with open(cfg.CSV_FILE, "r", encoding="utf-8-sig") as csvfile:
    with open(cfg.CSV_FILE, "r") as csvfile:
        # 兼容不同分隔符（优先逗号，若为空则尝试分号）
        reader = csv.DictReader(csvfile)
        for row_idx, row in enumerate(reader, 1):
            # 加类型判断，避免row是字符串
            if not isinstance(row, dict):
                print(f"⚠️ 第{row_idx}行：跳过非字典行: {row}")
                continue
            # 关键字段取值（兼容列名可能的空格）
            job_time = row.get("时间", row.get(" 时间", ""))
            platform = row.get("平台", row.get(" 平台", ""))
            company = row.get("公司", row.get(" 公司", ""))
            job_title = row.get("岗位", row.get(" 岗位", ""))
            salary = row.get("薪资", row.get(" 薪资", ""))
            city = row.get("地点", row.get(" 地点", ""))
            job_url = row.get("链接", row.get(" 链接", "")).strip()
            ai_score = row.get("AI匹配", row.get(" AI匹配", ""))
            ai_reason = row.get("AI理由", row.get(" AI理由", ""))
            human_reject_reason = row.get("不合适理由", row.get(" 不合适理由", "")).strip()

            # ========== 核心过滤规则 ==========
            # 1. 无不合适理由 → 跳过
            if not human_reject_reason:
                continue
            # 2. 无岗位链接（无法去重）→ 跳过
            if not job_url:
                print(f"⚠️ 第{row_idx}行：{company}-{job_title} 无岗位链接，跳过")
                continue
            # 3. 记忆库已存在 → 跳过
            if job_url in memory_job_urls:
                print(f"ℹ️ 第{row_idx}行：{company}-{job_title} 已在记忆中，跳过")
                continue
            # 4. 本次遍历中已处理过（CSV内重复行）→ 跳过
            if job_url in processed_urls_in_csv:
                print(f"ℹ️ 第{row_idx}行：{company}-{job_title} CSV内重复，跳过")
                continue

            # ========== 符合条件：写入记忆 ==========
            job_record = {
                "record_time": job_time,
                "platform": platform,
                "company": company,
                "job_title": job_title,
                "salary": salary,
                "city": city,
                "job_url": job_url,
                "ai_opinion": {
                    "匹配度": ai_score,
                    "AI判断理由": ai_reason
                },
                "human_reject_reason": human_reject_reason,
                "save_to_memory_time": time.strftime("%Y-%m-%d %H:%M:%S")
            }

            # 追加到记忆库
            memory_data["human_reject_jobs"].append(job_record)
            # 更新去重集合
            memory_job_urls.add(job_url)
            processed_urls_in_csv.add(job_url)
            # 统计新增条数
            new_add_count += 1
            print(f"✅ 第{row_idx}行：已存入记忆：{company} - {job_title} | 拒绝原因：{human_reject_reason}")

    # 第三步：更新保存记忆文件（只有新增数据时才更新）
    if new_add_count > 0:
        memory_data["update_time"] = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(cfg.MEMERY_FILE, "w", encoding="utf-8") as f:
            json.dump(memory_data, f, ensure_ascii=False, indent=2)

    # 最终统计
    total_memory_count = len(memory_data["human_reject_jobs"])
    print(f"\n📊 同步完成 | 本次新增 {new_add_count} 条记忆 | 记忆库总计 {total_memory_count} 条人工拒绝案例")