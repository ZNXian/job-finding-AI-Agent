# -*- coding: utf-8 -*-
"""
端到端测试：本机启动 uvicorn 后 POST /api/crawl_liepin，使用真实 Playwright 与猎聘网页。

默认：scene_id=6、crawl_only=true（不调 LLM、不写 CSV）。浏览器是否无头由 config.CRAWL_HEADLESS / .env 决定。

用法：
  venv\\Scripts\\python.exe scripts\\e2e_crawl_liepin_api.py
  venv\\Scripts\\python.exe scripts\\e2e_crawl_liepin_api.py --port 8765 --scene-id 6
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent


def _wait_http_ready(host: str, port: int, path: str = "/docs", attempts: int = 60) -> None:
    url = f"http://{host}:{port}{path}"
    for i in range(attempts):
        try:
            urllib.request.urlopen(url, timeout=2)
            return
        except (urllib.error.URLError, OSError):
            time.sleep(1)
    raise RuntimeError(f"服务在 {attempts}s 内未就绪: {url}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--scene-id", type=int, default=6)
    ap.add_argument("--timeout-sec", type=int, default=900, help="单次 POST 最长等待秒数")
    ap.add_argument("--no-crawl-only", action="store_true", help="为 True 时走完整 CSV+LLM（更久）")
    args = ap.parse_args()

    os.chdir(_ROOT)
    if str(_ROOT) not in sys.path:
        sys.path.insert(0, str(_ROOT))

    env = os.environ.copy()
    env.setdefault("PYTHONUNBUFFERED", "1")

    cmd = [
        sys.executable,
        "-m",
        "uvicorn",
        "main:app",
        "--host",
        args.host,
        "--port",
        str(args.port),
    ]
    print("启动:", " ".join(cmd), flush=True)
    proc = subprocess.Popen(
        cmd,
        cwd=str(_ROOT),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        _wait_http_ready(args.host, args.port)
        print(f"服务已就绪 http://{args.host}:{args.port}/docs", flush=True)

        crawl_only = "false" if args.no_crawl_only else "true"
        post_url = (
            f"http://{args.host}:{args.port}/api/crawl_liepin"
            f"?scene_id={args.scene_id}&crawl_only={crawl_only}&reset_checkpoint=false"
        )
        print("POST:", post_url, flush=True)
        print("(真实浏览器与网页爬取，请在本机观察窗口；耗时取决于 MAX_PAGE 与岗位数量)", flush=True)

        req = urllib.request.Request(post_url, method="POST", headers={})
        with urllib.request.urlopen(req, timeout=args.timeout_sec) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            print("HTTP", resp.status, flush=True)
            print(body[:4000], flush=True)
            if len(body) > 4000:
                print("... (响应已截断)", flush=True)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
