import json
import sys
from types import SimpleNamespace

import pytest

from mcd_agent import cli


def _argv() -> list[str]:
    return [
        "mcd-cli",
        "mautic-image",
        "install",
        "--config",
        "/etc/mcd/mcd.toml",
        "--image-ref",
        "vanila7",
        "--domain",
        "fresh.example.invalid",
        "--php-version",
        "8.4",
        "--yes",
        "--json",
    ]


def _success_result() -> dict:
    return {
        "status": "ok",
        "domain": "fresh.example.invalid",
        "webroot": "/var/www/fresh/public_html",
        "db_name": "baza_fresh",
        "php_version": "8.4",
        "mail_mode": "external",
        "mail_profile_id": "",
        "instances": 1,
        "runtime_warmup": {
            "command": "cache:warmup",
            "environment": "prod",
            "debug": False,
            "runtime_user": "www-data",
            "php_bin": "/usr/bin/php8.4",
            "status": "ok",
        },
        "runtime_verification": {
            "status": "ok",
            "url": "https://fresh.example.invalid/s/login",
            "http_status": 200,
            "connect_address": "127.0.0.1",
            "tls_verified": True,
            "login_form": True,
            "body_sha256": "a" * 64,
        },
    }


def test_json_success_is_one_terminal_line_after_state_push(monkeypatch, capsys):
    cfg = SimpleNamespace()
    result = _success_result()
    events: list[str] = []
    before_receipt = {"stderr": ""}

    def install(*args, **kwargs):
        events.append("install")
        print("installer progress")
        return result

    def push(*args, **kwargs):
        events.append("push")
        captured = capsys.readouterr()
        assert captured.out == ""
        before_receipt["stderr"] = captured.err

    monkeypatch.setattr(sys, "argv", _argv())
    monkeypatch.setattr(cli, "load_config", lambda *args, **kwargs: cfg)
    monkeypatch.setattr(cli, "maybe_notify_update", lambda *args, **kwargs: "upgrade available")
    monkeypatch.setattr(cli, "install_from_image", install)
    monkeypatch.setattr(cli, "_push_state_after_change", push)

    assert cli.main() == 0
    captured = capsys.readouterr()
    lines = captured.out.splitlines()
    assert events == ["install", "push"]
    assert len(lines) == 1
    assert json.loads(lines[0]) == result
    assert lines[0] == json.dumps(
        result,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    diagnostics = before_receipt["stderr"] + captured.err
    assert "NOTICE: upgrade available" in diagnostics
    assert "installer progress" in diagnostics

    # MCC scans completed output lines backwards and accepts the final complete
    # JSON object. Multiline progress before it must not affect that boundary.
    mixed_lines = diagnostics.splitlines() + lines
    parsed = None
    for line in reversed(mixed_lines):
        candidate = line.strip()
        if not candidate.startswith("{"):
            continue
        try:
            value = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(value, dict):
            parsed = value
            break
    assert parsed == result


def test_json_failure_emits_no_success_receipt_or_state_push(monkeypatch, capsys):
    cfg = SimpleNamespace()
    pushed: list[bool] = []

    def fail(*args, **kwargs):
        print("installer progress before failure")
        raise RuntimeError("install failed")

    monkeypatch.setattr(sys, "argv", _argv())
    monkeypatch.setattr(cli, "load_config", lambda *args, **kwargs: cfg)
    monkeypatch.setattr(cli, "maybe_notify_update", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli, "install_from_image", fail)
    monkeypatch.setattr(cli, "_push_state_after_change", lambda *args, **kwargs: pushed.append(True))

    with pytest.raises(RuntimeError, match="install failed"):
        cli.main()
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "installer progress before failure" in captured.err
    assert pushed == []
