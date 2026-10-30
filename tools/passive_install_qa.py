"""Isolated Ubuntu QA fixture; HTTPS Portal contract double, no live credentials."""
import hashlib
import http.server
import json
from pathlib import Path
import socket
import ssl
import subprocess
import threading

KEY = "qa-commercial-key-not-a-live-secret-12345"
calls = []


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
        assert self.headers["User-Agent"].startswith("MCD/")
        calls.append(body)
        if self.path.endswith("redeem-commercial-key"):
            assert body["commercial_key"] == KEY
            assert "public_key" not in body["agent"]
            result = {"schema": "portal-mcd-activation-response-v1", "agent_id": "agent_qa", "agent_credential": KEY, "commercial_key_id": "key_qa"}
        else:
            assert self.headers["Authorization"] == "Bearer " + KEY
            assert body["schema"] == "portal-mcd-host-register-request-v1"
            result = {"installation": {"id": "installation_qa", "agent_id": "agent_qa", "local_identity": socket.gethostname(), "status": "passive"}, "connection": {"status": "active"}}
        self.send_response(201)
        self.end_headers()
        self.wfile.write(json.dumps(result).encode())


subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", "/tmp/qa.key", "-out", "/tmp/qa.crt", "-days", "1", "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost"], check=True, capture_output=True)
server = http.server.HTTPServer(("127.0.0.1", 8443), Handler)
context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
context.load_cert_chain("/tmp/qa.crt", "/tmp/qa.key")
server.socket = context.wrap_socket(server.socket, server_side=True)
threading.Thread(target=server.serve_forever, daemon=True).start()
import os
os.environ["SSL_CERT_FILE"] = "/tmp/qa.crt"
# Container has no systemd PID1: record daemon-reload and prohibit any start.
Path("/usr/local/bin/systemctl").write_text('#!/bin/sh\n[ "$1" = daemon-reload ] || exit 99\nprintf "%s\\n" "$1" >> /tmp/systemctl-calls\n')
Path("/usr/local/bin/systemctl").chmod(0o755)
cmd = ["python3", "/source/tools/mcd_passive_install.py", "--portal-api-base", "https://localhost:8443/api/v1/commercial", "--archive-uri", "https://localhost:8443/runtime", "--archive-sha256", hashlib.sha256(Path("/runtime.tar.gz").read_bytes()).hexdigest(), "--manifest-sha256", "8ab969073cb75f3e765b4e485477b975dc6cd7756341da0fa3f2c3d0b7a458ae", "--runtime-version", "1.6.25", "--request-id", "qa-passive", "--bridge", "/source/tools/mcd_runtime_bootstrap.py"]
result = subprocess.run(cmd, input=KEY + "\n", text=True, capture_output=True)
assert result.returncode == 0, (result.returncode, result.stdout, result.stderr)
receipt = json.loads(result.stdout)
assert receipt["service_started"] is False and receipt["activation_state"] == "passive"
assert len(calls) == 2
assert Path("/tmp/systemctl-calls").read_text().strip() == "daemon-reload"
assert KEY not in result.stdout + result.stderr
repeat = subprocess.run(cmd, input=KEY + "\n", text=True, capture_output=True)
assert repeat.returncode != 0 and len(calls) == 2
print(json.dumps({"status": "PASS", "platform": "Ubuntu24.04/amd64", "https_single_key": True, "registration": "host", "fresh_install": True, "existing_install_refused": True, "worker_start_calls": 0, "systemd": "daemon-reload stub; real systemd not validated", "portal": "HTTPS contract double; live Portal not exercised"}))
