# -*- coding: utf-8 -*-
"""
多平台、多场景列表爬取断点（checkpoint.json）通用读写。

猎聘示例（无 version）：
{
  "liepin": {
    "platform": "liepin",
    "scenes": {
      "6": {"last_page": 5},
      "7": {"last_page": 0}
    }
  }
}

根下各 key 为平台标识（如 liepin）；每块含 "platform" 与 "scenes"。
其它平台可并列写入同一文件，读写时指定 platform 即可。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional

from config import log

_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CHECKPOINT_PATH = _ROOT / "data" / "checkpoint.json"
_SCENES_KEY = "scenes"
_DEFAULT_PLATFORM = "liepin"


def checkpoint_path(path: Optional[Path] = None) -> Path:
    """断点文件路径；传入 path 时用于测试覆盖。"""
    return path if path is not None else DEFAULT_CHECKPOINT_PATH


def _normalize_scenes_dict(scenes_raw: Any) -> Dict[str, Dict[str, int]]:
    out: Dict[str, Dict[str, int]] = {}
    if not isinstance(scenes_raw, dict):
        return out
    for k, v in scenes_raw.items():
        if v is not None and isinstance(v, dict) and "last_page" in v:
            try:
                out[str(int(k))] = {"last_page": int(v["last_page"])}
            except (TypeError, ValueError):
                continue
    return out


def _parse_root(raw: Dict[str, Any]) -> Dict[str, Any]:
    """仅接受根下各平台块：{ platform_key: { platform, scenes } }。"""
    out: Dict[str, Any] = {}
    for pk, block in raw.items():
        if not isinstance(block, dict) or _SCENES_KEY not in block:
            continue
        if not isinstance(block.get(_SCENES_KEY), dict):
            continue
        plat = str(block.get("platform", pk))
        scenes = _normalize_scenes_dict(block[_SCENES_KEY])
        out[str(pk)] = {"platform": plat, _SCENES_KEY: scenes}
    return out


def load_checkpoint_document(path: Optional[Path] = None) -> Dict[str, Any]:
    """读取 checkpoint 全文（多平台根对象）；结构不合法则返回 {}。"""
    p = checkpoint_path(path)
    if not p.exists():
        return {}
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return {}
        return _parse_root(raw)
    except (OSError, json.JSONDecodeError) as e:
        log.warning("checkpoint 读取失败，按空处理: %s", e)
        return {}


def _save_root(root: Dict[str, Any], path: Optional[Path] = None) -> None:
    p = checkpoint_path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    clean: Dict[str, Any] = {}
    for pk, block in root.items():
        if not isinstance(block, dict):
            continue
        plat = str(block.get("platform", pk))
        scenes = _normalize_scenes_dict(block.get(_SCENES_KEY, {}))
        if not scenes:
            continue
        clean[str(pk)] = {"platform": plat, _SCENES_KEY: scenes}
    if not clean:
        try:
            p.unlink(missing_ok=True)
        except OSError:
            pass
        return
    p.write_text(json.dumps(clean, ensure_ascii=False, indent=2), encoding="utf-8")


def _get_platform_block(root: Dict[str, Any], platform: str) -> Dict[str, Any]:
    key = str(platform)
    block = root.get(key)
    if isinstance(block, dict) and _SCENES_KEY in block:
        return block
    return {"platform": key, _SCENES_KEY: {}}


def get_scene_last_done_page(
    scene_id: int,
    *,
    platform: str = _DEFAULT_PLATFORM,
    path: Optional[Path] = None,
) -> Optional[int]:
    """若该平台 checkpoint 中存在该 scene_id，返回 last_page；否则 None。"""
    root = load_checkpoint_document(path)
    scenes = _get_platform_block(root, platform).get(_SCENES_KEY, {})
    entry = scenes.get(str(int(scene_id)))
    if not entry:
        return None
    try:
        return int(entry["last_page"])
    except (KeyError, TypeError, ValueError):
        return None


def get_resume_list_page_index(
    scene_id: int,
    *,
    reset: bool = False,
    platform: str = _DEFAULT_PLATFORM,
    path: Optional[Path] = None,
) -> int:
    """
    下一列表页起始索引（从 0 起）。reset=True：清除该平台下该场景并返回 0。
    无记录返回 0；有记录返回 last_page + 1。
    """
    if reset:
        remove_scene_checkpoint(scene_id, platform=platform, path=path)
        log.info(
            "已清除平台 %s 场景 %s 的断点，从列表页索引 0 开始",
            platform,
            scene_id,
        )
        return 0
    last_done = get_scene_last_done_page(scene_id, platform=platform, path=path)
    if last_done is None:
        return 0
    nxt = last_done + 1
    log.info(
        "断点续爬[平台 %s 场景 %s]：上次已完成至列表页索引 %s，本次从索引 %s 开始",
        platform,
        scene_id,
        last_done,
        nxt,
    )
    return nxt


def set_scene_last_page(
    scene_id: int,
    last_page: int,
    *,
    platform: str = _DEFAULT_PLATFORM,
    path: Optional[Path] = None,
) -> None:
    """合并写入：更新该平台下该场景的 last_page。"""
    root = load_checkpoint_document(path)
    key = str(platform)
    block = dict(_get_platform_block(root, platform))
    scenes: Dict[str, Any] = dict(block.get(_SCENES_KEY, {}))
    scenes[str(int(scene_id))] = {"last_page": int(last_page)}
    root[key] = {"platform": key, _SCENES_KEY: scenes}
    _save_root(root, path)
    log.info(
        "断点已写入 %s 平台=%s 场景=%s last_page=%s",
        checkpoint_path(path),
        platform,
        scene_id,
        last_page,
    )


def remove_scene_checkpoint(
    scene_id: int,
    *,
    platform: str = _DEFAULT_PLATFORM,
    path: Optional[Path] = None,
) -> None:
    """移除该平台下指定场景；该平台无场景后移除该平台块；根为空则删文件。"""
    root = load_checkpoint_document(path)
    key = str(platform)
    block = dict(_get_platform_block(root, platform))
    scenes: Dict[str, Any] = dict(block.get(_SCENES_KEY, {}))
    sk = str(int(scene_id))
    if sk not in scenes:
        return
    del scenes[sk]
    p = checkpoint_path(path)
    if not scenes:
        root.pop(key, None)
    else:
        root[key] = {"platform": key, _SCENES_KEY: scenes}
    if not root:
        try:
            p.unlink(missing_ok=True)
        except OSError as e:
            log.debug("删除空 checkpoint 文件失败: %s", e)
        log.info("断点文件已删除（无任何平台记录）: %s", p)
        return
    _save_root(root, path)
    log.debug("已移除平台 %s 场景 %s 的断点记录: %s", platform, scene_id, p)
