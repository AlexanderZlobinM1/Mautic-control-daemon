"""Read-only Wazuh inventory; provisioning belongs to MCC-distributed artifacts."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
from typing import Any
import xml.etree.ElementTree as ET


_WAZUH_KEYRING_PATH = Path("/usr/share/keyrings/wazuh.gpg")
_WAZUH_LIST_PATH = Path("/etc/apt/sources.list.d/wazuh.list")
_WAZUH_OSSEC_CONF = Path("/var/ossec/etc/ossec.conf")
_WAZUH_CLIENT_KEYS = Path("/var/ossec/etc/client.keys")
_WAZUH_SERVICE = "wazuh-agent"


def _run(
    cmd: list[str],
    *,
    timeout_sec: int = 120,
    env_extra: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = {"DEBIAN_FRONTEND": "noninteractive", "LC_ALL": "C", "LANG": "C"}
    if env_extra:
        env.update({str(k): str(v) for k, v in env_extra.items()})
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=max(5, int(timeout_sec)),
        env={**os.environ, **env},
    )


def _bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    raw = str(value or "").strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    return default


def _pkg_state(package: str) -> tuple[bool, str]:
    proc = _run(["dpkg-query", "-W", "-f=${Status}\t${Version}", package], timeout_sec=20)
    if proc.returncode != 0:
        return False, ""
    line = (proc.stdout or "").strip()
    if not line.startswith("install ok installed"):
        return False, ""
    if "\t" not in line:
        return True, ""
    return True, line.split("\t", 1)[1].strip()


def _systemctl_value(*args: str) -> str:
    proc = _run(["systemctl", *args], timeout_sec=20)
    if proc.returncode != 0:
        return ""
    return (proc.stdout or proc.stderr or "").strip()


def _service_state() -> dict[str, Any]:
    return {
        "enabled": _systemctl_value("is-enabled", _WAZUH_SERVICE) in {"enabled", "static"},
        "active": _systemctl_value("is-active", _WAZUH_SERVICE) == "active",
    }


def _read_xml_root(path: Path) -> ET.Element | None:
    if not path.exists():
        return None
    try:
        raw = path.read_text(encoding="utf-8", errors="ignore")
        return ET.fromstring(f"<mcd_root>{raw}</mcd_root>")
    except Exception:
        return None


def _string_child(parent: ET.Element | None, name: str) -> str:
    if parent is None:
        return ""
    child = parent.find(name)
    if child is None or child.text is None:
        return ""
    return child.text.strip()


def _parsed_agent_config(path: Path = _WAZUH_OSSEC_CONF) -> dict[str, Any]:
    root = _read_xml_root(path)
    if root is None:
        return {}
    client = root.find("./ossec_config/client")
    if client is None:
        client = root.find(".//client")
    if client is None:
        return {}
    server = client.find("server")
    enrollment = client.find("enrollment")
    return {
        "manager_address": _string_child(server, "address"),
        "manager_port": _string_child(server, "port"),
        "protocol": _string_child(server, "protocol"),
        "registration_server": _string_child(enrollment, "manager_address"),
        "registration_port": _string_child(enrollment, "port"),
        "agent_name": _string_child(enrollment, "agent_name"),
        "agent_group": _string_child(enrollment, "groups"),
    }


def collect_wazuh_agent_state(profile: dict[str, Any] | None = None) -> dict[str, Any]:
    installed, version = _pkg_state("wazuh-agent")
    repo_present = False
    repo_entry = ""
    if _WAZUH_LIST_PATH.exists():
        try:
            lines = [line.strip() for line in _WAZUH_LIST_PATH.read_text(encoding="utf-8", errors="ignore").splitlines()]
            active = [line for line in lines if line and not line.startswith("#")]
            repo_present = any(line.startswith("deb ") for line in active)
            repo_entry = active[0] if active else ""
        except Exception:
            repo_present = False
    state = {
        "installed": installed,
        "version": version,
        "repo_present": repo_present,
        "repo_entry": repo_entry,
        "keyring_present": _WAZUH_KEYRING_PATH.exists(),
        "service": _service_state(),
        "client_keys_present": _WAZUH_CLIENT_KEYS.exists(),
        "config": _parsed_agent_config(),
    }
    if isinstance(profile, dict):
        state["profile_enabled"] = _bool(profile.get("enabled"), False)
    return state
