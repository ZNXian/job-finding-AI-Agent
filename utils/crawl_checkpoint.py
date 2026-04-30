# -*- coding: utf-8 -*-
"""
多平台列表爬取断点（checkpoint.json）读写。

猎聘（liepin）场景结构（不兼容仅含 last_page 的旧版；当前 plan item 含 keyword，旧记录可自动升级）：

{
  "liepin": {
    "platform": "liepin",
    "scenes": {
      "7": {
        "plan": [
          { "city_code": "050140", "pubTime": 30, "keyword": "Python开发" },
          { "city_code": "050140", "pubTime": 30, "keyword": "后端开发" },
          { "city_code": "050090", "pubTime": 7, "keyword": "Python开发" }
        ],
        "segment_index": 0,
        "last_list_page": 3
      }
    }
  }
}

- plan: 本场景本次运行所需的全部 (city_code, pubTime, keyword) 子任务，顺序与列表爬取一致
- segment_index: 当前断点所在的子任务下标（0 起）
- last_list_page: 在 segment_index 对应 (city_code, pubTime, keyword) 下已完成的最后列表页 0 基下标；若尚未开爬该子任务则为 -1
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from config import log

_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CHECKPOINT_PATH = _ROOT / "data" / "checkpoint.json"
_SCENES_KEY = "scenes"
_DEFAULT_PLATFORM = "liepin"


def checkpoint_path(path: Optional[Path] = None) -> Path:
    """断点文件路径；传入 path 时用于测试覆盖。"""
    return path if path is not None else DEFAULT_CHECKPOINT_PATH


def _plan_entries_equal(
    a: List[Dict[str, Any]], b: List[Dict[str, Any]]
) -> bool:
    if not isinstance(a, list) or not isinstance(b, list) or len(a) != len(b):
        return False
    for x, y in zip(a, b):
        if not isinstance(x, dict) or not isinstance(y, dict):
            return False
        if str(x.get("city_code", "")) != str(y.get("city_code", "")):
            return False
        if int(x.get("pubTime", -1)) != int(y.get("pubTime", -1)):
            return False
        if str(x.get("keyword", "") or "").strip() != str(y.get("keyword", "") or "").strip():
            return False
    return True


def _plan_entries_is_prefix(old: List[Dict[str, Any]], new: List[Dict[str, Any]]) -> bool:
    """判断 old 是否为 new 的前缀（city_code/pubTime/keyword 均一致，且 old 更短）。"""
    if not isinstance(old, list) or not isinstance(new, list):
        return False
    if len(old) == 0 or len(old) > len(new):
        return False
    for x, y in zip(old, new):
        if not isinstance(x, dict) or not isinstance(y, dict):
            return False
        if str(x.get("city_code", "")) != str(y.get("city_code", "")):
            return False
        if int(x.get("pubTime", -1)) != int(y.get("pubTime", -1)):
            return False
        if str(x.get("keyword", "") or "").strip() != str(y.get("keyword", "") or "").strip():
            return False
    return True


def _plan_entries_equal_coarse_city_pubtime(
    a: List[Dict[str, Any]], b: List[Dict[str, Any]]
) -> bool:
    """仅按 city_code/pubTime 判断计划是否相等（用于旧断点升级，不考虑 keyword）。"""
    if not isinstance(a, list) or not isinstance(b, list) or len(a) != len(b):
        return False
    for x, y in zip(a, b):
        if not isinstance(x, dict) or not isinstance(y, dict):
            return False
        if str(x.get("city_code", "")) != str(y.get("city_code", "")):
            return False
        if int(x.get("pubTime", -1)) != int(y.get("pubTime", -1)):
            return False
    return True


def _is_valid_liepin_entry(v: Any) -> bool:
    if not isinstance(v, dict):
        return False
    p = v.get("plan")
    if not isinstance(p, list) or not p:
        return False
    for item in p:
        if not isinstance(item, dict):
            return False
        # keyword 允许缺失（旧断点会自动升级），但写回时会标准化为字符串
        if "city_code" not in item or "pubTime" not in item:
            return False
    if "segment_index" not in v or "last_list_page" not in v:
        return False
    try:
        int(v["segment_index"])
        int(v["last_list_page"])
    except (TypeError, ValueError):
        return False
    return True


def _normalize_liepin_scenes(scenes_raw: Any) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    if not isinstance(scenes_raw, dict):
        return out
    for k, v in scenes_raw.items():
        if v is not None and _is_valid_liepin_entry(v):
            try:
                sk = str(int(k))
            except (TypeError, ValueError):
                sk = str(k)
            p = v["plan"]
            if isinstance(p, list):
                plan: List[Dict[str, Any]] = []
                for it in p:
                    if not isinstance(it, dict):
                        continue
                    plan.append(
                        {
                            "city_code": str(it.get("city_code", "")).strip(),
                            "pubTime": int(it.get("pubTime", 0)),
                            "keyword": str(it.get("keyword", "") or "").strip(),
                        }
                    )
                out[sk] = {
                    "plan": plan,
                    "segment_index": int(v["segment_index"]),
                    "last_list_page": int(v["last_list_page"]),
                }
    return out


def _parse_root(raw: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for pk, block in raw.items():
        if not isinstance(block, dict) or _SCENES_KEY not in block:
            continue
        if not isinstance(block.get(_SCENES_KEY), dict):
            continue
        plat = str(block.get("platform", pk))
        if str(pk) == "liepin":
            scenes = _normalize_liepin_scenes(block[_SCENES_KEY])
        else:
            # AI 删除
            # 删除原因：本仓库现仅对 liepin 做断点；他平台可后续按相同模式扩展
            continue
        out[str(pk)] = {"platform": plat, _SCENES_KEY: scenes}
    return out


def load_checkpoint_document(path: Optional[Path] = None) -> Dict[str, Any]:
    """读取 checkpoint 全文；结构不合法则返回 {}。"""
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


def has_liepin_scene_checkpoint(scene_id: int, *, path: Optional[Path] = None) -> bool:
    """是否存在该 scene_id 的猎聘断点记录（存在即视为需要续爬/未完成）。"""
    try:
        root = load_checkpoint_document(path)
    except Exception:
        return False
    block = root.get(_DEFAULT_PLATFORM)
    if not isinstance(block, dict):
        return False
    scenes = block.get(_SCENES_KEY)
    if not isinstance(scenes, dict):
        return False
    return str(int(scene_id)) in scenes


def _save_root(root: Dict[str, Any], path: Optional[Path] = None) -> None:
    p = checkpoint_path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    clean: Dict[str, Any] = {}
    for pk, block in root.items():
        if not isinstance(block, dict):
            continue
        if str(pk) == "liepin":
            scenes = _normalize_liepin_scenes(block.get(_SCENES_KEY, {}))
            if not scenes:
                continue
        else:
            continue
        clean[str(pk)] = {"platform": "liepin", _SCENES_KEY: scenes}
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


def get_liepin_list_resume(
    scene_id: int,
    plan: List[Dict[str, Any]],
    *,
    reset: bool = False,
    path: Optional[Path] = None,
) -> Tuple[int, int]:
    """
    从断点得到本次猎聘列表爬起位置。
    :return: (segment_index, list_start) — 从 plan[segment] 的列表第 list_start 页起抓（0 基）；reset 或无效则 (0,0)。
    """
    if not plan:
        return 0, 0
    if reset:
        remove_scene_checkpoint(
            int(scene_id), platform=_DEFAULT_PLATFORM, path=path
        )
        log.info("已清除猎聘场景 %s 的断点，从子任务 0 页 0 起", scene_id)
        return 0, 0
    root = load_checkpoint_document(path)
    scenes = _get_platform_block(root, _DEFAULT_PLATFORM).get(_SCENES_KEY, {})
    entry = scenes.get(str(int(scene_id)))
    if not entry or not _is_valid_liepin_entry(entry):
        return 0, 0
    saved_plan = entry.get("plan", [])
    if not _plan_entries_equal(saved_plan, plan):
        # 兼容：当仅在末尾追加新的 segments（如 ACCEPT_REMOTE 后 pubTime=7 全集）时，不打断断点续爬
        if _plan_entries_is_prefix(saved_plan, plan):
            seg_keep = int(entry.get("segment_index", 0))
            last_keep = int(entry.get("last_list_page", -1))
            log.info(
                "断点 plan 为当前 plan 的前缀：自动升级 checkpoint.plan（保留 segment_index=%s last_list_page=%s）scene_id=%s",
                seg_keep,
                last_keep,
                scene_id,
            )
            try:
                set_liepin_list_checkpoint(
                    int(scene_id),
                    plan,
                    seg_keep,
                    last_keep,
                    path=path,
                )
            except Exception as e:
                log.warning("自动升级 checkpoint.plan 失败（将继续尝试续爬）: %s", e)
        else:
            # 兼容：旧 checkpoint.plan 不含 keyword 的场景（旧计划仅按 city_code/pubTime 分段）
            # 将旧 segment_index 映射到新 plan：new_segment_index = old_segment_index * keyword_count（默认对应该城市段的第一个 keyword）
            old_has_keyword = False
            if isinstance(saved_plan, list) and saved_plan:
                try:
                    old_has_keyword = any(isinstance(it, dict) and "keyword" in it for it in saved_plan)
                except Exception:
                    old_has_keyword = False
            if (not old_has_keyword) and isinstance(saved_plan, list) and isinstance(plan, list) and plan:
                # new plan 的 keyword_count：取第一个 city+pubTime 组合的连续条数
                first_city = str(plan[0].get("city_code") or "")
                first_pub = int(plan[0].get("pubTime", 30) or 30)
                kw_count = 0
                for it in plan:
                    if str(it.get("city_code") or "") == first_city and int(it.get("pubTime", 30) or 30) == first_pub:
                        kw_count += 1
                    else:
                        break
                kw_count = max(1, kw_count)
                # 将 new plan 压缩成 coarse 计划与旧计划比较
                coarse_new: List[Dict[str, Any]] = []
                i = 0
                while i < len(plan):
                    coarse_new.append({"city_code": plan[i].get("city_code"), "pubTime": plan[i].get("pubTime")})
                    i += kw_count
                if _plan_entries_equal_coarse_city_pubtime(saved_plan, coarse_new):
                    seg_keep = int(entry.get("segment_index", 0))
                    last_keep = int(entry.get("last_list_page", -1))
                    new_seg = max(0, seg_keep * kw_count)
                    log.info(
                        "旧断点 plan 不含 keyword：自动升级到 keyword 版 plan（segment_index %s→%s last_list_page=%s）scene_id=%s",
                        seg_keep,
                        new_seg,
                        last_keep,
                        scene_id,
                    )
                    try:
                        set_liepin_list_checkpoint(
                            int(scene_id),
                            plan,
                            new_seg,
                            last_keep,
                            path=path,
                        )
                    except Exception as e:
                        log.warning("旧断点自动升级失败（将继续尝试续爬）: %s", e)
                else:
                    log.info(
                        "断点中 plan 与当前场景构建不一致，从子任务 0 页 0 起: scene_id=%s",
                        scene_id,
                    )
                    return 0, 0
    seg = int(entry["segment_index"])
    if seg < 0 or seg >= len(plan):
        return 0, 0
    last = int(entry["last_list_page"])
    if last < 0:
        return seg, 0
    nxt = last + 1
    log.info(
        "断点续爬[猎聘 场景 %s]: plan 子任务 %s (city_code=%s pubTime=%s)，上次已至第 %s 页，本段从第 %s 页起",
        scene_id,
        seg,
        plan[seg].get("city_code"),
        plan[seg].get("pubTime"),
        last,
        nxt,
    )
    return seg, nxt


def set_liepin_list_checkpoint(
    scene_id: int,
    plan: List[Dict[str, Any]],
    segment_index: int,
    last_list_page: int,
    *,
    path: Optional[Path] = None,
) -> None:
    # AI 生成
    # 生成目的：写入当前 (segment_index) 在 plan 中对应的 city_code+pubTime 下已处理到的最后列表页
    if plan is None:
        return
    root = load_checkpoint_document(path)
    key = _DEFAULT_PLATFORM
    block = dict(_get_platform_block(root, key))
    scenes: Dict[str, Any] = dict(block.get(_SCENES_KEY, {}))
    p_norm: List[Dict[str, Any]] = []
    for it in plan:
        if isinstance(it, dict):
            p_norm.append(
                {
                    "city_code": str(it.get("city_code", "")).strip(),
                    "pubTime": int(it.get("pubTime", 0)),
                    "keyword": str(it.get("keyword", "") or "").strip(),
                }
            )
    scenes[str(int(scene_id))] = {
        "plan": p_norm,
        "segment_index": int(segment_index),
        "last_list_page": int(last_list_page),
    }
    root[key] = {"platform": key, _SCENES_KEY: scenes}
    _save_root(root, path)
    pseg = p_norm[segment_index] if 0 <= segment_index < len(p_norm) else {}
    log.info(
        "断点已写入 %s 猎聘 scene_id=%s segment_index=%s city_code=%s pubTime=%s last_list_page=%s",
        checkpoint_path(path),
        scene_id,
        segment_index,
        pseg.get("city_code"),
        pseg.get("pubTime"),
        last_list_page,
    )


def remove_scene_checkpoint(
    scene_id: int,
    *,
    platform: str = _DEFAULT_PLATFORM,
    path: Optional[Path] = None,
) -> None:
    if str(platform) != "liepin":
        return
    root = load_checkpoint_document(path)
    key = _DEFAULT_PLATFORM
    block = dict(_get_platform_block(root, key))
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
        log.info("断点文件已删除（无记录）: %s", p)
        return
    _save_root(root, path)
    log.debug("已移除猎聘场景 %s 的断点: %s", scene_id, p)
