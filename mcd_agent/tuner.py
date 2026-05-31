from __future__ import annotations

import json
import subprocess
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mcd_agent.config import AgentConfig, load_config
from mcd_agent.db import MauticDB
from mcd_agent.discovery import discover_mautic
from mcd_agent.executor import execute_mautic_command_template


@dataclass(frozen=True)
class SegmentBenchRow:
    parallel: int
    elapsed_sec: float
    ok: int
    failed: int


def _pick_install(config: AgentConfig, root: str | None) -> Any:
    installs = discover_mautic(
        config.discovery_roots,
        config.exclude_path_contains,
        config.supported_mautic_majors,
        config.custom_instances,
    )
    if root:
        for inst in installs:
            if inst.root == root:
                return inst
        raise RuntimeError(f"install root not found: {root}")
    if not installs:
        raise RuntimeError("no mautic installs discovered")
    return installs[0]


def _bench_classic(config: AgentConfig, root: str) -> float:
    t0 = time.monotonic()
    rc, _ = execute_mautic_command_template(
        php_bin=config.php_bin,
        root=root,
        template="mautic:segments:update --batch-limit={batch_limit}",
        timeout_sec=config.command_timeout_sec,
        batch_limit=config.segment_batch_limit,
    )
    if rc != 0:
        raise RuntimeError(f"classic benchmark failed rc={rc}")
    return time.monotonic() - t0


def _bench_parallel_ids(config: AgentConfig, root: str, ids: list[int], parallel: int) -> SegmentBenchRow:
    t0 = time.monotonic()
    ok = 0
    failed = 0
    queue = list(ids)
    workers = max(1, parallel)
    with ThreadPoolExecutor(max_workers=workers) as ex:
        running: set[Any] = set()
        while queue or running:
            while queue and len(running) < workers:
                sid = queue.pop(0)
                fut = ex.submit(
                    execute_mautic_command_template,
                    php_bin=config.php_bin,
                    root=root,
                    template=config.cmd_segment_update_template,
                    timeout_sec=config.command_timeout_sec,
                    id=sid,
                    batch_limit=config.segment_batch_limit,
                )
                fut.sid = sid  # type: ignore[attr-defined]
                running.add(fut)
            if not running:
                break
            done, running = wait(running, return_when=FIRST_COMPLETED)
            for fut in done:
                rc, _ = fut.result()
                if rc == 0:
                    ok += 1
                else:
                    failed += 1
    return SegmentBenchRow(
        parallel=parallel,
        elapsed_sec=time.monotonic() - t0,
        ok=ok,
        failed=failed,
    )


def _update_runtime_value(config_path: str, key: str, value: int) -> None:
    p = Path(config_path)
    text = p.read_text(encoding="utf-8")
    lines = text.splitlines()
    out: list[str] = []
    in_runtime = False
    written = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            if in_runtime and not written:
                out.append(f"{key} = {value}")
                written = True
            in_runtime = stripped == "[runtime]"
            out.append(line)
            continue
        if in_runtime and stripped.startswith(f"{key} ="):
            out.append(f"{key} = {value}")
            written = True
            continue
        out.append(line)
    if not written:
        if not in_runtime:
            out.append("")
            out.append("[runtime]")
        out.append(f"{key} = {value}")
    p.write_text("\n".join(out) + "\n", encoding="utf-8")


def tune_segments(
    *,
    config_path: str,
    root: str | None,
    max_parallel: int,
    apply: bool,
    apply_priority: bool,
) -> dict[str, object]:
    cfg = load_config(config_path)
    inst = _pick_install(cfg, root)
    if not inst.db:
        raise RuntimeError("target install has no db config")
    db = MauticDB(inst.db)
    ids = db.fetch_ids(cfg.sql_segments_due, limit=10000)
    if not ids:
        raise RuntimeError("no segment ids to benchmark")

    classic_sec = _bench_classic(cfg, inst.root)

    rows: list[SegmentBenchRow] = []
    limit = max(1, max_parallel)
    for p in range(1, limit + 1):
        rows.append(_bench_parallel_ids(cfg, inst.root, ids, p))

    valid = [r for r in rows if r.failed == 0]
    best = min(valid, key=lambda r: r.elapsed_sec) if valid else min(rows, key=lambda r: r.elapsed_sec)

    applied: dict[str, int] = {}
    if apply:
        _update_runtime_value(config_path, "segment_regular_parallel_idle", best.parallel)
        applied["segment_regular_parallel_idle"] = best.parallel
        if apply_priority:
            _update_runtime_value(config_path, "segment_priority_parallel_idle", best.parallel)
            applied["segment_priority_parallel_idle"] = best.parallel

    payload: dict[str, object] = {
        "root": inst.root,
        "segments_count": len(ids),
        "classic_elapsed_sec": round(classic_sec, 3),
        "rows": [
            {
                "parallel": r.parallel,
                "elapsed_sec": round(r.elapsed_sec, 3),
                "ok": r.ok,
                "failed": r.failed,
            }
            for r in rows
        ],
        "recommended_parallel": best.parallel,
        "recommended_elapsed_sec": round(best.elapsed_sec, 3),
        "faster_than_classic": best.elapsed_sec < classic_sec,
        "applied": applied,
    }
    return payload


def format_tune_result(payload: dict[str, object]) -> str:
    return json.dumps(payload, indent=2, ensure_ascii=False)
