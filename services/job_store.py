# -*- coding: utf-8 -*-

# AI 生成
# 生成目的：SQLite 存岗位元数据，FAISS 做拒绝理由向量检索；纯 Python + faiss-cpu，避免 Windows 下部分向量库原生崩溃
# 向量：OpenAI 兼容 Embeddings（config.openai_API_KEY / OPENAI_EMBEDDING_MODEL / OPENAI_API_BASE）
#
# 历史说明（为何不用 Chroma）：
# 本模块曾基于 ChromaDB（PersistentClient + OpenAI 远程 Embedding）实现 pending / memory 与拒绝理由相似检索。
# 在 Windows 上多次出现：Embeddings 请求已 HTTP 200 返回后，在 collection.add / pending 写入阶段进程直接退出，
# 退出码约 -1073741819（0xC0000005，访问冲突）；曾尝试更换 chromadb 版本、换本地数据目录、gc/sleep、子进程
# 隔离写入等，子进程内仍会同样崩溃，故改为 SQLite 存业务数据 + faiss-cpu 内存索引做向量检索，绕开该原生路径。
#
# AI 生成
# 生成目的：总览「哪些数据、经哪条流程、进哪张表/库」
# —————————————————————————————————————————————————————————————————
# 根目录：默认项目下 faiss_sqlite_data/，可通过 set_job_store_dir 覆盖；本模块使用两类 SQLite 文件（相互独立）。
#
# （一）crawl_{platform}.db，表 list_jobs（按平台分库，如 crawl_liepin.db）
#   · 首写：猎聘等爬虫在【列表页通过硬筛、尚未进详情或详情未跑完前】对每条岗位
#     upsert_crawl_list_job(platform, scene_id, job_dict)
#     写入/覆盖：id=md5(scene_id+url)、scene_id、title/company/location/description(列表侧「介绍」)、url、fetch_timestamp；
#     用于 is_crawl_list_url_present 去重、断点与列表侧快照。INSERT OR REPLACE 会整行替换，未带 hr_greeting 时该列会按库默认回退。
#   · 后写：API main 在【LLM 筛选 + write_to_csv 写 HR招呼语 列之后】
#     utils.files.write_to_csv 内调用 update_crawl_list_hr_greeting(platform, scene_id, url, hr_greeting)
#     对「已存在 list_jobs 行」UPDATE hr_greeting；无对应行则无任何插入（需先经列表 upsert）。
#
# （二）jobs.db，表 pending_jobs 与 memory_jobs（岗位「待人工 / 已决策」主库，与列表快照库分离）
#   · pending_jobs：add_pending_job(job_dict) 在「该 url 的 id 既不在 pending 也不在 memory」时
#     INSERT 一条元数据（id=md5(url) 仅按链接全局去重，与 list_jobs 主键不同）。
#     本仓库主流程不强制从爬虫子模块自动写入，供后续待审核队列/其他入口调用；集成测试会主动调用。
#   · memory_jobs：move_to_memory(job_id, approved|rejected, reason?) 在人工审核/反馈时
#     从 pending 删除并写入 memory：含 status、reject_reason、reviewed_at；拒绝对象会算拒绝理由的 Embedding
#     写入 reject_emb（BLOB），供 get_similar_rejected_reasons 做相似「不合适理由」检索（FAISS 在内存、不落盘；该
#     查询过程中可能对缺向量的历史拒绝行补算 reject_emb 并回写表）。
# —————————————————————————————————————————————————————————————————
#

from __future__ import annotations

import hashlib
import random
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

import faiss
import numpy as np

# AI 生成
# 生成目的：项目根下的默认数据目录
_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_STORE_DIR = _ROOT / "faiss_sqlite_data"

# AI 生成
# 生成目的：测试时可覆盖存储根目录
_STORE_DIR_OVERRIDE: Optional[str] = None
_conn: Optional[sqlite3.Connection] = None
_lock = threading.RLock()

# 各平台列表爬取快照库（与 pending/memory 的 jobs.db 分离）：{platform: sqlite3.Connection}
_crawl_conns: Dict[str, sqlite3.Connection] = {}


def set_job_store_dir(path: str | None) -> None:
    # AI 生成
    # 生成目的：切换 SQLite 文件所在根目录；传入 None 恢复默认 faiss_sqlite_data
    global _STORE_DIR_OVERRIDE, _conn, _crawl_conns
    with _lock:
        _STORE_DIR_OVERRIDE = path
        if _conn is not None:
            try:
                _conn.close()
            except Exception:
                pass
            _conn = None
        for _plat, c in list(_crawl_conns.items()):
            try:
                c.close()
            except Exception:
                pass
        _crawl_conns.clear()


def _store_dir() -> Path:
    # AI 生成
    # 生成目的：解析当前数据根目录
    if _STORE_DIR_OVERRIDE:
        return Path(_STORE_DIR_OVERRIDE).resolve()
    return _DEFAULT_STORE_DIR.resolve()


def _db_path() -> Path:
    # AI 生成
    # 生成目的：SQLite 单库路径
    return _store_dir() / "jobs.db"


def _crawl_platform_db_filename(platform: str) -> str:
    safe = "".join(c if c.isalnum() or c in "_-" else "_" for c in (platform or "unknown").lower())
    return f"crawl_{safe}.db"


def _crawl_db_path(platform: str) -> Path:
    return _store_dir() / _crawl_platform_db_filename(platform)


def _ensure_list_jobs_hr_greeting(c: sqlite3.Connection) -> None:
    # AI 生成
    # 生成目的：列表快照表增加与 CSV 同步的「HR 招呼语」
    cur = c.execute("PRAGMA table_info(list_jobs)").fetchall()
    col_names = {r[1] for r in cur} if cur else set()
    if "hr_greeting" in col_names:
        return
    c.execute(
        "ALTER TABLE list_jobs ADD COLUMN hr_greeting TEXT NOT NULL DEFAULT ''"
    )
    c.commit()


def _ensure_list_jobs_llm_cols(c: sqlite3.Connection) -> None:
    """列表快照表增加 LLM 结构化筛选列（match_level/reason/apply）。"""
    cur = c.execute("PRAGMA table_info(list_jobs)").fetchall()
    col_names = {r[1] for r in cur} if cur else set()
    alters: list[str] = []
    if "match_level" not in col_names:
        alters.append(
            "ALTER TABLE list_jobs ADD COLUMN match_level TEXT NOT NULL DEFAULT ''"
        )
    if "reason" not in col_names:
        alters.append("ALTER TABLE list_jobs ADD COLUMN reason TEXT NOT NULL DEFAULT ''")
    if "apply" not in col_names:
        alters.append("ALTER TABLE list_jobs ADD COLUMN apply TEXT NOT NULL DEFAULT ''")
    # 预留：人工复核字段（后续扩展用）
    if "manual_apply" not in col_names:
        alters.append(
            "ALTER TABLE list_jobs ADD COLUMN manual_apply TEXT NOT NULL DEFAULT ''"
        )
    if "manual_reason" not in col_names:
        alters.append(
            "ALTER TABLE list_jobs ADD COLUMN manual_reason TEXT NOT NULL DEFAULT ''"
        )
    if not alters:
        return
    for sql in alters:
        c.execute(sql)
    c.commit()


def _get_crawl_conn(platform: str) -> sqlite3.Connection:
    global _crawl_conns
    key = (platform or "unknown").strip().lower() or "unknown"
    with _lock:
        if key not in _crawl_conns:
            d = _store_dir()
            d.mkdir(parents=True, exist_ok=True)
            p = _crawl_db_path(key)
            c = sqlite3.connect(str(p), check_same_thread=False)
            c.row_factory = sqlite3.Row
            c.executescript(
                """
                CREATE TABLE IF NOT EXISTS list_jobs (
                    id TEXT PRIMARY KEY,
                    scene_id INTEGER NOT NULL,
                    title TEXT NOT NULL DEFAULT '',
                    company TEXT NOT NULL DEFAULT '',
                    location TEXT NOT NULL DEFAULT '',
                    description TEXT NOT NULL DEFAULT '',
                    url TEXT NOT NULL DEFAULT '',
                    fetch_timestamp TEXT NOT NULL DEFAULT ''
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_list_jobs_scene_url
                    ON list_jobs(scene_id, url);
                """
            )
            _ensure_list_jobs_hr_greeting(c)
            _ensure_list_jobs_llm_cols(c)
            c.commit()
            _crawl_conns[key] = c
        return _crawl_conns[key]


def update_crawl_list_hr_greeting(
    platform: str, scene_id: int, url: str, hr_greeting: str
) -> None:
    # AI 生成
    # 生成目的：在列表爬取表上按 scene_id+url 回写与 CSV 一致的招呼语
    u = (url or "").strip()
    if not u:
        return
    conn = _get_crawl_conn(platform)
    with _lock:
        _ensure_list_jobs_hr_greeting(conn)
        conn.execute(
            "UPDATE list_jobs SET hr_greeting = ? WHERE scene_id = ? AND url = ?",
            (hr_greeting or "", int(scene_id), u),
        )
        conn.commit()


def update_crawl_list_llm_fields(
    platform: str,
    scene_id: int,
    url: str,
    *,
    match_level: str = "",
    reason: str = "",
    apply: str = "",
    hr_greeting: str = "",
) -> None:
    """LLM 完成后回写 list_jobs 的结构化筛选列与招呼语（按 scene_id+url）。"""
    u = (url or "").strip()
    if not u:
        return
    conn = _get_crawl_conn(platform)
    with _lock:
        _ensure_list_jobs_hr_greeting(conn)
        _ensure_list_jobs_llm_cols(conn)
        conn.execute(
            "UPDATE list_jobs SET match_level = ?, reason = ?, apply = ?, hr_greeting = ? "
            "WHERE scene_id = ? AND url = ?",
            (
                str(match_level or ""),
                str(reason or ""),
                str(apply or ""),
                str(hr_greeting or ""),
                int(scene_id),
                u,
            ),
        )
        conn.commit()

def update_crawl_list_description(
    platform: str, scene_id: int, url: str, description: str
) -> None:
    """详情阶段回写 list_jobs.description（避免 INSERT OR REPLACE 覆盖 hr_greeting）。"""
    u = (url or "").strip()
    if not u:
        return
    conn = _get_crawl_conn(platform)
    with _lock:
        conn.execute(
            "UPDATE list_jobs SET description = ? WHERE scene_id = ? AND url = ?",
            (str(description or ""), int(scene_id), u),
        )
        conn.commit()


def crawl_list_row_id(scene_id: int, url: str) -> str:
    """列表爬取行主键：同平台库内按 scene_id + url 唯一。"""
    normalized = f"{int(scene_id)}\0{(url or '').strip()}"
    return hashlib.md5(normalized.encode("utf-8")).hexdigest()


def _job_dict_from_liepin_list(job_dict: Dict[str, Any]) -> Dict[str, str]:
    title = str(job_dict.get("标题") or job_dict.get("title", "")).strip()
    company = str(job_dict.get("公司") or job_dict.get("company", "")).strip()
    location = str(job_dict.get("地点") or job_dict.get("location", "")).strip()
    description = str(job_dict.get("介绍") or job_dict.get("description", "")).strip()
    url = str(job_dict.get("链接") or job_dict.get("url", "")).strip()
    ts = job_dict.get("fetch_timestamp")
    if ts is None or str(ts).strip() == "":
        ts = datetime.now(timezone.utc).isoformat()
    fetch_timestamp = str(ts).strip()
    return {
        "title": title,
        "company": company,
        "location": location,
        "description": description,
        "url": url,
        "fetch_timestamp": fetch_timestamp,
    }


def is_crawl_list_url_present(platform: str, scene_id: int, url: str) -> bool:
    """某平台库 + 场景下是否已记录该列表页链接（用于跳过已爬岗位）。"""
    u = (url or "").strip()
    if not u:
        return False
    conn = _get_crawl_conn(platform)
    with _lock:
        row = conn.execute(
            "SELECT 1 FROM list_jobs WHERE scene_id = ? AND url = ? LIMIT 1",
            (int(scene_id), u),
        ).fetchone()
        return row is not None


def upsert_crawl_list_job(platform: str, scene_id: int, job_dict: Dict[str, Any]) -> None:
    """列表阶段硬校验通过后写入该平台库（字段与 pending 元数据一致 + scene_id）。"""
    f = _job_dict_from_liepin_list(job_dict)
    url = f["url"]
    if not url:
        return
    jid = crawl_list_row_id(scene_id, url)
    conn = _get_crawl_conn(platform)
    with _lock:
        conn.execute(
            """INSERT OR REPLACE INTO list_jobs
            (id, scene_id, title, company, location, description, url, fetch_timestamp)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                jid,
                int(scene_id),
                f["title"],
                f["company"],
                f["location"],
                f["description"],
                f["url"],
                f["fetch_timestamp"],
            ),
        )
        conn.commit()


def _get_conn() -> sqlite3.Connection:
    # AI 生成
    # 生成目的：懒加载连接并建表
    global _conn
    with _lock:
        if _conn is None:
            d = _store_dir()
            d.mkdir(parents=True, exist_ok=True)
            _conn = sqlite3.connect(str(_db_path()), check_same_thread=False)
            _conn.row_factory = sqlite3.Row
            _ensure_schema(_conn)
        return _conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    # AI 生成
    # 生成目的：pending / memory 两表及拒绝理由向量列
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS pending_jobs (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL DEFAULT '',
            company TEXT NOT NULL DEFAULT '',
            location TEXT NOT NULL DEFAULT '',
            description TEXT NOT NULL DEFAULT '',
            url TEXT NOT NULL DEFAULT '',
            fetch_timestamp TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS memory_jobs (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL DEFAULT '',
            company TEXT NOT NULL DEFAULT '',
            location TEXT NOT NULL DEFAULT '',
            description TEXT NOT NULL DEFAULT '',
            url TEXT NOT NULL DEFAULT '',
            fetch_timestamp TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL,
            reject_reason TEXT NOT NULL DEFAULT '',
            reviewed_at TEXT NOT NULL DEFAULT '',
            reject_emb BLOB
        );
        """
    )
    conn.commit()


def job_id_from_url(url: str) -> str:
    # AI 生成
    # 生成目的：按 URL 的 md5 作为全局稳定主键
    normalized = (url or "").strip()
    return hashlib.md5(normalized.encode("utf-8")).hexdigest()


def _normalize_job_dict(job_dict: Dict[str, Any]) -> Dict[str, str]:
    # AI 生成
    # 生成目的：统一字段名与类型
    title = str(job_dict.get("title", "")).strip()
    company = str(job_dict.get("company", "")).strip()
    location = str(job_dict.get("location", "")).strip()
    description = str(job_dict.get("description", "")).strip()
    url = str(job_dict.get("url", "")).strip()
    ts = job_dict.get("fetch_timestamp")
    if ts is None or str(ts).strip() == "":
        ts = datetime.now(timezone.utc).isoformat()
    fetch_timestamp = str(ts).strip()
    return {
        "title": title,
        "company": company,
        "location": location,
        "description": description,
        "url": url,
        "fetch_timestamp": fetch_timestamp,
    }


def _openai_client():
    # AI 生成
    # 生成目的：从 config 构造 OpenAI 客户端（与 verify_openai_key 一致）
    import config as cfg
    from openai import OpenAI

    key = (getattr(cfg, "openai_API_KEY", None) or "").strip()
    if not key:
        raise ValueError("未配置 openai_API_KEY")
    base_raw = (getattr(cfg, "OPENAI_API_BASE", None) or "").strip()
    base = base_raw.rstrip("/")
    if base and not base.endswith("/v1"):
        base = f"{base}/v1"
    kw: Dict[str, Any] = {"api_key": key}
    if base:
        kw["base_url"] = base
    return OpenAI(**kw), cfg


def _embed_texts(texts: List[str]) -> np.ndarray:
    # AI 生成
    # 生成目的：批量调用 OpenAI Embeddings，返回 float32 矩阵并已 L2 归一化（便于 FAISS 内积≈余弦）
    if not texts:
        return np.zeros((0, 0), dtype=np.float32)
    client, cfg = _openai_client()
    model = (getattr(cfg, "OPENAI_EMBEDDING_MODEL", None) or "text-embedding-3-small").strip()
    resp = client.embeddings.create(model=model, input=texts)
    order = sorted(range(len(resp.data)), key=lambda i: resp.data[i].index)
    vecs = np.array(
        [resp.data[i].embedding for i in order],
        dtype=np.float32,
    )
    faiss.normalize_L2(vecs)
    return vecs


def _row_from_sql(row: sqlite3.Row, include_memory: bool) -> Dict[str, Any]:
    # AI 生成
    # 生成目的：将 SQLite 行转为对外 dict
    out: Dict[str, Any] = {
        "id": row["id"],
        "title": row["title"] or "",
        "company": row["company"] or "",
        "location": row["location"] or "",
        "description": row["description"] or "",
        "url": row["url"] or "",
        "fetch_timestamp": row["fetch_timestamp"] or "",
    }
    if include_memory:
        out["status"] = row["status"] or ""
        out["reject_reason"] = row["reject_reason"] or ""
        out["reviewed_at"] = row["reviewed_at"] or ""
    return out


def add_pending_job(job_dict: Dict[str, Any]) -> bool:
    # AI 生成
    # 生成目的：写入 pending；若 id 已在 pending 或 memory 则跳过
    fields = _normalize_job_dict(job_dict)
    url = fields["url"]
    if not url:
        return False
    jid = job_id_from_url(url)
    if is_job_processed(url):
        return False
    conn = _get_conn()
    with _lock:
        cur = conn.execute(
            """INSERT OR IGNORE INTO pending_jobs
            (id, title, company, location, description, url, fetch_timestamp)
            VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                jid,
                fields["title"],
                fields["company"],
                fields["location"],
                fields["description"],
                fields["url"],
                fields["fetch_timestamp"],
            ),
        )
        conn.commit()
        return cur.rowcount == 1


def get_pending_jobs(limit: int = 1) -> List[Dict[str, Any]]:
    # AI 生成
    # 生成目的：随机取若干条待审核
    conn = _get_conn()
    with _lock:
        ids = [r[0] for r in conn.execute("SELECT id FROM pending_jobs").fetchall()]
    if not ids:
        return []
    k = min(max(1, int(limit)), len(ids))
    picked = random.sample(ids, k)
    out: List[Dict[str, Any]] = []
    with _lock:
        for jid in picked:
            row = conn.execute(
                "SELECT * FROM pending_jobs WHERE id = ?", (jid,)
            ).fetchone()
            if row:
                out.append(_row_from_sql(row, include_memory=False))
    return out


def move_to_memory(
    job_id: str,
    status: Literal["approved", "rejected"],
    reason: Optional[str] = None,
) -> bool:
    # AI 生成
    # 生成目的：从 pending 删除并写入 memory；拒绝时写入理由向量列供 FAISS 检索
    if status not in ("approved", "rejected"):
        raise ValueError("status 必须是 approved 或 rejected")
    conn = _get_conn()
    reject_reason = (reason or "").strip() if status == "rejected" else ""
    reviewed_at = datetime.now(timezone.utc).isoformat()
    emb_blob: Optional[bytes] = None
    if status == "rejected" and reject_reason:
        v = _embed_texts([reject_reason])[0]
        emb_blob = v.astype(np.float32).tobytes()

    with _lock:
        prow = conn.execute(
            "SELECT * FROM pending_jobs WHERE id = ?", (job_id,)
        ).fetchone()
        if not prow:
            return False
        conn.execute("DELETE FROM memory_jobs WHERE id = ?", (job_id,))
        conn.execute(
            """INSERT INTO memory_jobs (
                id, title, company, location, description, url, fetch_timestamp,
                status, reject_reason, reviewed_at, reject_emb
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                job_id,
                prow["title"],
                prow["company"],
                prow["location"],
                prow["description"],
                prow["url"],
                prow["fetch_timestamp"],
                status,
                reject_reason,
                reviewed_at,
                emb_blob,
            ),
        )
        conn.execute("DELETE FROM pending_jobs WHERE id = ?", (job_id,))
        conn.commit()
    return True


def is_job_processed(job_url: str) -> bool:
    # AI 生成
    # 生成目的：URL 对应 id 是否已在 pending 或 memory
    jid = job_id_from_url(job_url)
    conn = _get_conn()
    with _lock:
        if conn.execute(
            "SELECT 1 FROM pending_jobs WHERE id = ? LIMIT 1", (jid,)
        ).fetchone():
            return True
        if conn.execute(
            "SELECT 1 FROM memory_jobs WHERE id = ? LIMIT 1", (jid,)
        ).fetchone():
            return True
    return False


def get_similar_rejected_reasons(query_text: str, n: int = 3) -> List[Dict[str, Any]]:
    # AI 生成
    # 生成目的：对 memory 中 status=rejected 且理由非空的记录，用 FAISS 内积检索与 query 最相近的若干条
    q = (query_text or "").strip()
    if not q:
        return []
    conn = _get_conn()
    with _lock:
        rows = conn.execute(
            """SELECT id, title, company, location, description, url, fetch_timestamp,
                      reject_reason, reject_emb FROM memory_jobs
               WHERE status = 'rejected' AND reject_reason != ''"""
        ).fetchall()
    if not rows:
        return []

    vecs: List[Optional[np.ndarray]] = []
    missing_idx: List[int] = []
    missing_texts: List[str] = []
    for i, row in enumerate(rows):
        blob = row["reject_emb"]
        if blob:
            v = np.frombuffer(blob, dtype=np.float32).copy()
            faiss.normalize_L2(v.reshape(1, -1))
            vecs.append(v.reshape(-1))
        else:
            vecs.append(None)
            missing_idx.append(i)
            missing_texts.append((row["reject_reason"] or "").strip())

    if missing_texts:
        new_m = _embed_texts(missing_texts)
        with _lock:
            for k, mi in enumerate(missing_idx):
                v = new_m[k]
                vecs[mi] = v.copy()
                conn.execute(
                    "UPDATE memory_jobs SET reject_emb = ? WHERE id = ?",
                    (v.astype(np.float32).tobytes(), rows[mi]["id"]),
                )
            conn.commit()

    dense = [v for v in vecs if v is not None]
    if len(dense) != len(rows):
        return []
    X = np.stack(dense).astype(np.float32)
    d = X.shape[1]
    qv = _embed_texts([q])
    if qv.shape[1] != d:
        return []
    index = faiss.IndexFlatIP(d)
    index.add(X)
    scores, idxs = index.search(qv, min(max(1, int(n)), len(rows)))
    out: List[Dict[str, Any]] = []
    for j in range(idxs.shape[1]):
        ti = int(idxs[0, j])
        if ti < 0 or ti >= len(rows):
            continue
        row = rows[ti]
        ip = float(scores[0, j])
        out.append(
            {
                "id": row["id"],
                "reject_reason": row["reject_reason"] or "",
                "url": row["url"] or "",
                "title": row["title"] or "",
                "company": row["company"] or "",
                "distance": 1.0 - ip,
            }
        )
    return out


def reset_collections_for_tests() -> None:
    # AI 生成
    # 生成目的：测试用清空两表及各平台 crawl_*.db
    global _crawl_conns
    conn = _get_conn()
    with _lock:
        conn.executescript(
            "DROP TABLE IF EXISTS pending_jobs; DROP TABLE IF EXISTS memory_jobs;"
        )
        _ensure_schema(conn)
        for _plat, c in list(_crawl_conns.items()):
            try:
                c.close()
            except Exception:
                pass
        _crawl_conns.clear()
        try:
            for p in _store_dir().glob("crawl_*.db"):
                p.unlink(missing_ok=True)
        except OSError:
            pass
