# -*- coding: utf-8 -*-
"""
导出本项目 SQLite 库到 CSV（不依赖启动服务）。

默认会在项目根目录下找 `faiss_sqlite_data/` 中的所有 `.db` 文件，并导出其中所有表：
  python scripts/export_sqlite_to_csv.py

可指定目录（例如你自定义过 job_store.set_job_store_dir）：
  python scripts/export_sqlite_to_csv.py --store-dir D:/path/to/store

输出目录默认：scripts/sqlite_exports/<yyyyMMdd_HHmmss>/
每张表一个 CSV：<db_name>__<table_name>.csv
"""

from __future__ import annotations

import argparse
import base64
import csv
import datetime as dt
import os
import sqlite3
from pathlib import Path
from typing import Any, Iterable, List, Sequence


def _root_dir() -> Path:
    return Path(__file__).resolve().parent.parent


def _default_store_dir() -> Path:
    return _root_dir() / "faiss_sqlite_data"


def _iter_db_files(store_dir: Path) -> List[Path]:
    if not store_dir.exists():
        return []
    return sorted([p for p in store_dir.iterdir() if p.is_file() and p.suffix.lower() == ".db"])


def _list_tables(conn: sqlite3.Connection) -> List[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    return [r[0] for r in rows]


def _to_cell(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, (bytes, bytearray, memoryview)):
        b = bytes(v)
        # 可能是向量/二进制：用 base64，避免 CSV 乱码/截断
        return "base64:" + base64.b64encode(b).decode("ascii")
    # sqlite3.Row / numpy 类型等
    try:
        return str(v)
    except Exception:
        return repr(v)


def _write_csv(path: Path, header: Sequence[str], rows: Iterable[Sequence[Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(list(header))
        for r in rows:
            w.writerow([_to_cell(x) for x in r])


def export_db(db_path: Path, out_dir: Path) -> List[Path]:
    exported: List[Path] = []
    conn = sqlite3.connect(str(db_path))
    try:
        conn.row_factory = sqlite3.Row
        tables = _list_tables(conn)
        for t in tables:
            cur = conn.execute(f'SELECT * FROM "{t}"')
            cols = [d[0] for d in cur.description] if cur.description else []
            # list_jobs 导出时隐藏内部行主键 id/job_id，只保留业务主键 platform_job_id
            if t == "list_jobs":
                keep_idx = [i for i, c in enumerate(cols) if c not in {"id", "job_id"}]
                out_cols = [cols[i] for i in keep_idx]
                rows = [[r[i] for i in keep_idx] for r in cur.fetchall()]
            else:
                out_cols = cols
                rows = cur.fetchall()
            out = out_dir / f"{db_path.stem}__{t}.csv"
            _write_csv(out, out_cols, rows)
            exported.append(out)
    finally:
        conn.close()
    return exported


def main() -> int:
    ap = argparse.ArgumentParser(description="Export sqlite .db files to CSV")
    ap.add_argument("--store-dir", type=str, default="", help="包含 .db 的目录；默认 faiss_sqlite_data/")
    ap.add_argument("--out-dir", type=str, default="", help="输出目录；默认 scripts/sqlite_exports/<timestamp>/")
    args = ap.parse_args()

    store_dir = Path(args.store_dir).resolve() if args.store_dir else _default_store_dir()
    ts = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = Path(args.out_dir).resolve() if args.out_dir else (_root_dir() / "scripts" / "sqlite_exports" / ts)

    dbs = _iter_db_files(store_dir)
    if not dbs:
        print(f"未找到 .db 文件。store_dir={store_dir}")
        print("提示：这些库通常在运行爬虫/写入 job_store 后才会生成。")
        return 0

    print(f"发现 {len(dbs)} 个 DB：{store_dir}")
    total_csv = 0
    for db in dbs:
        outs = export_db(db, out_dir)
        total_csv += len(outs)
        print(f"- {db.name}: 导出 {len(outs)} 张表")

    print(f"完成：共导出 {total_csv} 个 CSV 到 {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

