import logging
import subprocess
import time
from dataclasses import dataclass
from typing import Dict, List, Optional

from .instances import MauticInstance

logger = logging.getLogger(__name__)


@dataclass
class RunningProcess:
    instance_id: str
    kind: str
    ref_id: Optional[int]
    popen: subprocess.Popen
    started_at: float


class ProcessManager:
    """
    Минимальный менеджер процессов:
    - стартует команды bin/console
    - ограничивает параллельные segment:update
    - периодически чистит завершившиеся процессы
    """

    def __init__(self, max_segment_parallel: int) -> None:
        self.max_segment_parallel = max_segment_parallel
        self._running: List[RunningProcess] = []

    def poll(self) -> None:
        alive: List[RunningProcess] = []
        for proc in self._running:
            ret = proc.popen.poll()
            if ret is None:
                alive.append(proc)
            else:
                logger.info(
                    "Process finished: instance=%s kind=%s ref=%s exit=%s",
                    proc.instance_id,
                    proc.kind,
                    proc.ref_id,
                    ret,
                )
        self._running = alive

    def _count_segment_updates(self, instance_id: str) -> int:
        return sum(
            1
            for p in self._running
            if p.instance_id == instance_id and p.kind == "segment_update"
        )

    def can_start_segment_update(self, instance_id: str) -> bool:
        return self._count_segment_updates(instance_id) < self.max_segment_parallel

    def start_segment_update(self, instance: MauticInstance, segment_id: int) -> None:
        if not self.can_start_segment_update(instance.id):
            logger.debug(
                "Skip segment:update for instance %s, concurrency limit reached",
                instance.id,
            )
            return

        args = [
            instance.bin_console,
            "mautic:segment:update",
            str(segment_id),
            "--batch-limit=0",
        ]
        self._start(instance, "segment_update", segment_id, args)

    def start_campaign_rebuild(self, instance: MauticInstance, campaign_id: int) -> None:
        args = [
            instance.bin_console,
            "mautic:campaigns:rebuild",
            str(campaign_id),
        ]
        self._start(instance, "campaign_rebuild", campaign_id, args)

    def start_campaign_trigger(self, instance: MauticInstance, campaign_id: int) -> None:
        args = [
            instance.bin_console,
            "mautic:campaigns:trigger",
            str(campaign_id),
        ]
        self._start(instance, "campaign_trigger", campaign_id, args)

    def start_import(self, instance: MauticInstance) -> None:
        # Пока без ID – Mautic сам решает что запускать
        args = [
            instance.bin_console,
            "mautic:import",
        ]
        self._start(instance, "import", None, args)

    def _start(
        self,
        instance: MauticInstance,
        kind: str,
        ref_id: Optional[int],
        args: List[str],
    ) -> None:
        logger.info(
            "Starting %s (ref=%s) on instance %s: %s",
            kind,
            ref_id,
            instance.id,
            " ".join(args),
        )
        popen = subprocess.Popen(
            ["php"] + args,  # php перед bin/console
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self._running.append(
            RunningProcess(
                instance_id=instance.id,
                kind=kind,
                ref_id=ref_id,
                popen=popen,
                started_at=time.time(),
            )
        )
