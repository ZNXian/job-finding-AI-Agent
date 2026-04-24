# -*- coding:utf-8 -*-
# @CreatTime : 2026 19:14
# @Author : XZN
# CSV文件读写工具
import os
import csv
import logging
from datetime import datetime
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
    
def write_to_csv(job, ai_result):
    """将岗位信息和AI筛选结果写入CSV
    :param job: DICT
    :param ai_result: STR
    :return:
    """
    csv_file_name = getattr(cfg, "CSV_FILE", "ai_job_matches.csv")
    csv_file_path = os.path.join(parent_dir, csv_file_name)
    file_exists = os.path.exists(csv_file_path)

    with open(csv_file_path, "a", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        # 写入表头（文件不存在时）
        if not file_exists:
            writer.writerow([
                "时间", "平台", "公司", "岗位", "薪资", "地点", "链接",
                "AI匹配度", "AI理由", "是否投递"
            ])

        # 解析AI结果
        lines = [line.strip() for line in ai_result.strip().split("\n") if line.strip()]
        match_score = lines[0].replace("【匹配度】", "") if len(lines) > 0 else ""
        reason = lines[1].replace("【理由】", "") if len(lines) > 1 else ""
        apply = lines[2].replace("【是否投递】", "") if len(lines) > 2 else ""

        # 写入数据行
        writer.writerow([
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            job["平台"], job["公司"], job["标题"], job["薪资"], job["地点"], job["链接"],
            match_score, reason, apply
        ])