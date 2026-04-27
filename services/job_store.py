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


def set_job_store_dir(path: str | None) -> None:
    # AI 生成
    # 生成目的：切换 SQLite 文件所在根目录；传入 None 恢复默认 faiss_sqlite_data
    global _STORE_DIR_OVERRIDE, _conn
    with _lock:
        _STORE_DIR_OVERRIDE = path
        if _conn is not None:
            try:
                _conn.close()
            except Exception:
                pass
            _conn = None


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
    # 生成目的：测试用清空两表
    conn = _get_conn()
    with _lock:
        conn.executescript(
            "DROP TABLE IF EXISTS pending_jobs; DROP TABLE IF EXISTS memory_jobs;"
        )
        _ensure_schema(conn)
