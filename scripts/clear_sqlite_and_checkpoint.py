# -*- coding: utf-8 -*-
"""
清理本项目 SQLite 数据库与 checkpoint.json。

默认数据目录：<repo>/faiss_sqlite_data/
 - crawl_*.db（如 crawl_liepin.db）：表 list_jobs，包含 scene_id
 - jobs.db：表 pending_jobs / memory_jobs（不含 scene_id）

用法：
  # 清理指定场景（仅影响 crawl_*.db 的 list_jobs）
  python scripts/clear_sqlite_and_checkpoint.py --scene-id 6 --scene-id 7

  # 清空所有 SQLite（包含 jobs.db 的 pending/memory）
  python scripts/clear_sqlite_and_checkpoint.py --all

  # 同时清空 checkpoint.json（默认会清空）
  python scripts/clear_sqlite_and_checkpoint.py --scene-id 6 --scene-id 7 --clear-checkpoint
"""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path
from typing import Iterable, List, Optional, Sequence


def _root_dir() -> Path:
    return Path(__file__).resolve().parent.parent


def _store_dir() -> Path:
    return _root_dir() / "faiss_sqlite_data"


def _checkpoint_path() -> Path:
    # 与 utils.crawl_checkpoint.DEFAULT_CHECKPOINT_PATH 保持一致
    return _root_dir() / "data" / "checkpoint.json"


def _iter_db_files(store: Path) -> List[Path]:
    if not store.exists():
        return []
    return sorted([p for p in store.iterdir() if p.is_file() and p.suffix.lower() == ".db"])


def _exec(conn: sqlite3.Connection, sql: str, params: Sequence[object] = ()) -> int:
    cur = conn.execute(sql, params)
    return cur.rowcount if cur.rowcount is not None else 0


def _clear_list_jobs_by_scene(db_path: Path, scene_ids: Iterable[int]) -> int:
    conn = sqlite3.connect(str(db_path))
    try:
        # 确认表存在
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        }
        if "list_jobs" not in tables:
            return 0
        n = 0
        for sid in scene_ids:
            n += _exec(conn, "DELETE FROM list_jobs WHERE scene_id = ?", (int(sid),))
        conn.commit()
        return n
    finally:
        conn.close()


def _clear_all_tables(db_path: Path) -> int:
    conn = sqlite3.connect(str(db_path))
    try:
        tables = [
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        ]
        total = 0
        for t in tables:
            total += _exec(conn, f'DELETE FROM "{t}"')
        conn.commit()
        return total
    finally:
        conn.close()


def _clear_checkpoint_file(p: Path) -> bool:
    try:
        p.unlink(missing_ok=True)
        return True
    except OSError:
        return False


def main() -> int:
    ap = argparse.ArgumentParser(description="Clear sqlite store and checkpoint.json")
    ap.add_argument("--scene-id", action="append", type=int, default=[], help="按 scene_id 清 list_jobs（可重复传入）")
    ap.add_argument("--all", action="store_true", help="清空 store_dir 下所有 .db 的全部表（危险）")
    ap.add_argument("--store-dir", type=str, default="", help="自定义 faiss_sqlite_data 目录")
    ap.add_argument("--clear-checkpoint", action="store_true", help="清空 data/checkpoint.json（默认：会清）")
    args = ap.parse_args()

    store = Path(args.store_dir).resolve() if args.store_dir else _store_dir()
    dbs = _iter_db_files(store)

    if not dbs:
        print(f"未找到 .db 文件：{store}")
    else:
        if args.all:
            print(f"即将清空所有 SQLite：{store}（{len(dbs)} 个 .db）")
            for db in dbs:
                n = _clear_all_tables(db)
                print(f"- {db.name}: deleted_rows={n}")
        else:
            scene_ids: List[int] = [int(x) for x in (args.scene_id or [])]
            if not scene_ids:
                print("未提供 --scene-id，且未指定 --all；未对 SQLite 执行删除。")
            else:
                print(f"按 scene_id 清理 list_jobs：{scene_ids}")
                for db in dbs:
                    n = _clear_list_jobs_by_scene(db, scene_ids)
                    if n:
                        print(f"- {db.name}: deleted_rows={n}")
                    else:
                        print(f"- {db.name}: no-op (no list_jobs or no matching scene_id)")

    # checkpoint：用户本次明确要求清空；默认也清（避免断点残留）
    if args.clear_checkpoint or True:
        cp = _checkpoint_path()
        ok = _clear_checkpoint_file(cp)
        print(f"checkpoint.json 清空：{cp} ({'ok' if ok else 'failed'})")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

