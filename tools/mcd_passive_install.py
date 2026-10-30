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
import socket
import subprocess
import sys
import tempfile
from urllib import request, parse


class NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise RuntimeError("bootstrap_redirect_refused")


def https(url):
    parsed = parse.urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        raise ValueError("bootstrap_https_required")
    return url


def post(base, path, body, key=None):
    headers = {"Content-Type": "application/json", "X-Portal-Contract": "com.sales-snap.portal-commercial/1.0",
               "Idempotency-Key": body["idempotency_key"], "User-Agent": "MCD/1.6.25 passive-bootstrap"}
    if key:
        headers["Authorization"] = "Bearer " + key
    req = request.Request(https(base + path), data=json.dumps(body).encode(), headers=headers)
    with request.build_opener(NoRedirect()).open(req, timeout=60) as response:
        if response.status not in (200, 201):
            raise RuntimeError("bootstrap_portal_not_accepted")
        return json.loads(response.read(1048576))


def run(*args, data=None):
    result = subprocess.run(args, input=data, text=True, capture_output=True)
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
    parser.add_argument("--bridge", required=True, help="Separately SHA-verified MCD runtime bridge")
    args = parser.parse_args()
    base = https(args.portal_api_base.rstrip("/"))
    https(args.archive_uri)
    if not base.endswith("/api/v1/commercial"):
        raise ValueError("bootstrap_api_base_invalid")
    for digest in (args.archive_sha256, args.manifest_sha256):
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("bootstrap_digest_invalid")
    key = sys.stdin.readline(258).rstrip("\n")
    if not 24 <= len(key) <= 256 or "\r" in key or "\n" in key:
        raise ValueError("bootstrap_key_invalid")
    root = Path("/opt/mcd")
    unit = Path("/etc/systemd/system/mcd.service")
    if os.geteuid() != 0 or root.exists() or unit.exists() or Path("/etc/systemd/system/mcd-agent.service").exists():
        raise RuntimeError("bootstrap_fresh_root_required")
    os.umask(0o077)
    spec = importlib.util.spec_from_file_location("mcd_bridge", args.bridge)
    bridge = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = bridge
    spec.loader.exec_module(bridge)
    with tempfile.TemporaryDirectory(prefix="mcd-passive-") as temp:
        archive = Path(temp) / "runtime.tar.gz"
        digest = hashlib.sha256()
        with request.build_opener(NoRedirect()).open(request.Request(args.archive_uri, headers={"User-Agent": "MCD/1.6.25 passive-bootstrap"}), timeout=120) as response, archive.open("wb") as output:
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
        actual = run(python, "-c", "import mcd_agent;print(mcd_agent.__version__)").strip()
        if actual != args.runtime_version:
            raise RuntimeError("bootstrap_import_version_mismatch")
        for name in ("etc", "var"):
            (root / name).mkdir(mode=0o700)
        config = root / "etc/mcd.toml"
        config.write_text('[include]\nfiles = ["/opt/mcd/src/etc/mcd-agent.system.example.toml", "/opt/mcd/src/etc/mcd-agent.operator.example.toml"]\n[profile]\nname = "passive"\n[mcc]\nurl = ""\npush_enabled = false\n[state]\nbackend = "sqlite"\nstate_db_path = "/opt/mcd/var/mcd-state.db"\n')
        fingerprint = "sha256:" + hashlib.sha256(socket.gethostname().encode()).hexdigest()
        identity = socket.gethostname().lower()
        if not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", identity):
            raise RuntimeError("bootstrap_host_identity_invalid")
        activation = post(base, "/activations/redeem-commercial-key", {"schema": "portal-mcd-commercial-key-activation-request-v1", "contract_version": "1.0", "commercial_key": key, "agent": {"host_fingerprint": fingerprint, "agent_version": actual, "platform": "linux"}, "supported_contract_versions": ["1.0"], "idempotency_key": args.request_id + ":activation"})
        if activation.get("schema") != "portal-mcd-activation-response-v1" or activation.get("agent_credential") != key:
            raise RuntimeError("bootstrap_activation_response_invalid")
        agent = activation["agent_id"]
        installation = post(base, "/agents/" + parse.quote(agent, safe="") + "/installations", {"schema": "portal-mcd-host-register-request-v1", "contract_version": "1.0", "registration_kind": "host", "idempotency_key": args.request_id + ":installation", "host": {"local_identity": identity, "display_name": identity}}, key)
        registered = installation["installation"]
        if registered.get("status") != "passive" or registered.get("agent_id") != agent or registered.get("local_identity") != identity:
            raise RuntimeError("bootstrap_host_response_invalid")
        installation["installation_id"] = registered["id"]
        material = {"schema": "mcd-portal-activation-material-v1", "contract_version": "1.0", "portal_api_base": base, "agent_id": agent, "portal_agent_credential": key, "commercial_key_id": activation["commercial_key_id"], "host_fingerprint": fingerprint, "installation_ids": {identity: installation["installation_id"]}}
        run(python, "-m", "mcd_agent.commercial_registration", "--state-db", str(root / "var/mcd-state.db"), "--json", data=json.dumps(material))
        unit.write_text('[Unit]\nDescription=MCD passive commercial worker\nAfter=network-online.target\n[Service]\nType=simple\nWorkingDirectory=/opt/mcd\nExecStart=/opt/mcd/venv/bin/python -m mcd_agent run --config /opt/mcd/etc/mcd.toml\nRestart=on-failure\n[Install]\nWantedBy=multi-user.target\n')
        unit.chmod(0o644)
        run("systemctl", "daemon-reload")
        receipt = {"schema": "mcd-passive-install-receipt-v1", "version": actual, "archive_sha256": args.archive_sha256, "manifest_sha256": args.manifest_sha256, "agent_id": agent, "installation_id": installation["installation_id"], "activation_state": "passive", "service_started": False}
        (root / "var/passive-install-receipt.json").write_text(json.dumps(receipt, sort_keys=True) + "\n")
        print(json.dumps(receipt))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print("bootstrap_failed:" + type(exc).__name__, file=sys.stderr)
        sys.exit(1)
