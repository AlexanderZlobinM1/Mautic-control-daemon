import os
from typing import Any, Dict

import yaml


DEFAULT_CONFIG: Dict[str, Any] = {
    "php_binary": "php",
    "search_paths": ["/var/www"],
    "loop_interval_seconds": 10,
    "segment": {
        "max_parallel_updates": 2,
        "stale_after_minutes": 30,
    },
    "log_level": "INFO",
}


def load_config(path: str = "config/daemon.yml") -> Dict[str, Any]:
    cfg = DEFAULT_CONFIG.copy()

    if os.path.isfile(path):
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        cfg = _deep_merge(cfg, data)

    return cfg


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    result = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result
