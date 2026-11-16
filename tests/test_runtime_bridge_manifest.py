from __future__ import annotations

import importlib.util
import hashlib
from pathlib import Path

import pytest



_MODULE_PATH = Path(__file__).parents[1] / "tools" / "build-runtime-bridge-manifest.py"
_SPEC = importlib.util.spec_from_file_location("build_runtime_bridge_manifest", _MODULE_PATH)
assert _SPEC is not None and _SPEC.loader is not None
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)
build_manifest = _MODULE.build_manifest


ARCHIVE_SHA = "0f2938faeb8d5f7aa77f652800cfe5e650cb94b58bc42e281c64b2385f5cb223"
RUNTIME_MANIFEST_SHA = "03b86f46ef24502b397a94b26b7c11772dbf06e59eca9f8c3d7d02c905cc741f"
QA_SHA = "fea0fdff0be289aaef264340a07eadcdde6622d79f4cb09b52325293c084fb79"
SOURCE_COMMIT = "10db1785b84a612b7ebc1bb827010cd2cd2d1062"


def _kwargs() -> dict[str, str]:
    return {
        "version": "1.6.0",
        "source_commit": SOURCE_COMMIT,
        "artifact_name": "mcd-runtime-1.6.0.bridge.py",
        "artifact_url": "https://servercontrol.sales-snap.com/mauticctl/mcd/mcd-runtime-1.6.0.bridge.py",
        "runtime_archive_sha256": ARCHIVE_SHA,
        "runtime_manifest_sha256": RUNTIME_MANIFEST_SHA,
        "qa_sidecar_sha256": QA_SHA,
    }


def test_bridge_manifest_binds_current_release(tmp_path: Path) -> None:
    bridge = tmp_path / "mcd_runtime_bootstrap.py"
    bridge.write_bytes(b"bridge-v1\n")
    manifest = build_manifest(bridge, **_kwargs())

    assert manifest["schema"] == "mcd-runtime-bridge-package-v1"
    assert manifest["entrypoint"] == "mcd_runtime_bootstrap.py"
    assert manifest["source_commit"] == SOURCE_COMMIT
    assert manifest["runtime_release"] == {
        "archive_sha256": ARCHIVE_SHA,
        "manifest_sha256": RUNTIME_MANIFEST_SHA,
        "qa_sidecar_sha256": QA_SHA,
    }


def test_current_bridge_source_digest_is_bound_from_exact_transported_bytes() -> None:
    bridge = Path(__file__).parents[1] / "tools" / "mcd_runtime_bootstrap.py"
    manifest = build_manifest(
        bridge,
        artifact_name="mcd-runtime-1.6.0.bridge.py",
        artifact_url="https://servercontrol.sales-snap.com/mauticctl/mcd/mcd-runtime-1.6.0.bridge.py",
        **{key: value for key, value in _kwargs().items() if key not in {"artifact_name", "artifact_url"}},
    )
    assert manifest["artifact_sha256"] == hashlib.sha256(bridge.read_bytes()).hexdigest()


def test_bridge_revision_keeps_failed_immutable_artifact_untouched(tmp_path: Path) -> None:
    bridge = tmp_path / "mcd_runtime_bootstrap.py"
    bridge.write_bytes(b"bridge-v2\n")
    manifest = build_manifest(
        bridge,
        **{
            **_kwargs(),
            "artifact_name": "mcd-runtime-1.6.0.bridge-r2.py",
            "artifact_url": "https://servercontrol.sales-snap.com/mauticctl/mcd/mcd-runtime-1.6.0.bridge-r2.py",
        },
    )
    assert manifest["artifact"] == "mcd-runtime-1.6.0.bridge-r2.py"


@pytest.mark.parametrize("field", ["runtime_archive_sha256", "runtime_manifest_sha256", "qa_sidecar_sha256"])
def test_bridge_manifest_rejects_unbound_runtime_digest(tmp_path: Path, field: str) -> None:
    bridge = tmp_path / "mcd_runtime_bootstrap.py"
    bridge.write_bytes(b"bridge-v1\n")
    values = _kwargs()
    values[field] = "not-a-sha"
    with pytest.raises(ValueError, match=field):
        build_manifest(bridge, **values)
