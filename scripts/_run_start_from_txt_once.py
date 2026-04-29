"""One-off: POST /api/start_from_txt with a temp file (real LLM). Run: python scripts/_run_start_from_txt_once.py"""
import os
import sys
import tempfile

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)

from fastapi.testclient import TestClient

import main


def main_fn() -> None:
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".txt", delete=False, encoding="utf-8"
    ) as f:
        f.write("求职 Python 后端开发，期望北京或远程，薪资 25-35K，3 年经验。\n")
        p = f.name
    try:
        c = TestClient(main.app)
        r = c.post("/api/start_from_txt", json={"file_path": p})
        print("status", r.status_code)
        print(r.text)
    finally:
        os.unlink(p)


if __name__ == "__main__":
    main_fn()
