# -*- coding:utf-8 -*-
# @CreatTime : 2026 10:14
# @Author : XZN
from pathlib import Path
# ====================== 服务配置 ======================
HOST = "0.0.0.0"
PORT = 8000
DEBUG = True

# ========================== 筛选 / 动态配置 ==========================
# 1. 大模型 API
LLM_MODEL = "Tongyi"
DASHSCOPE_API_KEY = "" #写入你的QWEN API_KEY


# ========================== 人工复查记忆 输出 ==========================
_SCENE_DIR = Path(__file__).resolve().parent / "data"
_SCENE_DIR.mkdir(parents=True, exist_ok=True)
HISTORY_SCENE_PATH = str(_SCENE_DIR / "SCENE.json")
SCENE_MAX_NUMBER=3 #最大场景数量
# ========================== 搜索页面数,测试时控制爬虫量 ==========================
MAX_PAGE = 1
# 猎聘爬虫：True=无头后台运行（不显示浏览器窗口），False=显示窗口便于登录态与调试
CRAWL_HEADLESS = False
# ====================== 日志配置（全局唯一） ======================
import logging



# 日志文件路径
_LOG_DIR = Path(__file__).resolve().parent / "log"
_LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = str(_LOG_DIR / "jobs_agent.log")

# 配置格式
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s → %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler()
    ]
)
# 全局共用的 log 对象
log = logging.getLogger(__name__)

# ========================== 工作筛选信息动态配置，本段不要修改 ==========================
from contextvars import ContextVar
import json
from pathlib import Path

_DEFAULT_JOBCONFIG_PATH = Path(__file__).resolve().parent / "data" / "default_jobconfig.json"
try:
    _DEFAULT_JOBCONFIG = json.loads(_DEFAULT_JOBCONFIG_PATH.read_text(encoding="utf-8"))
except Exception:
    _DEFAULT_JOBCONFIG = {}

dynamic_jobconfig = ContextVar("dynamic_config", default=_DEFAULT_JOBCONFIG)
def __getattr__(name):
    config = dynamic_jobconfig.get()
    if config and name in config:
        return config[name]
    raise AttributeError(f"Config {name} not found")
