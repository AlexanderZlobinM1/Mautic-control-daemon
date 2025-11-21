import logging
import time
from typing import Dict, List

import pymysql

from .config import load_config
from .executor import ProcessManager
from .instances import MauticInstance, discover_instances


logger = logging.getLogger(__name__)


def run_daemon() -> None:
    cfg = load_config()
    _setup_logging(cfg.get("log_level", "INFO"))

    logger.info("Mautic Control Daemon starting")

    instances = discover_instances(cfg)
    if not instances:
        logger.warning("No Mautic instances discovered, exiting")
        return

    loop_interval = int(cfg.get("loop_interval_seconds", 10))
    pm = ProcessManager(
        max_segment_parallel=int(cfg.get("segment", {}).get("max_parallel_updates", 2))
    )

    while True:
        pm.poll()

        for inst in instances:
            try:
                _process_instance(inst, cfg, pm)
            except Exception as exc:
                logger.exception("Error while processing instance %s: %s", inst.id, exc)

        time.sleep(loop_interval)


def _setup_logging(level_name: str) -> None:
    level = getattr(logging, level_name.upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )


def _get_db_connection(inst: MauticInstance):
    return pymysql.connect(
        host=inst.db.host,
        user=inst.db.user,
        password=inst.db.password,
        database=inst.db.name,
        port=inst.db.port,
        charset="utf8mb4",
        autocommit=True,
        cursorclass=pymysql.cursors.DictCursor,
    )


def _process_instance(
    inst: MauticInstance, cfg: Dict, pm: ProcessManager
) -> None:
    """
    Здесь минимальный каркас:
    - подключение к БД
    - вызовы заглушек, где дальше будем реализовывать твою логику приоритетов.
    """
    logger.debug("Processing instance %s", inst.id)

    # Подключение к БД (одно на итерацию; при желании можно кешировать)
    conn = _get_db_connection(inst)
    try:
        with conn.cursor() as cur:
            # TODO: реализация логики по шагам 1–7 из твоего описания.
            # Сейчас только каркас вызовов – ничего не дергает без явной логики.
            #
            # Примеры мест, где будем работать:
            #
            # _handle_campaign_triggers(inst, cur, cfg, pm)
            # _handle_imports(inst, cur, cfg, pm)
            # _handle_background_segments(inst, cur, cfg, pm)
            pass
    finally:
        conn.close()
