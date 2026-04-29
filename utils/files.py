# -*- coding:utf-8 -*-
# @CreatTime : 2026 19:14
# @Author : XZN
# CSV文件读写工具
import os
import csv
import logging
from datetime import datetime
from typing import Optional, Union, Any, Dict
import config as cfg

log = logging.getLogger(__name__)

# 获取utils目录的上级目录
parent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

def read_and_clean_txt(file_path: str) -> str:
    '''
    TXT读取 + 清洗函数
    :param file_path:
    :return:
    '''
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            content = f.read()

        # 简单清洗：空行、多余空格、换行
        content = content.strip()
        content = content.replace("\n\n", "\n")
        content = content.replace("  ", " ")
        return content
    except Exception as e :
        log.error(f'读入{file_path}失败，原因：{e}')
        return ""
    
# AI 生成
# 生成目的：与 write_to_csv 新表头（含 HR招呼语）一致；老文件缺列时一次性平铺补齐
_CSV_HEADER_FULL = [
    "时间",
    "平台",
    "公司",
    "岗位",
    "薪资",
    "地点",
    "链接",
    "AI匹配度",
    "AI理由",
    "是否投递",
    "HR招呼语",
]


def _migrate_csv_hr_greeting_row(csv_file_path: str) -> None:
    if not os.path.exists(csv_file_path):
        return
    with open(csv_file_path, "r", newline="", encoding="utf-8-sig") as f:
        rows = list(csv.reader(f))
    if not rows or _CSV_HEADER_FULL[-1] in (rows[0] or []):
        return
    h_old = rows[0]
    n_new = len(_CSV_HEADER_FULL)
    for i in range(1, len(rows)):
        r = list(rows[i])
        if len(r) < n_new:
            r.extend([""] * (n_new - len(r)))
        rows[i] = r[:n_new]
    rows[0] = list(_CSV_HEADER_FULL)
    with open(csv_file_path, "w", newline="", encoding="utf-8-sig") as f:
        csv.writer(f).writerows(rows)
    log.info("已迁移 CSV 表头并补齐「HR招呼语」列: %s", csv_file_path)


def write_to_csv(
    job,
    ai_result: Union[str, Dict[str, Any]],
    scene_id: Optional[int] = None,
    platform: str = "liepin",
):
    """将岗位信息、AI筛选结果、可选 HR 招呼语写入 CSV，并回写 list_jobs.hr_greeting
    :param job: 岗位 dict
    :param ai_result: 筛选三行 str，或 { ai_result, hr_greeting } dict
    """
    match_score = ""
    reason = ""
    ap = ""
    hr = ""

    if isinstance(ai_result, dict):
        # 新版：结构化列（推荐）
        if any(k in ai_result for k in ("match_level", "reason", "apply")):
            match_score = str(ai_result.get("match_level", "") or "").strip()
            reason = str(ai_result.get("reason", "") or "").strip()
            ap = str(ai_result.get("apply", "") or "").strip()
            hr = str(ai_result.get("hr_greeting", "") or "")
        # 旧版：三行文本（兼容）
        else:
            at = str(ai_result.get("ai_result", "") or "")
            hr = str(ai_result.get("hr_greeting", "") or "")
            lines = [line.strip() for line in at.strip().split("\n") if line.strip()]
            match_score = lines[0].replace("【匹配度】", "") if len(lines) > 0 else ""
            reason = lines[1].replace("【理由】", "") if len(lines) > 1 else ""
            ap = lines[2].replace("【是否投递】", "") if len(lines) > 2 else ""
    else:
        # 纯文本三行（兼容）
        at = str(ai_result)
        lines = [line.strip() for line in at.strip().split("\n") if line.strip()]
        match_score = lines[0].replace("【匹配度】", "") if len(lines) > 0 else ""
        reason = lines[1].replace("【理由】", "") if len(lines) > 1 else ""
        ap = lines[2].replace("【是否投递】", "") if len(lines) > 2 else ""
    csv_file_name = getattr(cfg, "CSV_FILE", "ai_job_matches.csv")
    csv_file_path = os.path.join(parent_dir, csv_file_name)
    file_exists = os.path.exists(csv_file_path)
    if file_exists:
        _migrate_csv_hr_greeting_row(csv_file_path)

    with open(csv_file_path, "a", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(_CSV_HEADER_FULL)
        if file_exists and not os.path.getsize(csv_file_path):
            writer.writerow(_CSV_HEADER_FULL)

        writer.writerow(
            [
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                job["平台"],
                job["公司"],
                job["标题"],
                job["薪资"],
                job["地点"],
                job["链接"],
                match_score,
                reason,
                ap,
                hr,
            ]
        )
    if scene_id is not None and job.get("链接"):
        # 与 CSV 同步：按 scene+url 回写到列表库
        from services.job_store import update_crawl_list_llm_fields

        update_crawl_list_llm_fields(
            platform,
            int(scene_id),
            str(job.get("链接", "")),
            match_level=match_score,
            reason=reason,
            apply=ap,
            hr_greeting=hr,
        )