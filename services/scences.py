# -*- coding:utf-8 -*-
# @CreatTime : 2026 11:00
# @Author : XZN
# 多场景管理类
import os
import json
import time
from pathlib import Path

# from fastapi import FastAPI, UploadFile, File
from typing import List, Dict, Optional,Union
from config import HISTORY_SCENE_PATH, log, SCENE_MAX_NUMBER


class SceneManager:
    """场景管理类，缓存场景数据避免重复读取文件"""
    def __init__(self):
        # 初始化时加载所有场景到内存
        log.info("=== SceneManager __init__ 被调用 ===")
        self.scenes: List[Dict] = []
        self._load_all_scenes()
        log.info("=== SceneManager __init__ 调用结束 ===")
        

    def _load_all_scenes(self) -> None:
        """
        私有方法：从文件加载所有场景到内存
        """
        if not os.path.exists(HISTORY_SCENE_PATH):
            log.error(f"载入历史场景失败: 找不到路径 {HISTORY_SCENE_PATH}")
            self.scenes = []
            return
        try:
            with open(HISTORY_SCENE_PATH, "r", encoding="utf-8") as f:
                self.scenes = json.load(f)
                log.info("载入历史场景成功")
                # log.info(self.scenes)
        except Exception as e:
            log.error(f"载入历史场景失败: {str(e)}")
            self.scenes = []

    def _save_to_file(self) -> None:
        """
        私有方法：将内存中的场景数据保存到文件
        """
        # 确保保存目录存在
        dir_path = os.path.dirname(HISTORY_SCENE_PATH)
        if dir_path and not os.path.exists(dir_path):
            os.makedirs(dir_path)

        # 最多保留SCENE_MAX_NUMBER个，按时间排序，删最旧
        if len(self.scenes) > SCENE_MAX_NUMBER:
            self.scenes = sorted(self.scenes, key=lambda x: x["update_time"], reverse=True)
            self.scenes = self.scenes[:SCENE_MAX_NUMBER]  # 改用配置常量而非硬编码5
            log.info("已删除最久不使用场景")

        with open(HISTORY_SCENE_PATH, "w", encoding="utf-8") as f:
            json.dump(self.scenes, f, ensure_ascii=False, indent=2)
        log.info("场景数据已保存到文件")

    def get_all_scenes(self) -> List[Dict]:
        """
        获取所有场景（从内存读取）
        :return: 场景列表
        """
        return self.scenes.copy()  # 返回副本避免外部修改内存数据

    def save_scene(self, new_scene: Dict) -> None:
        """
        保存/更新单个场景（先更新内存，再保存到文件）
        :param new_scene: 新场景数据
        """
        # 去重：如果scene_id已存在，替换原有场景
        self.scenes = [s for s in self.scenes if s.get("scene_id") != new_scene["scene_id"]]
        self.scenes.append(new_scene)

        # 保存到文件
        self._save_to_file()
        log.info(f"已保存场景: {new_scene.get('scene_id')}")

    def update_scene_from_ai(self, is_new: bool, standard_result: Union[str, int]) -> int:
        """
        根据AI输出更新场景
        :param is_new: 是否是新场景
        :param standard_result: AI返回的标准结果（新场景为JSON字符串，旧场景为scene_id）
        """
        if is_new:
            # 创建新场景
            try:
                standard_json = json.loads(standard_result)
                # 自动生成scene_id（取最大ID+1，避免长度计算的问题）
                max_id = max([s.get("scene_id", 0) for s in self.scenes], default=0)
                new_scene = {
                    "scene_id": max_id + 1,
                    "search_keywords": standard_json["search_keywords"],
                    "city": standard_json["city"],
                    "accept_remote": standard_json["accept_remote"],
                    "min_salary": standard_json["min_salary"],
                    "max_salary": standard_json["max_salary"],
                    "requirements": standard_json["requirements"],
                    "update_time": time.strftime("%Y-%m-%d %H:%M:%S")
                }
                self.save_scene(new_scene)
                log.info(f"已创建新场景: {new_scene['scene_id']}")
                self.refresh_scenes()
                return new_scene["scene_id"]
            except json.JSONDecodeError as e:
                log.error(f"解析新场景JSON失败: {str(e)}")
                raise

        else:
            target_scene = self.get_scene_by_id(standard_result)
            if target_scene:
                target_scene["update_time"] = time.strftime("%Y-%m-%d %H:%M:%S")
                self.save_scene(target_scene)  # 传入目标场景而非new_scene（修复原代码bug）
                log.info(f"已更新场景{standard_result}的最后修改时间")
                self.refresh_scenes()
                return target_scene["scene_id"]
            else:
                log.error(f"未找到要更新的场景: {standard_result}")
                raise

    def refresh_scenes(self) -> None:
        """
        手动刷新内存中的场景数据（从文件重新加载）
        """
        self._load_all_scenes()
        log.info("已手动刷新场景数据")

    def get_scene_by_id(self, scene_id: int) -> Optional[Dict]:
        """
        根据ID获取单个场景
        :param scene_id: 场景ID
        :return: 场景字典或None
        """
        log.info(f'场景有这些{self.scenes}')
        return next((s for s in self.scenes if s["scene_id"] == scene_id), None)

    def get_dynamic_jobconfig(self, scene_id: int):
        """
        根据场景ID获取动态配置
        """
        # self.refresh_scenes()
        scene = self.get_scene_by_id(scene_id)
        if not scene:
            raise Exception(f"场景 {scene_id} 不存在")

        REMOTE_KEYWORDS = ["远程", "居家", "灵活办公", "异地办公"]
        
        path = Path(__file__).resolve().parent.parent / "data"
        path.mkdir(parents=True, exist_ok=True)
        return {
            "SEARCH_KEYWORD": " ".join(scene["search_keywords"]),
            "PREFERRED_CITIES": [scene["city"]],
            "REMOTE_KEYWORDS": REMOTE_KEYWORDS if scene["accept_remote"] else [""],
            "REQUIRED_KEYWORDS": scene["search_keywords"],
            "MIN_SALARY": scene["min_salary"],
            "MAX_SALARY": scene["max_salary"],
            "MY_REQUIREMENT": "\n".join([f"{i + 1}. {item}" for i, item in enumerate(scene["requirements"])]),
            "MEMERY_FILE":os.path.join(path,f"memery_{scene_id}.json"),
            "CSV_FILE":   os.path.join(path,f"ai_job_matches_{scene_id}.csv"),
        }

# 全局唯一的场景管理器实例
scene_manager = SceneManager()
