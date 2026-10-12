from __future__ import annotations

from pathlib import Path

from mcd_agent import instance_migrate


def test_runtime_adapter_preflight_replaces_host_php_requirements(tmp_path: Path, monkeypatch) -> None:
    calls: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(instance_migrate, "_target_db_exists", lambda _name: False)
    monkeypatch.setattr(instance_migrate, "runtime_adapter_path", lambda name: tmp_path / name)
    monkeypatch.setattr(
        instance_migrate,
        "run_runtime_adapter",
        lambda name, *, operation, payload, timeout_sec: calls.append((operation, payload))
        or {"ok": True, "runtime": "docker"},
    )
    monkeypatch.setattr(
        instance_migrate.shutil,
        "which",
        lambda name: f"/usr/bin/{name}" if name in {"tar", "gzip"} else None,
    )

    result = instance_migrate.preflight_target_relay(
        target_root=str(tmp_path / "instances/newlook"),
        target_db_name="baza_newlook",
        runtime_adapter="mauticrfp-docker-v1",
        runtime="docker",
        install_type="composer",
        image_ref="playground7",
        domains_json='["newlook.example.com"]',
    )

    assert result["ok"] is True
    assert calls[0][0] == "target-preflight"
    assert calls[0][1]["runtime"] == "docker"
    assert calls[0][1]["install_type"] == "composer"
    assert calls[0][1]["image_ref"] == "playground7"


def test_runtime_adapter_finalizer_receives_database_and_layout(tmp_path: Path, monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_run(name, *, operation, payload, timeout_sec):
        captured.update(payload)
        return {"ok": True, "catchup_ok": True, "adapter": name, "operation": operation}

    monkeypatch.setattr(instance_migrate, "run_runtime_adapter", fake_run)
    result = instance_migrate.finalize_target_relay(
        type("Config", (), {"state_db_path": str(tmp_path / "state.db")})(),
        target_root=str(tmp_path / "instances/newlook"),
        target_db_name="baza_newlook",
        target_db_user="korisnik_newlook",
        target_db_password="secret",
        domains_json='["newlook.example.com"]',
        runtime_adapter="mauticrfp-docker-v1",
        runtime="docker",
        image_ref="playground7",
        install_type="composer",
    )

    assert result["ok"] is True
    assert captured["target_db_password"] == "secret"
    assert captured["runtime"] == "docker"
    assert captured["install_type"] == "composer"


def test_runtime_adapter_failure_never_wipes_target(tmp_path: Path, monkeypatch) -> None:
    target = tmp_path / "instances/newlook"
    target.mkdir(parents=True)
    sentinel = target / "keep.txt"
    sentinel.write_text("keep", encoding="utf-8")
    monkeypatch.setattr(instance_migrate, "runtime_adapter_path", lambda name: tmp_path / name)
    monkeypatch.setattr(
        instance_migrate,
        "run_runtime_adapter",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("image missing")),
    )
    monkeypatch.setattr(instance_migrate.shutil, "which", lambda name: f"/usr/bin/{name}")

    result = instance_migrate.preflight_target_relay(
        target_root=str(target),
        target_db_name="baza_newlook",
        wipe_target_root=True,
        runtime_adapter="mauticrfp-docker-v1",
        runtime="docker",
        install_type="composer",
        image_ref="missing-image",
        domains_json='["newlook.example.com"]',
    )

    assert result["ok"] is False
    assert sentinel.read_text(encoding="utf-8") == "keep"
    assert result["cleanup"] == []
