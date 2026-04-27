# -*- coding: utf-8 -*-
"""测试 POST /api/crawl_liepin：HTTP 路由 + crawl_only / reset_checkpoint 与 crawl_liepin 入参。"""
from __future__ import annotations

import os
import sys
from unittest.mock import patch

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
os.chdir(_ROOT)


def main() -> int:
    from fastapi.testclient import TestClient

    import main as app_main

    crawl_kwargs: list[dict] = []

    def fake_crawl_liepin(**kwargs):
        crawl_kwargs.append(dict(kwargs))
        return [
            {
                "平台": "猎聘",
                "标题": "mock-岗位",
                "公司": "mock-公司",
                "薪资": "20-30万",
                "地点": "北京",
                "工作年限": "3-5年",
                "链接": "https://www.liepin.com/job/mock",
                "介绍": "mock",
            }
        ]

    client = TestClient(app_main.app)

    with patch.object(app_main, "crawl_liepin", side_effect=fake_crawl_liepin) as m_crawl, patch.object(
        app_main, "write_to_csv"
    ) as m_csv, patch.object(app_main, "llm_process_job") as m_llm:
        r = client.post("/api/crawl_liepin?scene_id=6&crawl_only=true")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body.get("code") == 200, body
        assert body.get("crawl_only") is True
        assert body.get("job_count") == 1
        assert body.get("scene_id") == 6
        assert "jobs_preview" in body
        assert len(crawl_kwargs) == 1
        assert crawl_kwargs[0] == {"scene_id": 6, "reset_checkpoint": False}
        m_crawl.assert_called_once()
        m_csv.assert_not_called()
        m_llm.assert_not_called()

    crawl_kwargs.clear()
    with patch.object(app_main, "crawl_liepin", side_effect=fake_crawl_liepin), patch.object(
        app_main, "write_to_csv"
    ), patch.object(app_main, "llm_process_job"):
        r2 = client.post("/api/crawl_liepin?scene_id=6&crawl_only=false&reset_checkpoint=true")
        assert r2.status_code == 200, r2.text
        b2 = r2.json()
        assert b2.get("crawl_only") is False
        assert "csv_file" in b2
        assert crawl_kwargs[0] == {"scene_id": 6, "reset_checkpoint": True}

    print("OK: POST /api/crawl_liepin")
    print("  - crawl_only=true → 200, jobs_preview, crawl_liepin(scene_id=6, reset_checkpoint=False)")
    print("  - reset_checkpoint=true → crawl_liepin(..., reset_checkpoint=True)")
    print("真实爬取：启动服务后 curl -X POST \"http://127.0.0.1:8000/api/crawl_liepin?scene_id=6&crawl_only=true\"")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
