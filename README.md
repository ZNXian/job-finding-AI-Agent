# job-finding-AI-Agent
一个基于 FastAPI 和通义千问 的多场景求职筛选与记忆助手，帮你高效管理不同方向的求职偏好，自动过滤岗位。

# 当前已实现功能
1. 多场景求职配置

支持通过本地文件（`txt` / `md` / 简历 `pdf` 或图片 `png` 等，见 `services/resume_document_ingest`）中的自然语言描述，经 LLM 结构化决策**复用或新建**求职场景；接口为 `POST /api/start_from_txt`（Body 传服务器路径）或 `POST /api/start_from_upload`（multipart，单文件上限见 `config.MAX_SCENE_UPLOAD_BYTES`）。

每个场景拥有独立的配置：关键词、目标城市、薪资区间、个人要求；可选 **LangGraph 一键流水线** `POST /api/agent/run`：可选 `user_file_path` 先做场景准备，再依次调用登录 → 爬取（仅 SQLite）→ 标题初筛 → 详情精筛（内部 HTTP 调本服务，默认基址 `config.AGENT_API_BASE_URL`，与 `PORT` 对齐；超时见 `AGENT_TIMEOUT_*`）。

目前只支持猎聘；后续可扩展更多招聘平台。

2. 岗位筛选与记忆

基于求职场景配置的关键词、城市、薪资等规则，自动过滤不符合要求的岗位；支持爬取只写库、标题初筛、详情精筛与 CSV 输出（见 `api/crawl.py` 等）。

每个求职场景拥有独立的记忆库，记录偏好 / 拒绝原因等；`POST /api/feedback` 可在人工标注后更新记忆。支持后续接入 RAG 做偏好学习。


 

# windows 本地运行安装环境 

在项目根目录执行（路径换成自己的克隆目录）：

Windows（PowerShell）：

cd D:\path\to\job-finding-AI-Agent

python -m venv venv

.\venv\Scripts\Activate.ps1

python -m pip install --upgrade pip

pip install -r requirements.txt

python -m playwright install chromium

# Linux / macOS： 本地运行安装环境 

cd /path/to/job-finding-AI-Agent

python3 -m venv venv

source venv/bin/activate

python -m pip install --upgrade pip

pip install -r requirements.txt

python -m playwright install chromium

# 进行配置（API Key 等）

1. 在项目根目录复制环境变量模板为 `.env`（`.env` 已被 `.gitignore` 忽略，勿提交仓库；模板见 `.env.example`）：

PowerShell：`Copy-Item .env.example .env`  
bash：`cp .env.example .env`

2. 编辑 `.env`。应用启动时由 `config.py` 通过 `python-dotenv` 加载项目根目录下的 `.env`。与 `.env.example` 中变量对应关系如下（详见模板内注释）：

| 变量 | 说明 |
|------|------|
| `DASHSCOPE_API_KEY` | 通义千问 / 百炼 API Key（文本 LLM、场景解析、初筛精筛等） |
| `LLM_CHAT_MODEL` | 百炼 OpenAI 兼容 `chat.completions` 模型 id，如 `qwen-flash` |
| `DASHSCOPE_BASE_HTTP_API_URL` | 与 Key 地域一致；默认 `https://dashscope.aliyuncs.com/compatible-mode/v1` |
| `LLM_REQUEST_TIMEOUT_MS` / `LLM_MAX_TOKENS` | 可选；文本请求超时（毫秒）、`max_tokens` |
| `LOGIN_USERNAME` / `LOGIN_PASSWORD` | 猎聘账号（按需） |
| `captcha_api_key` | 2Captcha 等（按需） |
| `VLM_ENABLED` / `VLM_MODEL` | 猎聘详情是否截图 + 多模态；如 `qwen-vl-ocr-latest` |
| `VLM_IMAGE_MIN_PIXELS` / `VLM_IMAGE_MAX_PIXELS` / `VLM_REQUEST_TIMEOUT_MS` | 可选 |
| `openai_API_KEY` / `OPENAI_API_BASE` / `OPENAI_EMBEDDING_MODEL` | 记忆向量嵌入（与 `config` 中默认网关、模型一致） |
| `MAX_PAGE` | 爬列表最大页数，测试可设为 `1` |

3. **LangGraph** `POST /api/agent/run` 通过 HTTP 回调本服务：可在 `.env` 中设置 `AGENT_API_BASE_URL`（未设置时 `config` 默认为 `http://127.0.0.1:{PORT}`，须与实际监听端口一致）。可选超时（秒）：`AGENT_TIMEOUT_LOGIN_S`、`AGENT_TIMEOUT_CRAWL_S`、`AGENT_TIMEOUT_PREFILTER_S`、`AGENT_TIMEOUT_SUBMIT_S`（见 `agent_orchestrator.py`）。

4. `HOST`、`PORT` 等写在 `config.py` 源码中（默认 `PORT=8000`）；上传单文件大小上限为 `config.MAX_SCENE_UPLOAD_BYTES`，**不从** `.env` 读取。

# 启动服务

先验证安装

python -c "import main; print('ok')"。

python main.py （端口以 config.py 里的 HOST / PORT 为准。）

uvicorn main:app --host 0.0.0.0 --port 8000

# 介绍

服务默认运行在 `http://localhost:{PORT}`（`PORT` 以 `config.py` 为准；本地浏览器常用 `http://127.0.0.1:{PORT}`）。

无头模式未经过验证,请勿过多使用,小心封号!

有头模式安全,已采取安全的反反爬手段

**HTTP 路由**在 `api/` 包内注册，`main.py` 仅创建 `FastAPI` 并挂载路由；完整路径列表与说明见 `main.py` 顶部注释（与下列一致，均为 **POST**）：

| 路径 | 说明 |
|------|------|
| `/api/liepin_login` | 猎聘浏览器登录，保存 `storage_state` |
| `/api/start_from_txt` | Body：`file_path`（服务器本地路径），场景准备（与 LangGraph 内 `prepare_scene` 同源） |
| `/api/start_from_upload` | `multipart/form-data` 上传单文件；大小上限 `config.MAX_SCENE_UPLOAD_BYTES`（代码常量，非 env） |
| `/api/crawl_liepin` | 爬取 + 可选 LLM + 写 CSV；Query：`scene_id`，`crawl_only`，`reset_checkpoint` |
| `/api/crawl_liepin_crawl_only` | 只爬取写 SQLite；Query：`scene_id`，`reset_checkpoint` |
| `/api/prefilter_titles_for_scene` | 标题初筛写回 SQLite；Query：`scene_id`，`include_*` |
| `/api/submit_llm_for_scene` | 详情精筛 pending、写 CSV；Query：`scene_id` |
| `/api/feedback` | 人工反馈后更新记忆；Query：`scene_id` |
| `/api/agent/run` | LangGraph：`scene_id` 与 `user_file_path`（Query）二选一；可选 `reset_checkpoint`、`include_*`；线程池内执行以免自调用死锁 |

文件扩展名与 VLM/PDF：`.txt` / `.md`、`.png` / `.jpg` / `.jpeg` / `.webp`（VLM 转纯文本）、`.pdf`（先抽文本，不足再渲染前若干页走 VLM）。需配置 `DASHSCOPE_API_KEY`；PDF 需 `pymupdf`（见 `requirements.txt`）。**方案 B**：首次可将「求职期望 + 简历」放在同一文件做场景匹配；之后若只改 `data/resume/{scene_id}.txt`，不会自动改写 `SCENE.json`，除非再次调用场景匹配类接口。

使用示例:
1./api/liepin_login 猎聘浏览器登录接口

curl -X POST "http://127.0.0.1:8000/api/liepin_login"

会弹出浏览器，此时请在120秒内登录猎聘，登录好直接关闭标签页或者浏览器就行。

登录以后可以看到的岗位数更多，并且可以识别该岗位您之前有没有聊过（如果已经聊过就不记录这个岗位了）。

2./api/start_from_txt 自然语言匹配求职场景 

把您的简历和求职期望写到本地文件（如 `D:/test_scene1.txt`，或 `.pdf` / 简历截图 `.png`），然后执行：

curl -X POST "http://localhost:8000/api/start_from_txt" -H "Content-Type: application/json" -d "{\"file_path\": \"D:/test_scene1.txt\"}"

上传方式示例（PowerShell，路径换成你的文件）：

curl -X POST "http://127.0.0.1:8000/api/start_from_upload" -F "file=@D:/test_scene1.txt"

接口会返回一个 `scene_id`

3./api/crawl_liepin 根据求职场景爬猎聘网岗位信息，结合AI输出岗位匹配度和AI建议是否投递（附带岗位链接）

把您想查找工作的scene_id填入{您的scene_id}

curl -X POST "http://127.0.0.1:8000/api/crawl_liepin?scene_id={您的scene_id}"

浏览器会自动打开进行操作，不要关闭，操作完以后会自动关闭。

如果手动关闭，会处理当前已获取到的岗位信息。

接口会返回一个csv文件，这是已经过AI判断的岗位信息。一般在data目录下，后缀是scene_id编号

3a./api/crawl_liepin_crawl_only 只爬取并写入 SQLite（不调 LLM、不写 CSV），适合与初筛 / 精筛拆步执行：

curl -X POST "http://127.0.0.1:8000/api/crawl_liepin_crawl_only?scene_id={您的scene_id}"

可选从第 1 页重爬：`...?scene_id=1&reset_checkpoint=true`

3b./api/prefilter_titles_for_scene 标题初筛，写回 SQLite（reject → 低/否；其余 → pending）：

curl -X POST "http://127.0.0.1:8000/api/prefilter_titles_for_scene?scene_id={您的scene_id}"

初筛时带上公司 / 地点 / 薪资字段（默认不带）：`...&include_company=true&include_location=true&include_salary=true`

3c./api/submit_llm_for_scene 对 `match_level=pending` 的岗位做详情精筛并写 CSV：

curl -X POST "http://127.0.0.1:8000/api/submit_llm_for_scene?scene_id={您的scene_id}"

4./api/agent/run LangGraph 一键流水线（**须先启动本服务**；`AGENT_API_BASE_URL` 指向当前端口）。已有 `scene_id`、从登录起跑：

curl -X POST "http://127.0.0.1:8000/api/agent/run?scene_id={您的scene_id}"

或先按本地路径做场景准备再跑全流程（路径含中文或空格时请自行 URL 编码）：

curl -X POST "http://127.0.0.1:8000/api/agent/run?user_file_path=D:/test_scene1.txt"

可选：`reset_checkpoint=true`，以及初筛用的 `include_company` / `include_location` / `include_salary`（与上表一致）。

5./api/feedback 人工查看岗位并且对不适合的岗位说明理由以后，调用一次来更新记忆

打开上一个接口返回的csv文件，可以人工投递这些岗位。

在最右边新建一列"不合适理由"，用来记录“不合适，也不投递”的岗位原因。下次再检索到这条岗位会直接略过，AI也能学习您不喜欢这个岗位的原因。

操作完成以后执行：curl -X POST "http://127.0.0.1:8000/api/feedback?scene_id={您的scene_id}"

会自动刷新记忆。完成以后原本的csv文件可以删掉，留着也行，不影响。留着的话下一次会追加写入。

# 下个版本优化内容

1.增加通义 Qwen-VL进行VLM 视觉解析

2.提示词升级：现在对自然语言的分析还有点笨，应该是我提示词没写好

3.RAG 智能偏好学习：当前记忆架构较基础而且有点蠢，后面会升级

4.更多招聘平台支持

5.目前整个流程不是很流畅，下次会优化得丝滑一点



