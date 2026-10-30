#!/usr/bin/env python3
"""Fresh passive MCD installation using HTTPS, one Portal key and SHA-256.

Requires root, Python >=3.10 with venv/pip, and systemd. Never starts the worker.
Existing installations are refused. Read the API key from stdin, never argv.
"""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from urllib import request, parse
from urllib.error import HTTPError, URLError

USER_AGENT = "mcd-agent/1.6.26 portal-commercial/1.0 passive-bootstrap/1.0"

class NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise RuntimeError("bootstrap_redirect_refused")


PASSIVE_CONFIG = '[config]\nschema_version = 1\n[profile]\nname = "passive"\n[mcc]\nurl = ""\ntoken = ""\npush_enabled = false\n[state]\nbackend = "sqlite"\nstate_db_path = "/opt/mcd/var/mcd-state.db"\n'
PASSIVE_UNIT = '[Unit]\nDescription=MCD passive commercial worker\nAfter=network-online.target\n[Service]\nType=simple\nWorkingDirectory=/opt/mcd\nExecStart=/opt/mcd/venv/bin/python -m mcd_agent run --config /opt/mcd/etc/mcd.toml\nRestart=on-failure\nKillMode=process\n[Install]\nWantedBy=multi-user.target\n'


def require_passive_service():
    if run("systemctl", "show", "mcd.service", "--property=ActiveState", "--value").strip() != "inactive":
        raise RuntimeError("bootstrap_service_not_passive")
    if run("systemctl", "show", "mcd.service", "--property=UnitFileState", "--value").strip() != "disabled":
        raise RuntimeError("bootstrap_service_not_disabled")


def completed_replay(root, unit, args, key, identity):
    """An exact completed passive install is replayable without enrollment or writes."""
    receipt_path = root / "var/passive-install-receipt.json"
    if any(path.is_symlink() for path in (root, root / "var", root / "etc", root / "venv", root / "var/mcd-state.db", receipt_path, unit)) or not receipt_path.is_file() or not unit.is_file() or not (root / "var/mcd-state.db").is_file():
        raise RuntimeError("bootstrap_fresh_root_required")
    receipt = json.loads(receipt_path.read_text())
    expected = {"schema": "mcd-passive-install-receipt-v1", "version": args.runtime_version, "archive_sha256": args.archive_sha256, "manifest_sha256": args.manifest_sha256, "bridge_sha256": args.bridge_sha256, "request_id": args.request_id, "local_identity": identity, "activation_state": "passive", "service_started": False}
    if any(receipt.get(name) != value for name, value in expected.items()) or unit.read_text() != PASSIVE_UNIT or (root / "etc/mcd.toml").read_text() != PASSIVE_CONFIG:
        raise RuntimeError("bootstrap_replay_binding_mismatch")
    if receipt.get("identity_kind") != "dns" or "host_fingerprint" in receipt:
        raise RuntimeError("bootstrap_replay_identity_mismatch")
    material = {"key": key, "agent_id": receipt["agent_id"], "installation_id": receipt["installation_id"], "identity": identity, "base": args.portal_api_base.rstrip("/"), "version": args.runtime_version}
    check = '''import json,sys,hmac
from mcd_agent.commercial_portal import CommercialState
from mcd_agent import __version__
m=json.load(sys.stdin);s=CommercialState(sys.argv[1])
ok=hmac.compare_digest(str(s.get("portal_agent_credential") or ""),m["key"]) and s.get("identity_kind")=="dns" and s.get("local_identity")==m["identity"] and not s.get("host_fingerprint") and s.get("agent_id")==m["agent_id"] and s.get("installation_ids")=={m["identity"]:m["installation_id"]} and s.get("portal_api_base")==m["base"] and __version__==m["version"]
raise SystemExit(0 if ok else 1)
'''
    run(str(root / "venv/bin/python"), "-I", "-c", check, str(root / "var/mcd-state.db"), data=json.dumps(material))
    require_passive_service()
    return {**receipt, "idempotent_replay": True}


def https(url):
    parsed = parse.urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        raise ValueError("bootstrap_https_required")
    return url


def post(base, path, body, key=None):
    headers = {"Content-Type": "application/json", "X-Portal-Contract": "com.sales-snap.portal-commercial/1.0",
               "Idempotency-Key": body["idempotency_key"], "User-Agent": USER_AGENT}
    if key:
        headers["Authorization"] = "Bearer " + key
    req = request.Request(https(base + path), data=json.dumps(body).encode(), headers=headers)
    with request.build_opener(NoRedirect()).open(req, timeout=60) as response:
        if response.status not in (200, 201):
            raise RuntimeError("bootstrap_portal_not_accepted")
        return json.loads(response.read(1048576))


def run(*args, data=None):
    result = subprocess.run(args, input=data, text=True, capture_output=True, timeout=900)
    if result.returncode:
        # Do not echo subprocess output: enrollment material may contain the key.
        raise RuntimeError("bootstrap_command_failed:" + Path(args[0]).name)
    return result.stdout


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--portal-api-base", required=True)
    parser.add_argument("--archive-uri", required=True)
    parser.add_argument("--archive-sha256", required=True)
    parser.add_argument("--manifest-sha256", required=True)
    parser.add_argument("--runtime-version", required=True)
    parser.add_argument("--request-id", required=True)
    parser.add_argument("--instance-identity", required=True, help="Portable instance DNS identity, without a host suffix")
    parser.add_argument("--bridge", required=True, help="Separately SHA-verified MCD runtime bridge")
    parser.add_argument("--bridge-sha256", required=True)
    args = parser.parse_args()
    base = https(args.portal_api_base.rstrip("/"))
    https(args.archive_uri)
    if not base.endswith("/api/v1/commercial"):
        raise ValueError("bootstrap_api_base_invalid")
    for digest in (args.archive_sha256, args.manifest_sha256, args.bridge_sha256):
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("bootstrap_digest_invalid")
    identity = args.instance_identity.strip().lower()
    if not re.fullmatch(r"(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}", identity):
        raise ValueError("bootstrap_instance_identity_invalid")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,191}", args.request_id):
        raise ValueError("bootstrap_request_id_invalid")
    if hashlib.sha256(Path(args.bridge).read_bytes()).hexdigest() != args.bridge_sha256:
        raise RuntimeError("bootstrap_bridge_digest_mismatch")
    key = sys.stdin.readline(258).rstrip("\n")
    if not 24 <= len(key) <= 256 or "\r" in key or "\n" in key:
        raise ValueError("bootstrap_key_invalid")
    root = Path("/opt/mcd")
    unit = Path("/etc/systemd/system/mcd.service")
    if os.geteuid() != 0 or Path("/etc/systemd/system/mcd-agent.service").exists():
        raise RuntimeError("bootstrap_fresh_root_required")
    if root.exists() or root.is_symlink() or unit.exists() or unit.is_symlink():
        print(json.dumps(completed_replay(root, unit, args, key, identity)))
        return
    os.umask(0o077)
    spec = importlib.util.spec_from_file_location("mcd_bridge", args.bridge)
    bridge = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = bridge
    spec.loader.exec_module(bridge)
    with tempfile.TemporaryDirectory(prefix="mcd-passive-") as temp:
        archive = Path(temp) / "runtime.tar.gz"
        digest = hashlib.sha256()
        with request.build_opener(NoRedirect()).open(request.Request(args.archive_uri, headers={"User-Agent": USER_AGENT}), timeout=120) as response, archive.open("wb") as output:
            while chunk := response.read(1048576):
                digest.update(chunk)
                output.write(chunk)
        if digest.hexdigest() != args.archive_sha256:
            raise RuntimeError("bootstrap_archive_digest_mismatch")
        stage = Path(temp) / "stage"
        stage.mkdir()
        manifest = bridge._extract_archive(archive, stage)
        if hashlib.sha256((stage / bridge.MANIFEST_NAME).read_bytes()).hexdigest() != args.manifest_sha256 or manifest["version"] != args.runtime_version:
            raise RuntimeError("bootstrap_manifest_mismatch")
        root.mkdir(mode=0o700)
        # Preserve partial install on failure for diagnosis; never erase host state.
        import shutil
        shutil.copytree(stage, root / "src")
        run(sys.executable, "-m", "venv", str(root / "venv"))
        python = str(root / "venv/bin/python")
        run(python, "-m", "pip", "install", "--disable-pip-version-check", "-r", str(stage / manifest["requirements"]), str(stage / manifest["wheel"]))
        actual = run(python, "-I", "-c", "import mcd_agent,sys,importlib.metadata;from pathlib import Path;assert Path(sys.prefix).resolve() in Path(mcd_agent.__file__).resolve().parents;assert importlib.metadata.version('mcd-agent')==mcd_agent.__version__;print(mcd_agent.__version__)").strip()
        if actual != args.runtime_version:
            raise RuntimeError("bootstrap_import_version_mismatch")
        for name in ("etc", "var"):
            (root / name).mkdir(mode=0o700)
        config = root / "etc/mcd.toml"
        config.write_text(PASSIVE_CONFIG)
        activation = post(base, "/activations/redeem-commercial-key", {"schema": "portal-mcd-commercial-key-activation-request-v1", "contract_version": "1.0", "commercial_key": key, "agent": {"identity_kind": "dns", "local_identity": identity, "agent_version": actual, "platform": "linux"}, "supported_contract_versions": ["1.0"], "idempotency_key": args.request_id + ":dns-enroll-v1"})
        if activation.get("schema") != "portal-mcd-activation-response-v1" or activation.get("contract_version") != "1.0" or activation.get("agent_credential") != key or not activation.get("account_id") or not activation.get("commercial_key_id") or activation.get("portal", {}).get("api_base", "").rstrip("/") != base:
            raise RuntimeError("bootstrap_activation_response_invalid")
        agent = activation["agent_id"]
        if activation.get("identity_kind") != "dns" or activation.get("local_identity") != identity or "host_fingerprint" in activation:
            raise RuntimeError("bootstrap_activation_identity_invalid")
        if not isinstance(agent, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,192}", agent):
            raise RuntimeError("bootstrap_agent_identity_invalid")
        installation = post(base, "/agents/" + parse.quote(agent, safe="") + "/installations", {"schema": "portal-mcd-host-register-request-v1", "contract_version": "1.0", "registration_kind": "host", "idempotency_key": args.request_id + ":dns-host-v1", "host": {"local_identity": identity, "display_name": identity}}, key)
        registered = installation["installation"]
        if registered.get("status") != "passive" or registered.get("agent_id") != agent or registered.get("local_identity") != identity or not registered.get("id") or installation.get("connection", {}).get("status") != "active":
            raise RuntimeError("bootstrap_host_response_invalid")
        installation["installation_id"] = registered["id"]
        material = {"schema": "mcd-portal-enrollment-material-v1", "contract_version": "1.0", "identity_kind": "dns", "local_identity": identity, "account_id": activation["account_id"], "portal_api_base": base, "agent_id": agent, "portal_agent_credential": key, "commercial_key_id": activation["commercial_key_id"], "installation_ids": {identity: installation["installation_id"]}}
        imported = json.loads(run(python, "-I", "-m", "mcd_agent.commercial_registration", "--state-db", str(root / "var/mcd-state.db"), "--json", data=json.dumps(material)))
        if imported.get("schema") != "mcd-portal-registration-receipt-v1" or imported.get("status") != "configured" or imported.get("agent_id") != agent:
            raise RuntimeError("bootstrap_binding_import_invalid")
        unit.write_text(PASSIVE_UNIT)
        unit.chmod(0o644)
        run("systemctl", "daemon-reload")
        require_passive_service()
        receipt = {"schema": "mcd-passive-install-receipt-v1", "version": actual, "archive_sha256": args.archive_sha256, "manifest_sha256": args.manifest_sha256, "bridge_sha256": args.bridge_sha256, "request_id": args.request_id, "identity_kind": "dns", "local_identity": identity, "account_id": activation["account_id"], "commercial_key_id": activation["commercial_key_id"], "agent_id": agent, "installation_id": installation["installation_id"], "activation_state": "passive", "service_started": False}
        (root / "var/passive-install-receipt.json").write_text(json.dumps(receipt, sort_keys=True) + "\n")
        print(json.dumps(receipt))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        if isinstance(exc, HTTPError):
            code = f"bootstrap_http_{exc.code}"
        elif isinstance(exc, TimeoutError):
            code = "bootstrap_network_timeout"
        elif isinstance(exc, URLError):
            code = "bootstrap_network_error"
        else:
            message = str(exc)
            code = message if re.fullmatch(r"bootstrap_[A-Za-z0-9_:.-]{1,120}", message) else "bootstrap_failed"
        print(json.dumps({"schema": "mcd-passive-install-error-v1", "status": "failed", "code": code}), file=sys.stderr)
        sys.exit(1)
