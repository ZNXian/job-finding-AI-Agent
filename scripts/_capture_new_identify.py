# -*- coding: utf-8 -*-
"""一次性：新版 llm_services 用 test_scene2.txt 跑 llm_identify_scene，结果写入 UTF-8 文本。"""
import json
import os
import sys

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)

from utils.files import read_and_clean_txt
from services.scences import scene_manager
from services import llm_services as m

TXT = os.path.join(_ROOT, "test_scene2.txt")
OUT = os.path.join(_ROOT, "scripts", "_llm_new_identify_result.json")

def main() -> None:
    text = read_and_clean_txt(TXT)
    scenes = scene_manager.get_all_scenes()
    lines = []
    if scenes:
        scene_list = "\n".join(
            f"场景{s['scene_id']}：关键词={s['search_keywords']}, 城市={s['city']}, "
            f"省份={s.get('province', '')}, 远程={s['accept_remote']}, 薪资={s['min_salary']}-{s['max_salary']}"
            for s in scenes
        )
        prompt = m._SCENE_USER_TMPL.format(user_text=text, scene_list=scene_list)
        lines.append("=== _SCENE_USER_TMPL（含省份行）===")
        lines.append(prompt)
        lines.append("")
    is_new, scene_result = m.llm_identify_scene(text, scenes)
    lines.append(f"is_new_scene: {is_new}")
    if isinstance(scene_result, str):
        lines.append("scene_result (raw string):")
        lines.append(scene_result)
        try:
            parsed = json.loads(scene_result)
            lines.append("")
            lines.append("scene_result (pretty JSON):")
            lines.append(json.dumps(parsed, ensure_ascii=False, indent=2))
        except json.JSONDecodeError:
            pass
    else:
        lines.append(f"scene_result: {scene_result!r}")
    body = "\n".join(lines)
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(body)
    print("Wrote", OUT)


if __name__ == "__main__":
    main()
