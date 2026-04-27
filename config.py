# -*- coding:utf-8 -*-
# @CreatTime : 2026 10:14
# @Author : XZN
import os
from pathlib import Path

_CONFIG_DIR = Path(__file__).resolve().parent
try:
    from dotenv import load_dotenv

    load_dotenv(_CONFIG_DIR / ".env")
except ImportError:
    pass


def _env_str(name: str, default: str = "") -> str:
    raw = os.getenv(name)
    if raw is None:
        return default
    s = str(raw).strip()
    return s if s else default


def _env_int(name: str, default: int) -> int:
    raw = _env_str(name, "")
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


# ====================== 服务配置 ======================
HOST = "0.0.0.0"
PORT = 8000
DEBUG = True

# ========================== 筛选 / 动态配置（敏感项来自 .env，见 .env.example）==========================
LOGIN_USERNAME = _env_str("LOGIN_USERNAME")
LOGIN_PASSWORD = _env_str("LOGIN_PASSWORD")
# 猎聘 Playwright storage_state（由 scripts/liepin_login_save_state.py 写入；爬虫可加载复用登录态）
_LIEPIN_BROWSER_DATA = _CONFIG_DIR / "browser_data"
LIEPIN_STORAGE_STATE_PATH = str(_LIEPIN_BROWSER_DATA / "liepin_storage_state.json")
# 2Captcha（https://2captcha.com）API Key；猎聘登录：TencentTaskProxyless / CoordinatesTask
captcha_api_key = _env_str("captcha_api_key")
# 腾讯云验证码 appId（与 tcaptcha iframe URL 中 aid= 一致，如猎聘常见 2016659673）；留空则从 #tcaptcha_iframe[src] 解析
TENCENT_CAPTCHA_APP_ID = ""
# 1. 大模型 API
LLM_MODEL = _env_str("LLM_MODEL", "Tongyi")
DASHSCOPE_API_KEY = _env_str("DASHSCOPE_API_KEY")
openai_API_KEY = _env_str("openai_API_KEY")
# AI 生成
# 生成目的：与 services.job_store 中 OpenAI Embeddings 一致；更换模型后需清空 faiss_sqlite_data 或删库以免维度不一致
OPENAI_EMBEDDING_MODEL = _env_str("OPENAI_EMBEDDING_MODEL", "text-embedding-3-small")
# AI 生成
# 生成目的：兼容网关的 base_url，须含 /v1，与提供商示例一致：
#   client = OpenAI(api_key="本平台key", base_url="https://ai.nengyongai.cn/v1")
# 留空字符串则走官方 https://api.openai.com/v1
OPENAI_API_BASE = _env_str("OPENAI_API_BASE", "https://ai.nengyongai.cn/v1")

# ========================== 人工复查记忆 输出 ==========================
_SCENE_DIR = _CONFIG_DIR / "data"
_SCENE_DIR.mkdir(parents=True, exist_ok=True)
HISTORY_SCENE_PATH = str(_SCENE_DIR / "SCENE.json")
SCENE_MAX_NUMBER = 3  # 最大场景数量
# ========================== 搜索页面数,测试时控制爬虫量 ==========================
MAX_PAGE = _env_int("MAX_PAGE", 1)
# 猎聘爬虫：True=无头后台运行（不显示浏览器窗口），False=显示窗口便于登录态与调试
CRAWL_HEADLESS = False
# ====================== 日志配置（全局唯一） ======================
import logging



# 日志文件路径
_LOG_DIR = _CONFIG_DIR / "log"
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

_DEFAULT_JOBCONFIG_PATH = _CONFIG_DIR / "data" / "default_jobconfig.json"
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
