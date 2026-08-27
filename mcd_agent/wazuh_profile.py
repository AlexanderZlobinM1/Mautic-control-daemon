from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
from typing import Any
from urllib import request as urlrequest
import xml.etree.ElementTree as ET

from mcd_agent.config import AgentConfig
from mcd_agent.host_identity import resolve_agent_identity


_WAZUH_KEYRING_PATH = Path("/usr/share/keyrings/wazuh.gpg")
_WAZUH_LIST_PATH = Path("/etc/apt/sources.list.d/wazuh.list")
_WAZUH_OSSEC_CONF = Path("/var/ossec/etc/ossec.conf")
_WAZUH_AUTHD_PASS = Path("/var/ossec/etc/authd.pass")
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


def _int(value: Any, default: int, min_v: int = 0, max_v: int = 65535) -> int:
    try:
        out = int(value)
    except Exception:
        out = int(default)
    if out < min_v:
        out = min_v
    if out > max_v:
        out = max_v
    return out


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


def _resolved_agent_name(profile: dict[str, Any], cfg: AgentConfig) -> str:
    explicit = str(profile.get("agent_name", "") or "").strip()
    if explicit:
        return explicit
    ident = resolve_agent_identity(cfg)
    source = str(profile.get("agent_name_source", "mcc_host_name") or "mcc_host_name").strip().lower()
    if source == "mcc_host_name":
        return str(ident.get("effective_mcc_host_name") or ident.get("configured_host_name") or ident.get("local_hostname") or "localhost")
    if source == "configured_host_name":
        return str(ident.get("configured_host_name") or ident.get("local_hostname") or "localhost")
    return str(ident.get("local_hostname") or "localhost")


def _write_text(path: Path, content: str, *, mode: int | None = None) -> bool:
    old = None
    if path.exists():
        try:
            old = path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            old = None
    if old == content:
        if mode is not None:
            try:
                os.chmod(path, mode)
            except Exception:
                pass
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    if mode is not None:
        os.chmod(path, mode)
    return True


def _ensure_wazuh_keyring(profile: dict[str, Any], *, timeout_sec: int) -> tuple[bool, str]:
    key_url = str(profile.get("repo_key_url", "") or "").strip()
    if not key_url:
        raise RuntimeError("repo_key_url is required")
    with urlrequest.urlopen(key_url, timeout=max(10, int(timeout_sec))) as resp:
        armored = resp.read()
    with tempfile.NamedTemporaryFile(prefix="mcd-wazuh-key-", suffix=".asc", delete=False) as src:
        src.write(armored)
        src_path = Path(src.name)
    tmp_out = src_path.with_suffix(".gpg")
    try:
        proc = _run(["gpg", "--dearmor", "--yes", "-o", str(tmp_out), str(src_path)], timeout_sec=timeout_sec)
        if proc.returncode != 0:
            raise RuntimeError((proc.stderr or proc.stdout or "gpg --dearmor failed").strip())
        new_bytes = tmp_out.read_bytes()
        if _WAZUH_KEYRING_PATH.exists() and _WAZUH_KEYRING_PATH.read_bytes() == new_bytes:
            return False, str(_WAZUH_KEYRING_PATH)
        _WAZUH_KEYRING_PATH.parent.mkdir(parents=True, exist_ok=True)
        _WAZUH_KEYRING_PATH.write_bytes(new_bytes)
        os.chmod(_WAZUH_KEYRING_PATH, 0o644)
        return True, str(_WAZUH_KEYRING_PATH)
    finally:
        src_path.unlink(missing_ok=True)
        tmp_out.unlink(missing_ok=True)


def _ensure_wazuh_repo(profile: dict[str, Any]) -> bool:
    if not _bool(profile.get("repo_enabled"), True):
        if _WAZUH_LIST_PATH.exists():
            _WAZUH_LIST_PATH.unlink()
            return True
        return False
    repo_line = str(profile.get("repo_list_entry", "") or "").strip()
    if not repo_line:
        raise RuntimeError("repo_list_entry is required")
    return _write_text(_WAZUH_LIST_PATH, repo_line + "\n", mode=0o644)


def _ensure_prerequisites(timeout_sec: int) -> None:
    proc = _run(["apt-get", "install", "-y", "ca-certificates", "curl", "gnupg"], timeout_sec=timeout_sec)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "failed to install wazuh prerequisites").strip())


def _apt_update(timeout_sec: int) -> None:
    proc = _run(["apt-get", "update", "--allow-releaseinfo-change"], timeout_sec=timeout_sec)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "apt-get update failed").strip())


def _wazuh_package_env(profile: dict[str, Any], cfg: AgentConfig) -> dict[str, str]:
    manager = str(profile.get("manager_address", "") or "").strip()
    registration_server = str(profile.get("registration_server", "") or "").strip() or manager
    env = {
        "WAZUH_MANAGER": manager,
        "WAZUH_MANAGER_PORT": str(_int(profile.get("manager_port"), 1514, 1, 65535)),
        "WAZUH_PROTOCOL": str(profile.get("protocol", "tcp") or "tcp").strip().upper(),
        "WAZUH_REGISTRATION_SERVER": registration_server,
        "WAZUH_REGISTRATION_PORT": str(_int(profile.get("registration_port"), 1515, 1, 65535)),
        "WAZUH_AGENT_NAME": _resolved_agent_name(profile, cfg),
        "ENROLLMENT_DELAY": str(_int(profile.get("registration_delay_sec"), 20, 0, 86400)),
    }
    group = str(profile.get("agent_group", "") or "").strip()
    if group:
        env["WAZUH_AGENT_GROUP"] = group
    password = str(profile.get("registration_password", "") or "")
    if password:
        env["WAZUH_REGISTRATION_PASSWORD"] = password
    return env


def _install_wazuh_agent(profile: dict[str, Any], cfg: AgentConfig, *, timeout_sec: int) -> None:
    env = _wazuh_package_env(profile, cfg)
    proc = _run(["apt-get", "install", "-y", "wazuh-agent"], timeout_sec=timeout_sec, env_extra=env)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "failed to install wazuh-agent").strip())


def _wazuh_upgrade_available(timeout_sec: int) -> bool:
    proc = _run(["apt-get", "-s", "install", "--only-upgrade", "wazuh-agent"], timeout_sec=timeout_sec)
    if proc.returncode != 0:
        return False
    merged = f"{proc.stdout or ''}\n{proc.stderr or ''}"
    for line in merged.splitlines():
        if line.strip().startswith("Inst wazuh-agent "):
            return True
    return False


def _upgrade_wazuh_agent(profile: dict[str, Any], cfg: AgentConfig, *, timeout_sec: int) -> None:
    env = _wazuh_package_env(profile, cfg)
    proc = _run(
        ["apt-get", "install", "-y", "--only-upgrade", "wazuh-agent"],
        timeout_sec=timeout_sec,
        env_extra=env,
    )
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "failed to upgrade wazuh-agent").strip())


def _desired_client_settings(profile: dict[str, Any], cfg: AgentConfig) -> dict[str, str]:
    manager = str(profile.get("manager_address", "") or "").strip()
    registration_server = str(profile.get("registration_server", "") or "").strip() or manager
    return {
        "manager_address": manager,
        "manager_port": str(_int(profile.get("manager_port"), 1514, 1, 65535)),
        "protocol": str(profile.get("protocol", "tcp") or "tcp").strip().lower() or "tcp",
        "registration_server": registration_server,
        "registration_port": str(_int(profile.get("registration_port"), 1515, 1, 65535)),
        "agent_name": _resolved_agent_name(profile, cfg),
        "agent_group": str(profile.get("agent_group", "") or "").strip(),
        "registration_delay_sec": str(_int(profile.get("registration_delay_sec"), 20, 0, 86400)),
        "registration_password": str(profile.get("registration_password", "") or ""),
    }


def _serialize_wrapped_root(root: ET.Element) -> str:
    for node in root:
        ET.indent(node, space="  ")
    chunks = [ET.tostring(node, encoding="unicode") for node in root]
    return ("\n\n".join(chunk.strip() for chunk in chunks if chunk.strip()).strip() + "\n") if chunks else ""


def _ensure_agent_config(profile: dict[str, Any], cfg: AgentConfig) -> tuple[bool, list[str]]:
    desired = _desired_client_settings(profile, cfg)
    changed_files: list[str] = []
    root = _read_xml_root(_WAZUH_OSSEC_CONF)
    if root is None:
        root = ET.Element("mcd_root")
        ossec = ET.SubElement(root, "ossec_config")
    else:
        ossec = root.find("ossec_config")
        if ossec is None:
            ossec = ET.SubElement(root, "ossec_config")
    client = root.find("./ossec_config/client")
    if client is None:
        client = root.find(".//client")
    if client is None:
        client = ET.SubElement(ossec, "client")
    for child in list(client):
        if child.tag in {"server", "enrollment"}:
            client.remove(child)

    server = ET.SubElement(client, "server")
    ET.SubElement(server, "address").text = desired["manager_address"]
    ET.SubElement(server, "port").text = desired["manager_port"]
    ET.SubElement(server, "protocol").text = desired["protocol"]

    enrollment = ET.SubElement(client, "enrollment")
    ET.SubElement(enrollment, "enabled").text = "yes"
    ET.SubElement(enrollment, "manager_address").text = desired["registration_server"]
    ET.SubElement(enrollment, "port").text = desired["registration_port"]
    ET.SubElement(enrollment, "agent_name").text = desired["agent_name"]
    if desired["agent_group"]:
        ET.SubElement(enrollment, "groups").text = desired["agent_group"]
    ET.SubElement(enrollment, "delay_after_enrollment").text = desired["registration_delay_sec"]
    if desired["registration_password"]:
        ET.SubElement(enrollment, "authorization_pass_path").text = str(_WAZUH_AUTHD_PASS)

    new_text = _serialize_wrapped_root(root)
    if _write_text(_WAZUH_OSSEC_CONF, new_text):
        changed_files.append(str(_WAZUH_OSSEC_CONF))
    if desired["registration_password"]:
        if _write_text(_WAZUH_AUTHD_PASS, desired["registration_password"].rstrip("\n") + "\n", mode=0o600):
            changed_files.append(str(_WAZUH_AUTHD_PASS))
    return bool(changed_files), changed_files


def _set_package_hold(hold: bool) -> bool:
    selection = f"wazuh-agent {'hold' if hold else 'install'}\n"
    proc = subprocess.run(
        ["dpkg", "--set-selections"],
        input=selection,
        capture_output=True,
        text=True,
        timeout=20,
    )
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "dpkg --set-selections failed").strip())
    return True


def apply_wazuh_profile(
    profile: dict[str, Any] | None,
    cfg: AgentConfig,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    if profile is None:
        profile = {}
    current = collect_wazuh_agent_state(profile)
    if dry_run:
        return {
            "status": "planned",
            "actions": [
                "wazuh_repo",
                "wazuh_keyring",
                "apt_update",
                "wazuh_agent_install",
                "ossec_conf_reconcile",
                "optional_reenroll",
                "service_enable_restart",
            ],
            "current": current,
        }
    if os.geteuid() != 0:
        raise RuntimeError("service-profile apply requires root")

    if not _bool(profile.get("enabled"), False):
        return {
            "status": "disabled",
            "actions": ["wazuh:disabled"],
            "state": current,
        }

    manager = str(profile.get("manager_address", "") or "").strip()
    if not manager:
        return {
            "status": "error",
            "reason": "manager_address_required",
            "state": current,
        }

    actions: list[str] = []
    changed_files: list[str] = []
    timeout_sec = max(30, _int(profile.get("refresh_timeout_sec"), 180, 30, 3600))
    try:
        if _bool(profile.get("ensure_prerequisites"), True):
            _ensure_prerequisites(timeout_sec)
            actions.append("prerequisites:ok")

        repo_changed = _ensure_wazuh_repo(profile)
        actions.append("wazuh_repo:updated" if repo_changed else "wazuh_repo:ok")
        if repo_changed:
            changed_files.append(str(_WAZUH_LIST_PATH))
        key_changed = False
        if _bool(profile.get("repo_enabled"), True):
            key_changed, key_path = _ensure_wazuh_keyring(profile, timeout_sec=timeout_sec)
            actions.append("wazuh_keyring:updated" if key_changed else "wazuh_keyring:ok")
            if key_changed:
                changed_files.append(key_path)
        else:
            actions.append("wazuh_keyring:skipped")

        if repo_changed or key_changed:
            _apt_update(timeout_sec)
            actions.append("apt_update:ok")

        hold_package = _bool(profile.get("hold_package"), False)
        if not hold_package:
            _set_package_hold(False)
            actions.append("package_hold:disabled")

        installed, _version = _pkg_state("wazuh-agent")
        if not installed:
            if not _bool(profile.get("install_package"), True):
                raise RuntimeError("wazuh-agent is not installed and install_package=false")
            _install_wazuh_agent(profile, cfg, timeout_sec=timeout_sec)
            actions.append("wazuh_agent:installed")
        elif _bool(profile.get("install_package"), True) and not hold_package and _wazuh_upgrade_available(timeout_sec):
            _upgrade_wazuh_agent(profile, cfg, timeout_sec=timeout_sec)
            actions.append("wazuh_agent:upgraded")
        else:
            actions.append("wazuh_agent:present")

        config_changed, config_files = _ensure_agent_config(profile, cfg)
        if config_changed:
            actions.append("ossec_conf:updated")
            changed_files.extend(config_files)
        else:
            actions.append("ossec_conf:ok")

        if hold_package:
            _set_package_hold(True)
            actions.append("package_hold:enabled")

        if _bool(profile.get("force_reenroll"), False) and _WAZUH_CLIENT_KEYS.exists():
            _WAZUH_CLIENT_KEYS.unlink()
            actions.append("client_keys:removed")

        if _bool(profile.get("start_service"), True):
            _run(["systemctl", "daemon-reload"], timeout_sec=20)
            enable = _run(["systemctl", "enable", _WAZUH_SERVICE], timeout_sec=30)
            if enable.returncode != 0:
                raise RuntimeError((enable.stderr or enable.stdout or "failed to enable wazuh-agent").strip())
            restart = _run(["systemctl", "restart", _WAZUH_SERVICE], timeout_sec=60)
            if restart.returncode != 0:
                raise RuntimeError((restart.stderr or restart.stdout or "failed to restart wazuh-agent").strip())
            actions.append("service:restarted")
        else:
            actions.append("service:start_skipped")

        state = collect_wazuh_agent_state(profile)
        return {
            "status": "applied",
            "actions": actions,
            "changed_files": changed_files,
            "state": state,
        }
    except Exception as e:
        state = collect_wazuh_agent_state(profile)
        return {
            "status": "error",
            "reason": str(e),
            "actions": actions,
            "changed_files": changed_files,
            "state": state,
        }
