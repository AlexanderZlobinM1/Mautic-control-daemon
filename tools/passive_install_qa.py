"""Isolated Ubuntu QA fixture; HTTPS Portal contract double, no live credentials."""
import hashlib
import http.server
import json
import os
from pathlib import Path
import socket
import ssl
import subprocess
import threading
import tarfile

KEY = "qa-commercial-key-not-a-live-secret-12345"
calls = []
with tarfile.open("/runtime.tar.gz", "r:gz") as runtime_tar:
    manifest_bytes = runtime_tar.extractfile("mcd-runtime-manifest.json").read()
runtime_manifest = json.loads(manifest_bytes)
runtime_version = runtime_manifest["version"]


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        data = Path("/runtime.tar.gz").read_bytes()
        self.send_response(200)
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        assert self.headers["X-Portal-Contract"] == "com.sales-snap.portal-commercial/1.0"
        assert self.headers["Idempotency-Key"] == body["idempotency_key"]
        assert self.headers["User-Agent"].startswith(f"mcd-agent/{runtime_version} portal-commercial/1.0")
        calls.append(body)
        if self.path.endswith("redeem-commercial-key"):
            assert self.headers["User-Agent"] == f"mcd-agent/{runtime_version} portal-commercial/1.0 passive-bootstrap/1.0"
            assert body["commercial_key"] == KEY
            assert "public_key" not in body["agent"]
            assert "host_fingerprint" not in body["agent"]
            assert body["agent"]["identity_kind"] == "dns"
            assert body["agent"]["local_identity"] == "passive-qa.example.invalid"
            result = {"schema": "portal-mcd-activation-response-v1", "contract_version": "1.0", "identity_kind": "dns", "local_identity": "passive-qa.example.invalid", "account_id": "account_qa", "agent_id": "agent_qa", "agent_credential": KEY, "commercial_key_id": "key_qa", "portal": {"api_base": "https://localhost:8443/api/v1/commercial"}}
        elif self.path.endswith("/installations"):
            assert self.headers["Authorization"] == "Bearer " + KEY
            assert body["schema"] == "portal-mcd-host-register-request-v1"
            result = {"installation": {"id": "installation_qa", "agent_id": "agent_qa", "local_identity": "passive-qa.example.invalid", "status": "passive"}, "connection": {"status": "active"}}
        elif self.path.endswith("/poll"):
            assert self.headers["Authorization"] == "Bearer " + KEY
            result = {"schema": "portal-mcd-poll-response-v1", "contract_version": "1.0", "request_id": body["request_id"], "idempotency_key": body["idempotency_key"], "binding": body["binding"], "next_command_cursor": 0, "retry_after_ms": 1000, "commands": []}
        else:
            assert self.headers["Authorization"] == "Bearer " + KEY
            result = {}
        self.send_response(201)
        self.end_headers()
        self.wfile.write(json.dumps(result).encode())


subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", "/tmp/qa.key", "-out", "/tmp/qa.crt", "-days", "1", "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost"], check=True, capture_output=True)
server = http.server.HTTPServer(("127.0.0.1", 8443), Handler)
context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
context.load_cert_chain("/tmp/qa.crt", "/tmp/qa.key")
server.socket = context.wrap_socket(server.socket, server_side=True)
threading.Thread(target=server.serve_forever, daemon=True).start()
os.environ["SSL_CERT_FILE"] = "/tmp/qa.crt"
# Container has no systemd PID1: record daemon-reload and prohibit any start.
real_systemd = os.environ.get("REAL_SYSTEMD_QA") == "1"
if not real_systemd:
    Path("/usr/local/bin/systemctl").write_text('#!/bin/sh\nif [ "$1" = show ]; then if [ "$3" = --property=UnitFileState ]; then echo disabled; else echo inactive; fi; exit 0; fi\n[ "$1" = daemon-reload ] || exit 99\nprintf "%s\\n" "$1" >> /tmp/systemctl-calls\n')
    Path("/usr/local/bin/systemctl").chmod(0o755)
cmd = ["python3", "/source/tools/mcd_passive_install.py", "--portal-api-base", "https://localhost:8443/api/v1/commercial", "--archive-uri", "https://localhost:8443/runtime", "--archive-sha256", hashlib.sha256(Path("/runtime.tar.gz").read_bytes()).hexdigest(), "--manifest-sha256", hashlib.sha256(manifest_bytes).hexdigest(), "--runtime-version", runtime_version, "--request-id", "qa-passive", "--instance-identity", "passive-qa.example.invalid", "--bridge", "/source/tools/mcd_runtime_bootstrap.py", "--bridge-sha256", hashlib.sha256(Path("/source/tools/mcd_runtime_bootstrap.py").read_bytes()).hexdigest()]
result = subprocess.run(cmd, input=KEY + "\n", text=True, capture_output=True)
assert result.returncode == 0, (result.returncode, result.stdout, result.stderr)
receipt = json.loads(result.stdout)
assert receipt["service_started"] is False and receipt["activation_state"] == "passive"
assert len(calls) == 2
if not real_systemd:
    assert Path("/tmp/systemctl-calls").read_text().strip() == "daemon-reload"
assert KEY not in result.stdout + result.stderr
repeat = subprocess.run(cmd, input=KEY + "\n", text=True, capture_output=True)
assert repeat.returncode == 0 and len(calls) == 2, (repeat.stdout, repeat.stderr)
assert json.loads(repeat.stdout)["idempotent_replay"] is True
wrong_key = subprocess.run(cmd, input="different-test-key-123456789012345\n", text=True, capture_output=True)
assert wrong_key.returncode != 0 and len(calls) == 2
if real_systemd:
    import time
    subprocess.run(["systemd-analyze", "verify", "/etc/systemd/system/mcd.service"], check=True, capture_output=True)
    # This separate lifecycle test serves only an empty command queue. The installer
    # itself never starts a worker, and the fixture cannot supply a scenario.
    dropin = Path("/etc/systemd/system/mcd.service.d/qa.conf")
    dropin.parent.mkdir()
    Path("/tmp/qa-child.py").write_text('import subprocess\nfrom pathlib import Path\np=subprocess.Popen(["/usr/bin/sleep","120"],start_new_session=True)\nPath("/run/mcd-child.pid").write_text(str(p.pid))\n')
    dropin.write_text('[Service]\nEnvironment=SSL_CERT_FILE=/tmp/qa.crt\nExecStartPost=/usr/bin/python3 /tmp/qa-child.py\n')
    subprocess.run(["systemctl", "daemon-reload"], check=True)
    children = []
    for operation in ("start", "restart"):
        subprocess.run(["systemctl", operation, "mcd"], check=True)
        time.sleep(2)
        subprocess.run(["systemctl", "is-active", "--quiet", "mcd"], check=True)
        children.append(int(Path("/run/mcd-child.pid").read_text()))
        for pid in children:
            os.kill(pid, 0)
    subprocess.run(["systemctl", "stop", "mcd"], check=True)
    assert subprocess.check_output(["systemctl", "show", "mcd", "--property=ActiveState", "--value"], text=True).strip() == "inactive"
    assert subprocess.check_output(["systemctl", "show", "mcd", "--property=KillMode", "--value"], text=True).strip() == "process"
    for pid in children:
        os.kill(pid, 0)
        os.kill(pid, 15)
    dropin.unlink()
    dropin.parent.rmdir()
    subprocess.run(["systemctl", "daemon-reload"], check=True)
print(json.dumps({"status": "PASS", "platform": "Ubuntu24.04/amd64", "https_single_key": True, "registration": "host", "fresh_install": True, "completed_replay_without_enrollment": True, "wrong_key_rejected": True, "installer_worker_start_calls": 0, "systemd": "actual start/restart/stop PASS" if real_systemd else "daemon-reload stub; real systemd not validated", "portal": "HTTPS contract double; live Portal not exercised"}))
