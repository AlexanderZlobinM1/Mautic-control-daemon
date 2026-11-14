import json
import sys
from types import SimpleNamespace

import pytest

from mcd_agent import cli
from mcd_agent.instance_uid import (
    canonical_instance_uid,
    instance_uid_matches,
    normalize_instance_uid,
)
from mcd_agent.models import MauticInstall


def test_demov6_uid_is_bare_for_public_identity_and_legacy_input_normalizes() -> None:
    local = "demov6.sales-snap.com"
    host = "MauticFarm-02"
    canonical = "demov6.sales-snap.com@MauticFarm-02"

    assert canonical_instance_uid(canonical) == local
    assert normalize_instance_uid(local, host) == local
    assert normalize_instance_uid(canonical, host) == local
    assert instance_uid_matches(canonical, local, host)
    assert instance_uid_matches(local, local, host)


def test_same_dns_on_another_host_is_not_an_alias() -> None:
    with pytest.raises(ValueError, match="instance_uid_host_mismatch"):
        normalize_instance_uid(
            "demov6.sales-snap.com@MauticFarm-01", "MauticFarm-02"
        )
    assert not instance_uid_matches(
        "demov6.sales-snap.com@MauticFarm-01",
        "demov6.sales-snap.com",
        "MauticFarm-02",
    )


def test_legacy_unsuffixed_uid_remains_safe_on_current_host() -> None:
    assert instance_uid_matches(
        "demov6.sales-snap.com",
        "demov6.sales-snap.com",
        "MauticFarm-02",
    )


@pytest.mark.parametrize("selector", [
    "demov6.sales-snap.com",
    "demov6.sales-snap.com@MauticFarm-02",
])
def test_instances_list_accepts_bare_or_legacy_uid_and_emits_bare(selector, monkeypatch, capsys) -> None:
    row = MauticInstall(
        instance_uid="demov6.sales-snap.com@MauticFarm-02",
        name="demov6",
        root="/var/www/composer-demov6/public_html",
        console_path="/var/www/composer-demov6/public_html/bin/console",
        primary_domain="demov6.sales-snap.com",
    )

    class Inventory:
        def __init__(self, *args, **kwargs):
            pass

        def list_instances(self):
            return [row]

    monkeypatch.setattr(cli, "load_config", lambda *args, **kwargs: SimpleNamespace(state_db_path="unused"))
    monkeypatch.setattr(cli, "maybe_notify_update", lambda *args: None)
    monkeypatch.setattr(cli, "InstanceInventory", Inventory)
    monkeypatch.setattr(cli, "ensure_seeded", lambda *args: None)
    monkeypatch.setattr(
        cli,
        "resolve_agent_identity",
        lambda _cfg: {"effective_mcc_host_name": "MauticFarm-02"},
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "mcd-cli",
            "instances",
            "list",
            "--instance-uid",
            selector,
            "--json",
        ],
    )

    assert cli.main() == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload[0]["instance_uid"] == "demov6.sales-snap.com"
    assert payload[0]["root"] == row.root


def test_instances_list_rejects_qualified_uid_for_another_host(monkeypatch, capsys) -> None:
    row = MauticInstall(
        instance_uid="demov6.sales-snap.com",
        name="demov6",
        root="/var/www/composer-demov6/public_html",
        console_path="/var/www/composer-demov6/public_html/bin/console",
    )

    class Inventory:
        def __init__(self, *args, **kwargs):
            pass

        def list_instances(self):
            return [row]

    monkeypatch.setattr(cli, "load_config", lambda *args, **kwargs: SimpleNamespace(state_db_path="unused"))
    monkeypatch.setattr(cli, "maybe_notify_update", lambda *args: None)
    monkeypatch.setattr(cli, "InstanceInventory", Inventory)
    monkeypatch.setattr(cli, "ensure_seeded", lambda *args: None)
    monkeypatch.setattr(
        cli,
        "resolve_agent_identity",
        lambda _cfg: {"effective_mcc_host_name": "MauticFarm-02"},
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "mcd-cli",
            "instances",
            "list",
            "--instance-uid",
            "demov6.sales-snap.com@MauticFarm-01",
            "--json",
        ],
    )

    assert cli.main() == 2
    assert json.loads(capsys.readouterr().out) == []
