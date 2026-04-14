import os
from pathlib import Path
from typing import Optional
import yaml

from core.config import CONFIG_SEARCH_PATHS


# ============================================================
# 配置文件写入工具
# ============================================================


def _get_config_path() -> Optional[Path]:
    """获取当前使用的配置文件路径"""
    # 优先使用环境变量指定的路径
    env_path = os.environ.get("PILIPILI_CONFIG")
    if env_path:
        p = Path(env_path)
        if p.exists():
            return p

    # 搜索默认路径
    for path in CONFIG_SEARCH_PATHS:
        if path.exists():
            return path

    # 如果都不存在，返回默认写入路径
    default = Path("./configs/config.yaml")
    default.parent.mkdir(parents=True, exist_ok=True)
    return default


def _write_config_updates(updates: dict) -> None:
    """
    将扁平化的 key=value 更新写入 config.yaml。
    updates 格式: {"llm.deepseek.api_key": "sk-xxx", "tts.api_key": "sk-yyy"}
    支持最多 3 层嵌套路径。
    """
    config_path = _get_config_path()

    # 读取现有内容
    if config_path and config_path.exists():
        with open(config_path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
    else:
        raw = {}

    # 将扁平 key 写入嵌套 dict
    for dotted_key, value in updates.items():
        parts = dotted_key.split(".")
        d = raw
        for part in parts[:-1]:
            if part not in d or not isinstance(d[part], dict):
                d[part] = {}
            d = d[part]
        d[parts[-1]] = value

    # 写回文件
    if config_path:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        with open(config_path, "w", encoding="utf-8") as f:
            yaml.dump(raw, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
