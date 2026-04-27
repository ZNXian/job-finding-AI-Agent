# -*- coding: utf-8 -*-
"""验证 POST /api/crawl_liepin 在 crawl_only=true 时会调用 crawl_liepin，且不执行写 CSV / LLM。"""
from __future__ import annotations

import os
import sys
from unittest.mock import patch

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
os.chdir(_ROOT)


def main() -> int:
    import main as app_main

    crawl_calls: list[int] = []

    def fake_crawl_liepin():
        crawl_calls.append(1)
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

    with patch.object(app_main, "crawl_liepin", side_effect=fake_crawl_liepin) as m_crawl, patch.object(
        app_main, "write_to_csv"
    ) as m_csv, patch.object(app_main, "llm_process_job") as m_llm:
        body = app_main.run_crawl_and_ai(scene_id=6, crawl_only=True)

    assert body.get("code") == 200, body
    assert body.get("crawl_only") is True
    assert body.get("job_count") == 1
    assert body.get("scene_id") == 6
    assert "jobs_preview" in body
    assert len(crawl_calls) == 1
    m_crawl.assert_called_once()
    m_csv.assert_not_called()
    m_llm.assert_not_called()

    print("OK: run_crawl_and_ai(scene_id=6, crawl_only=True) 调用了 crawl_liepin，且未调用 write_to_csv / llm_process_job")
    print("真实爬取（列表+详情）在本机启动服务后执行：")
    print(
        '  curl -X POST "http://127.0.0.1:8000/api/crawl_liepin?scene_id=6&crawl_only=true"'
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
