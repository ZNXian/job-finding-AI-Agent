# -*- coding:utf-8 -*-
# @CreatTime : 2026 10:14
# @Author : XZN
from pathlib import Path
# ====================== 服务配置 ======================
HOST = "0.0.0.0"
PORT = 8000
DEBUG = True

# ========================== 筛选 / 动态配置 ==========================
LOGIN_USERNAME = "18142878012"
LOGIN_PASSWORD = "Xzn172801!"
# 猎聘 Playwright storage_state（由 scripts/liepin_login_save_state.py 写入；爬虫可加载复用登录态）
_LIEPIN_BROWSER_DATA = Path(__file__).resolve().parent / "browser_data"
LIEPIN_STORAGE_STATE_PATH = str(_LIEPIN_BROWSER_DATA / "liepin_storage_state.json")
# 2Captcha（https://2captcha.com）API Key；猎聘登录：TencentTaskProxyless / CoordinatesTask
captcha_api_key = "34a5e5965e6e0960ea06863a8fea3938"
# 腾讯云验证码 appId（与 tcaptcha iframe URL 中 aid= 一致，如猎聘常见 2016659673）；留空则从 #tcaptcha_iframe[src] 解析
TENCENT_CAPTCHA_APP_ID = ""
# 1. 大模型 API
LLM_MODEL = "Tongyi"
DASHSCOPE_API_KEY = "sk-5e0e9d574ba44813a4624fa21563d535" #写入你的QWEN API_KEY
openai_API_KEY = "sk-cs73xq1HM0HkQnHNtjInLfs3UgQNWpCT0FCkrrZLJiegRGPz"  # 写入你的 OpenAI API Key（FAISS+SQLite 拒绝理由向量用）
# AI 生成
# 生成目的：与 services.job_store 中 OpenAI Embeddings 一致；更换模型后需清空 faiss_sqlite_data 或删库以免维度不一致
OPENAI_EMBEDDING_MODEL = "text-embedding-3-small"
# AI 生成
# 生成目的：兼容网关的 base_url，须含 /v1，与提供商示例一致：
#   client = OpenAI(api_key="本平台key", base_url="https://ai.nengyongai.cn/v1")
# 留空字符串则走官方 https://api.openai.com/v1
OPENAI_API_BASE = "https://ai.nengyongai.cn/v1"

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
