# -*- coding:utf-8 -*-
# @CreatTime : 2026 10:14
# @Author : XZN
# 岗位硬过滤工具
import re
import logging
import config as cfg

log = logging.getLogger(__name__)

def parse_salary_range(salary_text):
    """
    解析薪资上下限（适配所有格式）
    :param salary_text: 薪资文本（如"15-20K"、"30-60K·14薪"、"20K+"、"面议"）
    :return: (min_sal, max_sal) 元组，面议返回(0, 999)
    """
    # 保留面议：返回(0, 999)（确保面议能通过薪资筛选）
    if "面议" in salary_text.strip():
        return (0, 999)
    # 提取所有数字（兼容 15-20K、20K+、30-60K·14薪 等格式）
    nums = re.findall(r'\d+', salary_text)
    if not nums:
        return (0, 0)  # 无数字的无效薪资

    min_sal = int(nums[0])  # 薪资下限（如15-20K → 15）
    # 薪资上限：有第二个数字则取，无则取下限（如20K+ → 20）
    max_sal = int(nums[1]) if len(nums) >= 2 else min_sal
    return (min_sal, max_sal)

def hard_filter(title, area, salary):
    """
    硬过滤逻辑：薪资+地点+远程关键词筛选
    :return: True（通过）/False（拒绝）
    """
    title = title.lower()
    area = area.lower()
    salary = salary.lower()

    # ====== 规则1：薪资筛选 ======
    sal_min, sal_max = parse_salary_range(salary)
    if "面议" not in salary:
        if sal_max < cfg.MIN_SALARY or sal_min > cfg.MAX_SALARY:
            log.info(
                f"⚠️ 薪资筛选排除：{salary}（最高{sal_max}K < 要求{cfg.MIN_SALARY}K 或 最低{sal_min}K > 要求{cfg.MAX_SALARY}K）")
            return False

    # ====== 规则2：地点+远程关键词筛选 ======
    in_preferred_city = any(c.lower() in area for c in cfg.PREFERRED_CITIES)

    if in_preferred_city:
        return True
    else:
        has_remote_keyword = any(
            w.lower() in title or w.lower() in area
            for w in cfg.REMOTE_KEYWORDS
        )
        # 新增：工作地筛选日志
        if has_remote_keyword:
            log.info(f"📍 工作地筛选：地点={area}（非首选城市），但含远程关键词 → 保留该岗位")
        else:
            log.info(f"📍 工作地筛选：地点={area}（非首选城市），且无远程关键词 → 排除该岗位")

        return has_remote_keyword

def check_chatted(chat_text):
    """检查是否需要继续聊天"""
    if "继续聊" in chat_text:
        return False
    else:
        return True