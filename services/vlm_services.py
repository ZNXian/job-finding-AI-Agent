# -*- coding:utf-8 -*-
# @CreatTime : 2026 10:14
# @Author : XZN
# LLM模型初始化和提示词模板
import os

from langchain_community.llms import Tongyi
from langchain_core.prompts import PromptTemplate
from config import DASHSCOPE_API_KEY
import config as cfg
from config import log

# ====================== 模型初始化 ======================
os.environ["DASHSCOPE_API_KEY"] = DASHSCOPE_API_KEY
llm = Tongyi(model="qwen-turbo", temperature=0.1)

# ====================== AI 提示词构造 ======================
#LLM求职筛选助手Prompt
filter_prompt = PromptTemplate(
    input_variables=["job_info", "my_requirement"],
    template="""
你是专业求职筛选助手，严格按条件判断是否匹配。

我的要求：
{my_requirement}

岗位信息：
{job_info}

输出固定3行：
【匹配度】高/中/低
【理由】简述
【是否投递】是/否
"""
)
filter_chain = filter_prompt | llm
# ----------------------
#LLM场景智能匹配Prompt
scene_prompt = PromptTemplate(
        input_variables=["user_text", "scene_list"],
        template="""
    你是求职场景智能匹配助手。

    已有场景：
    {scene_list}

    用户需求与简历：
    {user_text}

    请判断是否匹配已有场景。
    如果匹配，返回场景编号，例如：2
    如果不匹配，返回 new

    只返回结果，不要多余文字。
    """
    )
scene_chain = scene_prompt | llm
# ----------------------
#LLM提炼求职需求标准化信息Prompt
standard_prompt = PromptTemplate(
        input_variables=["user_text"],
        template="""
    从用户求职需求中提取标准化信息，严格返回JSON格式,其中薪资单位是K：
    {{
        "search_keywords": ["关键词1","关键词2","关键词3"],
        "city": "期望城市",
        "accept_remote": true/false,
        "min_salary": 数字,
        "max_salary": 数字,
        "requirements": ["需求1","需求2",...最多8条]
    }}

    用户需求：
    {user_text}
    """
    )
standard_chain = standard_prompt | llm
# ====================== 处理岗位并返回AI建议======================
def llm_process_job(job):
    """
    接收一个岗位 dict → 调用 LLM → 返回带AI结果的岗位
    """

    log.info(f"🧠 AI 正在处理：{job['平台']} | {job['标题']} | {job['薪资']}")

    # ========= LLM 处理岗位匹配程度 =========
    # ai_result = "匹配度：高\n理由：符合\n是否投递：是"
    # 把AI结果塞回岗位里
    # return job
    log.info("=" * 70)
    log.info(f"{job['平台']} | {job['标题']} | {job['薪资']}")

    job_info = f"岗位：{job['标题']}\n公司：{job['公司']}\n薪资：{job['薪资']}\n地点：{job['地点']}\n岗位介绍：{job['介绍']}"
    ai_res = filter_chain.invoke({
        "job_info": job_info,
        "my_requirement": cfg.MY_REQUIREMENT
    })
    return ai_res

# ====================== 判断自然语言求职场景 + AI 结果返回======================
def llm_identify_scene(user_text , scenes):
    '''
    输入历史场景文本，判断是否匹配原本场景
    :param user_text: Str
    :param scenes: List[Dict]
    :return: is_new(TRUE / FALSE), scene_id(isdigit)
    '''

    #如果场景是空的，跳过匹配场景，进行新建场景
    if len(scenes) > 0:
        # 构造历史场景文本
        scene_list = "\n".join([
            f"场景{s['scene_id']}：关键词={s['search_keywords']}, 城市={s['city']}, 远程={s['accept_remote']}, 薪资={s['min_salary']}-{s['max_salary']}"
            for s in scenes
        ])
        # ----------------------
        match_result = scene_chain.invoke({
            "user_text": user_text,
            "scene_list": scene_list
        }).strip()
        if match_result.isdigit():
            scene_id = int(match_result)
            return  False,scene_id
    #其他情况，新建场景
    standard_result = standard_chain.invoke({"user_text": user_text})
    return True ,standard_result


