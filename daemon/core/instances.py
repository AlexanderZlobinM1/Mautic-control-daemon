import json
import logging
import os
import subprocess
from dataclasses import dataclass
from typing import Dict, List, Optional


logger = logging.getLogger(__name__)


@dataclass
class DbConfig:
    host: str
    name: str
    user: str
    password: str
    port: int = 3306


@dataclass
class MauticInstance:
    id: str
    root_path: str
    local_php: str
    bin_console: str
    db: DbConfig


def discover_instances(cfg: Dict) -> List[MauticInstance]:
    php_binary = cfg.get("php_binary", "php")
    search_paths = cfg.get("search_paths", ["/var/www"])

    instances: List[MauticInstance] = []

    for base in search_paths:
        if not os.path.isdir(base):
            continue

        for root, dirs, files in os.walk(base):
            # Ускоряем: не ходим в очень глубокие node_modules и т.п. при желании можно усложнить
            if "node_modules" in dirs:
                dirs.remove("node_modules")

            if "local.php" not in files:
                continue

            local_path = os.path.join(root, "local.php")

            # Поддерживаем оба варианта:
            #   /path/to/mautic/app/config/local.php
            #   /path/to/mautic/config/local.php
            bin_console = _guess_bin_console(local_path)
            if not bin_console:
                logger.warning("Skip %s: cannot find bin/console", local_path)
                continue

            db_conf = _load_db_config_from_local(local_path, php_binary)
            if not db_conf:
                logger.warning("Skip %s: cannot parse DB config", local_path)
                continue

            instance_id = f"{db_conf.name}@{db_conf.host}"
            inst = MauticInstance(
                id=instance_id,
                root_path=os.path.dirname(os.path.dirname(local_path))
                if "/app/config/" in local_path.replace("\\", "/")
                else os.path.dirname(local_path),
                local_php=local_path,
                bin_console=bin_console,
                db=db_conf,
            )
            instances.append(inst)
            logger.info("Discovered Mautic instance %s at %s", inst.id, inst.root_path)

    return instances


def _guess_bin_console(local_php_path: str) -> Optional[str]:
    local_php_path = os.path.abspath(local_php_path)
    path_norm = local_php_path.replace("\\", "/")

    if "/app/config/local.php" in path_norm:
        root = path_norm.rsplit("/app/config/local.php", 1)[0]
    elif "/config/local.php" in path_norm:
        root = path_norm.rsplit("/config/local.php", 1)[0]
    else:
        return None

    candidates = [
        os.path.join(root, "bin", "console"),
        os.path.join(root, "docroot", "bin", "console"),
    ]

    for c in candidates:
        if os.path.isfile(c) and os.access(c, os.X_OK):
            return c

    return None


def _load_db_config_from_local(local_php: str, php_binary: str) -> Optional[DbConfig]:
    """
    Вызывает php-cli, делает include local.php и отдаёт json_encode массива параметров.
    """
    code = f'echo json_encode(include "{local_php}");'

    try:
        proc = subprocess.run(
            [php_binary, "-r", code],
            check=True,
            text=True,
            capture_output=True,
        )
    except Exception as exc:
        logger.error("PHP failed for %s: %s", local_php, exc)
        return None

    stdout = proc.stdout.strip()
    if not stdout:
        logger.error("Empty PHP output for %s", local_php)
        return None

    try:
        data = json.loads(stdout)
    except json.JSONDecodeError as exc:
        logger.error("JSON decode error for %s: %s", local_php, exc)
        return None

    # Два возможных формата: 'db' => [..] или плоские ключи db_host/db_name/...
    db_block: Dict = {}
    if isinstance(data, dict) and "db" in data and isinstance(data["db"], dict):
        db_block = data["db"]
        host = db_block.get("host")
        name = db_block.get("dbname") or db_block.get("name")
        user = db_block.get("user") or db_block.get("username")
        password = db_block.get("password")
        port = int(db_block.get("port") or 3306)
    else:
        host = data.get("db_host")
        name = data.get("db_name")
        user = data.get("db_user")
        password = data.get("db_password")
        port = int(data.get("db_port") or 3306)

    if not all([host, name, user]) or password is None:
        logger.error("Incomplete DB config in %s", local_php)
        return None

    return DbConfig(host=host, name=name, user=user, password=password, port=port)
