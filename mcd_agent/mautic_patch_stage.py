"""Isolated target-source acceptance before mutating an installation."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
from typing import Any, Callable

from mcd_agent.mautic_patch_plan_v3 import (
    PatchPlanV3Error, _file, _read, _root, _simulate, _validate_plan,
)
from mcd_agent.mautic_patch_resolution import canonical_json_sha256


def _is_composer_project_root(root: Path) -> bool:
    """Return whether root is a Composer project wrapper for a split install."""
    return (
        (root / "composer.json").is_file()
        and not (root / "composer.json").is_symlink()
        and (root / "bin/console").is_file()
        and not (root / "bin/console").is_symlink()
    )


def _is_upgrade_only_directory(path: Path) -> bool:
    """Recognize the stale root/app marker left beside a Composer docroot."""
    if path.is_symlink() or not path.is_dir():
        return False
    try:
        entries = list(path.iterdir())
    except OSError:
        return False
    return (
        len(entries) == 1
        and entries[0].name == "upgrade"
        and entries[0].is_dir()
        and not entries[0].is_symlink()
    )


def _is_stale_composer_application_candidate(root: Path) -> bool:
    """Ignore only the known Composer wrapper residue, never a real root layout."""
    if not _is_composer_project_root(root):
        return False
    plugins = root / "plugins"
    if plugins.is_symlink() or not plugins.is_dir():
        return False
    try:
        plugins_empty = not any(plugins.iterdir())
    except OSError:
        return False
    return _is_upgrade_only_directory(root / "app") and plugins_empty


def application_root(value: str | Path) -> Path:
    root = _root(str(value))
    if (root.name in {"docroot", "public"}
            and (root / "app").is_dir() and (root / "plugins").is_dir()
            and not (root / "bin/console").exists()
            and (root.parent / "bin/console").is_file()):
        root = _root(str(root.parent))
    candidates = []
    for relative in ("", "docroot", "public"):
        candidate = root / relative
        if candidate.is_symlink():
            raise PatchPlanV3Error("application_root_symlink")
        if not relative and _is_stale_composer_application_candidate(candidate):
            continue
        console_root = candidate
        if relative and not (candidate / "bin/console").exists():
            console_root = root
        if (candidate / "app").is_dir() and (candidate / "plugins").is_dir() and (console_root / "bin/console").is_file():
            for anchor in ("app", "plugins"):
                _file(candidate, anchor)
            for anchor in ("bin", "bin/console"):
                _file(console_root, anchor)
            candidates.append(candidate)
    if len(candidates) != 1:
        raise PatchPlanV3Error("application_root_missing_or_ambiguous")
    return candidates[0]


class TargetStage:
    def __init__(self, source: str, plan: dict[str, Any], prepare: Callable[[Path], None],
                 read_version: Callable[[Path], str | None], root_binding: Callable[[Path], Path] | None = None, facts_provider=None,
                 defer_conditions: bool = False):
        self.plan_sha256 = _validate_plan(plan)
        self.target_version = plan["target_version"]
        self.directory = Path(tempfile.mkdtemp(prefix="mcd-target-stage-"))
        self.root = self.directory / "source"
        self.hashes: dict[str, str | None] = {}
        self.original_hashes: dict[str, str | None] = {}
        self.original_composer_hashes: dict[str, str | None] = {}
        self.root_binding = root_binding or (lambda root: root)
        self.composer_files: dict[str, bytes] = {}
        self.facts_provider = facts_provider
        self.facts_receipt = None
        try:
            from mcd_agent.mautic_patch_plan_v3 import _observe_facts
            if not defer_conditions:
                self.facts_receipt = _observe_facts(plan, facts_provider, "verify", plan["phase"])
            original = _root(source)
            if read_version(original) != plan["source_version"]:
                raise PatchPlanV3Error("target_stage_source_version_mismatch")
            original_application = self.root_binding(original)
            tracked = list(dict.fromkeys(path for record in plan["patches"] for path in record["source_paths"]))
            for path in tracked:
                content, _st = _read(_file(original_application, path))
                self.original_hashes[path] = hashlib.sha256(content).hexdigest() if content is not None else None
            for path in ("composer.json", "composer.lock"):
                content, _st = _read(_file(original, path))
                self.original_composer_hashes[path] = hashlib.sha256(content).hexdigest() if content is not None else None
            shutil.copytree(_root(source), self.root, symlinks=True,
                            ignore=shutil.ignore_patterns(".mcd", ".git", "node_modules", "cache", "logs", "log"))
            for directory, dirs, files in os.walk(self.root, followlinks=False):
                for name in dirs + files:
                    path = Path(directory) / name
                    if path.is_symlink() and not path.resolve().is_relative_to(self.root):
                        raise PatchPlanV3Error("target_stage_external_symlink")
            prepare(self.root)
            if read_version(self.root) != self.target_version:
                raise PatchPlanV3Error("target_stage_version_mismatch")
            staged_application = self.root_binding(self.root)
            self.application_root_relative = staged_application.relative_to(self.root).as_posix()
            paths = list(dict.fromkeys(path for record in plan["patches"] for path in record["source_paths"]))
            for path in paths:
                content, _st = _read(_file(staged_application, path))
                self.hashes[path] = hashlib.sha256(content).hexdigest() if content is not None else None
            simulation_plan = plan
            if defer_conditions:
                simulation_plan = dict(plan, patches=[record for record in plan["patches"]
                                                       if not record.get("preconditions")])
            preview, self.outcomes, _final = _simulate(staged_application, simulation_plan, facts=self.facts_receipt)
            shutil.rmtree(preview, ignore_errors=True)
            for name in ("composer.json", "composer.lock"):
                path = self.root / name
                if path.is_file() and not path.is_symlink():
                    self.composer_files[name] = path.read_bytes()
            self.target_source_sha256 = canonical_json_sha256({"target_version": self.target_version, "files": self.hashes})
        except Exception:
            self.close()
            raise

    def verify_original(self, root: str, plan: dict[str, Any], read_version: Callable[[Path], str | None]) -> None:
        self.verify_facts(plan)
        live = _root(root)
        if canonical_json_sha256(plan) != self.plan_sha256 or read_version(live) != plan["source_version"]:
            raise PatchPlanV3Error("target_stage_source_binding_changed")
        live_application = self.root_binding(live)
        for path, expected in self.original_hashes.items():
            content, _st = _read(_file(live_application, path))
            actual = hashlib.sha256(content).hexdigest() if content is not None else None
            if actual != expected:
                raise PatchPlanV3Error("target_stage_source_changed_before_mutation")
        for path, expected in self.original_composer_hashes.items():
            content, _st = _read(_file(live, path))
            if (hashlib.sha256(content).hexdigest() if content is not None else None) != expected:
                raise PatchPlanV3Error("target_stage_composer_changed_before_mutation")

    def verify_live(self, root: str, plan: dict[str, Any], read_version: Callable[[Path], str | None]) -> dict[str, Any]:
        self.verify_facts(plan)
        if canonical_json_sha256(plan) != self.plan_sha256:
            raise PatchPlanV3Error("target_stage_plan_changed")
        live = _root(root)
        if read_version(live) != self.target_version:
            raise PatchPlanV3Error("target_live_version_mismatch")
        live_application = self.root_binding(live)
        if live_application.relative_to(live).as_posix() != self.application_root_relative:
            raise PatchPlanV3Error("target_live_application_layout_mismatch")
        for path, digest in self.hashes.items():
            content, _st = _read(_file(live_application, path))
            actual = hashlib.sha256(content).hexdigest() if content is not None else None
            if actual != digest:
                print("MCD_PATCH_TARGET_EVIDENCE=" + json.dumps({
                    "schema": "mcd-mautic-target-stage-evidence-v1",
                    "status": "failed",
                    "reason": "target_live_source_hash_mismatch",
                    "plan_sha256": self.plan_sha256,
                    "target_version": self.target_version,
                    "application_root_relative": self.application_root_relative,
                    "source_path": path,
                    "expected_sha256": digest,
                    "actual_sha256": actual,
                }, ensure_ascii=True, sort_keys=True, separators=(",", ":")), flush=True)
                raise PatchPlanV3Error("target_live_source_hash_mismatch")
        return {"schema": "mcd-mautic-target-stage-evidence-v1", "status": "success",
                "plan_sha256": self.plan_sha256, "target_version": self.target_version,
                "target_package_sha256": getattr(self, "target_package_sha256", None),
                "application_root_relative": self.application_root_relative,
                "target_source_sha256": self.target_source_sha256, "source_hashes": self.hashes}

    def verify_facts(self, plan) -> None:
        if self.facts_receipt is not None:
            from mcd_agent.mautic_patch_fact_binding import require_receipt
            from mcd_agent.mautic_patch_plan_v3 import _observe_facts
            require_receipt(self.facts_receipt, _observe_facts(plan, self.facts_provider, "apply", plan["phase"]))

    def close(self) -> None:
        shutil.rmtree(self.directory, ignore_errors=True)
