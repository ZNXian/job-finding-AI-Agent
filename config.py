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


# AI 生成
# 生成目的：猎聘 VLM 开关、模型名等从 .env 读布尔/字符串
def _env_bool(name: str, default: bool = False) -> bool:
    raw = _env_str(name, "")
    if not raw:
        return default
    return str(raw).strip().lower() in ("1", "true", "yes", "on", "y")


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
# 1. 大模型 API（文本走百炼 OpenAI 兼容 chat.completions，见 LLM_CHAT_MODEL）
LLM_MODEL = _env_str("LLM_MODEL", "dashscope-chat")
DASHSCOPE_API_KEY = _env_str("DASHSCOPE_API_KEY")
# 文本 chat 模型，官方示例如 qwen-flash；可按控制台实际 model id 修改（如 qwen3.x）
LLM_CHAT_MODEL = _env_str("LLM_CHAT_MODEL", "qwen-flash")
LLM_REQUEST_TIMEOUT_MS = _env_int("LLM_REQUEST_TIMEOUT_MS", 120_000)
# chat.completions 的 max_tokens（结构化/长 JSON 时可调大）
LLM_MAX_TOKENS = _env_int("LLM_MAX_TOKENS", 6000)
# AI 生成
# 生成目的：与 API Key 所属地域一致，华北 2（北京）默认 compatible-mode；VLM / 文本 LLM 共用 dashscope OpenAI 兼容入口
DASHSCOPE_BASE_HTTP_API_URL = _env_str(
    "DASHSCOPE_BASE_HTTP_API_URL",
    "https://dashscope.aliyuncs.com/compatible-mode/v1",
)
openai_API_KEY = _env_str("openai_API_KEY")
# AI 生成
# 生成目的：与 services.job_store 中 OpenAI Embeddings 一致；更换模型后需清空 faiss_sqlite_data 或删库以免维度不一致
OPENAI_EMBEDDING_MODEL = _env_str("OPENAI_EMBEDDING_MODEL", "text-embedding-3-small")
# AI 生成
# 生成目的：兼容网关的 base_url，须含 /v1，与提供商示例一致：
#   client = OpenAI(api_key="本平台key", base_url="https://ai.nengyongai.cn/v1")
# 留空字符串则走官方 https://api.openai.com/v1
OPENAI_API_BASE = _env_str("OPENAI_API_BASE", "https://ai.nengyongai.cn/v1")
# AI 生成
# 生成目的：猎聘详情使用 Qwen-VL 截图+视觉解析（与 HTML 结构化为同一五字段，见 crawlers.liepin_vlm）
VLM_ENABLED = _env_bool("VLM_ENABLED", False)
# AI 生成
# 生成目的：百炼 VL OCR 等视觉模型 id，官方示例：qwen-vl-ocr-latest
VLM_MODEL = _env_str("VLM_MODEL", "qwen-vl-ocr-latest")
# AI 生成
# 生成目的：与官方示例一致，控制输入图像缩放像素阈（可选）
VLM_IMAGE_MIN_PIXELS = _env_int("VLM_IMAGE_MIN_PIXELS", 32 * 32 * 3)
VLM_IMAGE_MAX_PIXELS = _env_int("VLM_IMAGE_MAX_PIXELS", 32 * 32 * 8192)
# AI 生成
# 生成目的：VLM 单次请求超时（毫秒）
VLM_REQUEST_TIMEOUT_MS = _env_int("VLM_REQUEST_TIMEOUT_MS", 120_000)

# ========================== 人工复查记忆 输出 ==========================
_SCENE_DIR = _CONFIG_DIR / "data"
_SCENE_DIR.mkdir(parents=True, exist_ok=True)
HISTORY_SCENE_PATH = str(_SCENE_DIR / "SCENE.json")
SCENE_MAX_NUMBER = 3  # 最大场景数量
REMOTE_KEYWORDS = ["远程", "居家", "灵活", "异地"]
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
    level=logging.DEBUG,
    format="%(asctime)s → %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler()
    ]
)
# 全局共用的 log 对象
log = logging.getLogger(__name__)

# AI 生成
# 生成目的：通义/百炼 Python SDK 全局 base，与 DASHSCOPE_BASE_HTTP_API_URL 及 Key 地域一致
try:
    import dashscope  # noqa: E402

    if (DASHSCOPE_BASE_HTTP_API_URL or "").strip():
        dashscope.base_http_api_url = (DASHSCOPE_BASE_HTTP_API_URL or "").strip()
except Exception as e:
    log.debug("config: dashscope base_http_api_url 未应用: %s", e)

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
