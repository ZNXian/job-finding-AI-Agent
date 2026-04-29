# -*- coding: utf-8 -*-
"""
不启动 HTTP 服务，直接调用 LLM（百炼 OpenAI 兼容 chat.completions，与 main 一致）做联调。

用法（在项目根目录，且已激活 venv 或指定 venv 的 python）：
  python scripts/test_llm_direct.py identify --text "求职 Python，北京 25-35K"
  python scripts/test_llm_direct.py identify --file D:/path/to/test_scene2.txt
  python scripts/test_llm_direct.py identify --file test_scene2.txt --print-prompt
  python scripts/test_llm_direct.py process
  python scripts/test_llm_direct.py all --file test_scene2.txt

默认从 services.llm_services_legacy 导入（与 main.py 一致）；加 --new 则从 llm_services 导入。
"""
from __future__ import annotations

import argparse
import os
import sys

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
os.chdir(_ROOT)


def _load_llm_funcs(use_new: bool):
    if use_new:
        from services.llm_services import llm_identify_scene, llm_process_job
    else:
        from services.llm_services_legacy import llm_identify_scene, llm_process_job

    return llm_identify_scene, llm_process_job


def _demo_job():
    return {
        "平台": "猎聘",
        "标题": "Python 后端开发工程师",
        "公司": "示例科技有限公司",
        "薪资": "25-35K·14薪",
        "地点": "北京-朝阳区",
        "介绍": "负责后端服务开发，熟悉 Python、FastAPI、MySQL，有微服务经验优先。",
    }


def _print_scene_user_prompt(text: str, scenes: list, use_new: bool) -> None:
    """打印与 llm_identify_scene 第一步相同的 _SCENE_USER_TMPL 拼装内容（便于对照调试）。"""
    if use_new:
        import services.llm_services as m

        def _one_line(s: dict) -> str:
            return (
                f"场景{s['scene_id']}：关键词={s['search_keywords']}, 城市={s['city']}, "
                f"省份={s.get('province', '')}, 远程={s['accept_remote']}, 薪资={s['min_salary']}-{s['max_salary']}"
            )
    else:
        import services.llm_services_legacy as m

        def _one_line(s: dict) -> str:
            return (
                f"场景{s['scene_id']}：关键词={s['search_keywords']}, 城市={s['city']}, "
                f"远程={s['accept_remote']}, 薪资={s['min_salary']}-{s['max_salary']}"
            )
    if not scenes:
        print("(当前无历史场景，不会调用 _SCENE_USER_TMPL，将直接走标准化 JSON)")
        return
    scene_list = "\n".join(_one_line(s) for s in scenes)
    prompt = m._SCENE_USER_TMPL.format(user_text=text, scene_list=scene_list)
    print("--- _SCENE_USER_TMPL 拼装结果（将作为 user 消息发给模型）---")
    print(prompt)
    print("--- 以上 ---\n")


def cmd_identify(args, llm_identify_scene) -> None:
    from services.scences import scene_manager

    if args.file:
        from utils.files import read_and_clean_txt

        text = read_and_clean_txt(os.path.abspath(args.file))
    else:
        text = args.text or ""
    if not text.strip():
        raise SystemExit("请提供 --text 或 --file")

    scenes = scene_manager.get_all_scenes()
    if getattr(args, "print_prompt", False):
        _print_scene_user_prompt(text, scenes, use_new=args.new)
    is_new, scene_result = llm_identify_scene(text, scenes)
    print("is_new_scene:", is_new)
    print("scene_result (新场景为 JSON 字符串，匹配旧场景为 scene_id):", scene_result)


def cmd_process(args, llm_process_job) -> None:
    out = llm_process_job(_demo_job(), scene_id=args.scene_id)
    print("llm_process_job 返回:")
    print(out)


def cmd_all(args, llm_identify_scene, llm_process_job) -> None:
    cmd_identify(args, llm_identify_scene)
    print("-" * 60)
    cmd_process(args, llm_process_job)


def main() -> int:
    p = argparse.ArgumentParser(description="直连测试 DashScope 文本模型 / 场景 LLM")
    p.add_argument(
        "--new",
        action="store_true",
        help="使用 services.llm_services（新版）；默认 legacy",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    p_id = sub.add_parser("identify", help="llm_identify_scene（可选读文件）")
    p_id.add_argument("--text", type=str, default="", help="自然语言求职描述")
    p_id.add_argument("--file", type=str, default="", help="txt 路径，与 create_scene_from_txt 一致")
    p_id.add_argument(
        "--print-prompt",
        action="store_true",
        help="调用前打印 _SCENE_USER_TMPL 拼装全文（需已有场景时才有场景匹配提示）",
    )

    p_pr = sub.add_parser("process", help="llm_process_job（内置一条假岗位）")
    p_pr.add_argument(
        "--scene-id",
        type=int,
        default=None,
        help="传给 llm_process_job 的 scene_id（新版用于招呼语；legacy 可忽略）",
    )

    p_all = sub.add_parser("all", help="先 identify 再 process")
    p_all.add_argument("--text", type=str, default="")
    p_all.add_argument("--file", type=str, default="")
    p_all.add_argument("--scene-id", type=int, default=None)

    args = p.parse_args()
    llm_identify_scene, llm_process_job = _load_llm_funcs(args.new)
    if args.cmd != "identify" and getattr(args, "print_prompt", False):
        raise SystemExit("--print-prompt 仅用于 identify 子命令")

    if args.cmd == "identify":
        cmd_identify(args, llm_identify_scene)
    elif args.cmd == "process":
        cmd_process(args, llm_process_job)
    else:
        cmd_all(args, llm_identify_scene, llm_process_job)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
