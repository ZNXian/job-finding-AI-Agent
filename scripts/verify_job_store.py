# -*- coding: utf-8 -*-
# AI 生成
# 生成目的：命令行一键验证 FAISS+SQLite 持久化、去重、pending↔memory 流转与拒绝理由语义检索

from __future__ import annotations

import shutil
import sys
from pathlib import Path

# AI 生成
# 生成目的：保证可从项目根或 scripts 目录执行时均能 import services
_ROOT = Path(__file__).resolve().parent.parent
# AI 生成
# 生成目的：集成测试用数据目录（项目根下，避免系统 Temp 触发杀毒/路径问题）
JOB_STORE_VERIFY_DIR = _ROOT / "job_store_verify_test"
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import config as cfg  # noqa: E402
from services import job_store as js  # noqa: E402


def _trace(step: str, detail: str = "") -> None:
    # AI 生成
    # 生成目的：分步输出并立即 flush，便于定位失败步骤
    suffix = f" | {detail}" if detail else ""
    print(f"[verify_job_store] {step}{suffix}", flush=True)


def main() -> int:
    # AI 生成
    # 生成目的：拒绝理由语义检索依赖 OpenAI Embeddings，无 Key 时提前退出并提示
    _trace("S00", "进入 main()")
    if not (getattr(cfg, "openai_API_KEY", None) or "").strip():
        print("请先在 config.py 填写 openai_API_KEY 后再运行本脚本（将调用 OpenAI Embedding API）")
        return 2

    # AI 生成
    # 生成目的：在项目内固定目录跑集成测试，不污染默认 faiss_sqlite_data；每次先清空再建
    verify_path = JOB_STORE_VERIFY_DIR.resolve()
    _trace("S01", f"存储目录（项目内）: {verify_path}")
    if verify_path.exists():
        shutil.rmtree(verify_path, ignore_errors=True)
    verify_path.mkdir(parents=True, exist_ok=True)
    verify_str = str(verify_path)
    try:
        _trace("S02", "调用 set_job_store_dir(verify_str)")
        js.set_job_store_dir(verify_str)
        _trace("S03", "调用 reset_collections_for_tests() 前")
        js.reset_collections_for_tests()
        _trace("S04", "reset_collections_for_tests() 返回")

        url = "https://www.liepin.com/job/100.shtml?"
        scene_id = 999
        platform = "liepin"
        platform_job_id = "100"
        jid = js.pending_memory_row_id(scene_id, platform, platform_job_id)
        _trace("S05", f"pending_memory_row_id OK, jid={jid[:8]}...")
        assert len(jid) == 32, "id 应为 32 位 md5"

        job = {
            "title": "Python 开发",
            "company": "DemoCo",
            "location": "上海",
            "description": "负责后端与爬虫系统",
            "url": url,
            "scene_id": scene_id,
            "platform": platform,
            "platform_job_id": platform_job_id,
            "fetch_timestamp": "2026-04-26T00:00:00+00:00",
        }

        # AI 生成
        # 生成目的：验证首次添加返回 True，重复添加因 id 已存在返回 False
        _trace("S06", "第一次 add_pending_job 前")
        assert js.add_pending_job(job) is True
        _trace("S07", "第一次 add_pending_job 返回 True")
        _trace("S08", "第二次 add_pending_job（去重）前")
        assert js.add_pending_job(job) is False
        _trace("S09", "第二次 add_pending_job 返回 False")

        # AI 生成
        # 生成目的：验证 is_job_processed 在 pending 阶段为 True
        _trace("S10", "is_job_processed 前")
        assert js.is_job_processed(scene_id, platform, platform_job_id) is True
        _trace("S11", "is_job_processed 返回 True")

        _trace("S12", "get_pending_jobs 前")
        rows = js.get_pending_jobs(limit=5)
        _trace("S13", f"get_pending_jobs 返回 {len(rows)} 条")
        assert len(rows) == 1
        assert rows[0]["id"] == jid
        assert rows[0]["url"] == url
        assert rows[0]["platform_job_id"] == platform_job_id

        # AI 生成
        # 生成目的：验证 move_to_memory 后 pending 空、memory 可检索
        _trace("S14", "move_to_memory(approved) 前")
        assert js.move_to_memory(scene_id, platform, platform_job_id, "approved", reason=None) is True
        _trace("S15", "move_to_memory(approved) 返回")
        _trace("S16", "get_pending_jobs（应空）前")
        assert js.get_pending_jobs(limit=5) == []
        _trace("S17", "pending 已空")
        assert js.is_job_processed(scene_id, platform, platform_job_id) is True
        _trace("S18", "is_job_processed 仍为 True")

        url2 = "https://www.liepin.com/job/200.shtml?"
        platform_job_id2 = "200"
        _trace("S19", "add_pending_job(job2) 前")
        js.add_pending_job(
            {
                "title": "Java 开发",
                "company": "OtherCo",
                "location": "北京",
                "description": "业务系统",
                "url": url2,
                "scene_id": scene_id,
                "platform": platform,
                "platform_job_id": platform_job_id2,
            }
        )
        _trace("S20", "add_pending_job(job2) 返回")
        _trace("S21", "move_to_memory(rejected) 前")
        assert (
            js.move_to_memory(
                scene_id, platform, platform_job_id2, "rejected", reason="薪资低于预期，且技术栈偏传统"
            )
            is True
        )
        _trace("S22", "move_to_memory(rejected) 返回")

        _trace("S23", "get_similar_rejected_reasons 前")
        sim = js.get_similar_rejected_reasons("钱给得太少，不想做老旧技术", n=3)
        _trace("S24", f"get_similar_rejected_reasons 返回 {len(sim)} 条")
        assert isinstance(sim, list) and len(sim) >= 1
        top = sim[0]
        assert "reject_reason" in top
        assert top.get("url") == url2

        _trace("S25", "全部断言通过")
        print("verify_job_store: OK", flush=True)
        return 0
    finally:
        _trace("FINALLY", "清理：set_job_store_dir(None) 与删除 job_store_verify_test")
        js.set_job_store_dir(None)
        shutil.rmtree(verify_path, ignore_errors=True)


if __name__ == "__main__":
    # AI 生成
    # 生成目的：若 import 阶段未崩，可确认进程已进入脚本入口
    _trace("ENTRY", "python 已加载本脚本，即将调用 main()")
    raise SystemExit(main())
