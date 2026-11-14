from pathlib import Path


def _split_config(tmp_path: Path) -> tuple[Path, Path]:
    entrypoint = tmp_path / "mcd.toml"
    local = tmp_path / "mcd.local.toml"
    entrypoint.write_text(
        '# MCD_CONFIG_ENTRYPOINT v1\n'
        '[include]\nfiles = ["missing-system.toml", "mcd.local.toml"]\n\n'
        '[profile]\nname = "passive"\n',
        encoding="utf-8",
    )
    local.write_text(
        '[mcc]\n'
        'url = "https://controller.example.invalid"\n'
        'token = "included-secret"\n'
        'host_name = "host.example.invalid"\n',
        encoding="utf-8",
    )
    return entrypoint, local


def test_bootstrap_recovery_reads_effective_split_mcc_section(tmp_path):
    from mcd_agent import config

    entrypoint, _ = _split_config(tmp_path)

    assert config._extract_mcc_bootstrap_from_config_path(entrypoint) == (
        "https://controller.example.invalid",
        "included-secret",
        "host.example.invalid",
    )


def test_missing_profile_artifact_recovers_with_included_bootstrap(tmp_path, monkeypatch):
    from mcd_agent import config

    entrypoint, _ = _split_config(tmp_path)
    calls = []

    def desired(url, payload, token, timeout_sec=12):
        calls.append((url, payload, token, timeout_sec))
        return {
            "status": "ok",
            "desired_config_toml": (
                '[profile]\nname = "custom"\n\n'
                '[mcc]\n'
                'url = "https://controller.example.invalid"\n'
                'token = "included-secret"\n'
                'host_name = "host.example.invalid"\n'
            ),
        }

    monkeypatch.setattr(config, "_post_json", desired)

    loaded = config.load_config(str(entrypoint))

    assert loaded.profile_name == "custom"
    assert loaded.mcc_url == "https://controller.example.invalid"
    assert loaded.mcc_token == "included-secret"
    assert len(calls) == 1
    assert calls[0][0] == "https://controller.example.invalid/api/v1/agent/config-desired"
    assert calls[0][2] == "included-secret"
    assert list(tmp_path.glob("mcd.toml.broken-*"))
