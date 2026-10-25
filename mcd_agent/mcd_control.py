from __future__ import annotations
import json, sqlite3, time
from pathlib import Path
from typing import Any, Mapping

SCHEMA = "mcd-control-v1"
COMMAND = "docker_runtime_preflight_v1"

class ControlStore:
    def __init__(self, path: str):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as db:
            db.execute("CREATE TABLE IF NOT EXISTS mcd_control_jobs (job_id TEXT PRIMARY KEY, request_json TEXT NOT NULL, receipt_json TEXT NOT NULL, updated_at REAL NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS mcd_result_key_ack (key_id TEXT PRIMARY KEY, ack_json TEXT NOT NULL, updated_at REAL NOT NULL)")

    def receipts(self) -> list[dict[str, Any]]:
        with sqlite3.connect(self.path) as db:
            rows = db.execute("SELECT receipt_json FROM mcd_control_jobs ORDER BY updated_at DESC LIMIT 100").fetchall()
        return [json.loads(row[0]) for row in rows]

    def ingest(self, envelope: Mapping[str, Any], *, agent_id: str, host_fingerprint: str) -> list[dict[str, Any]]:
        if envelope.get("schema") != SCHEMA or envelope.get("contract_version") != "1.0":
            raise ValueError("mcd_control_schema_invalid")
        commands = envelope.get("commands")
        if not isinstance(commands, list):
            raise ValueError("mcd_control_commands_invalid")
        out = []
        for command in commands:
            if not isinstance(command, dict) or command.get("command") != COMMAND or command.get("agent_id") != agent_id or command.get("host_fingerprint") != host_fingerprint:
                raise ValueError("mcd_control_command_binding_invalid")
            if any(key in command for key in ("shell", "script", "argv", "command_line")):
                raise ValueError("mcd_control_arbitrary_payload_forbidden")
            job_id = str(command.get("job_id") or "")
            if not job_id:
                raise ValueError("mcd_control_job_id_missing")
            with sqlite3.connect(self.path) as db:
                row = db.execute("SELECT receipt_json FROM mcd_control_jobs WHERE job_id=?", (job_id,)).fetchone()
                if row:
                    out.append(json.loads(row[0])); continue
                receipt = {"schema": "mcd-control-receipt-v1", "contract_version": "1.0", "job_id": job_id, "request_id": str(command.get("request_id") or ""), "status": "accepted", "idempotency_key": job_id, "command": COMMAND, "updated_at": int(time.time())}
                db.execute("INSERT INTO mcd_control_jobs VALUES (?,?,?,?)", (job_id, json.dumps(command, sort_keys=True), json.dumps(receipt, sort_keys=True), time.time()))
                db.commit()
                out.append(receipt)
        return out

    def record_key_ack(self, ack: Mapping[str, Any]) -> None:
        with sqlite3.connect(self.path) as db:
            db.execute("INSERT OR REPLACE INTO mcd_result_key_ack VALUES (?,?,?)", (str(ack["key_id"]), json.dumps(dict(ack), sort_keys=True), time.time()))
            db.commit()

    def active_key_ack(self, key_id: str) -> dict[str, Any] | None:
        with sqlite3.connect(self.path) as db:
            row = db.execute("SELECT ack_json FROM mcd_result_key_ack WHERE key_id=?", (key_id,)).fetchone()
        if not row:
            return None
        value = json.loads(row[0])
        return value if value.get("status") == "active" else None

    def execute_pending(self, *, agent_id: str, host_fingerprint: str, active_key: tuple[Any, str] | None, runner: Any, signer: Any) -> list[dict[str, Any]]:
        if not active_key:
            return []
        private, key_id = active_key
        with sqlite3.connect(self.path) as db:
            rows = db.execute("SELECT job_id,request_json,receipt_json FROM mcd_control_jobs WHERE receipt_json NOT LIKE '%\\\"status\\\": \\\"succeeded\\\"%' AND receipt_json NOT LIKE '%\\\"status\\\": \\\"failed\\\"%'").fetchall()
        out = []
        for job_id, request_json, _receipt_json in rows:
            request = json.loads(request_json)
            with sqlite3.connect(self.path) as db:
                db.execute("UPDATE mcd_control_jobs SET receipt_json=?,updated_at=? WHERE job_id=?", (json.dumps({"job_id": job_id, "status": "running", "idempotency_key": job_id}, sort_keys=True), time.time(), job_id)); db.commit()
            try:
                result = dict(runner(request))
                terminal = signer({"schema": "mcd-docker-runtime-preflight-terminal-v1", "contract_version": "1.0", "job_id": job_id, "request_id": request["request_id"], "probe_id": request["probe_id"], "status": "succeeded", "idempotency_key": job_id, "result": result}, private, key_id)
            except Exception as exc:
                terminal = {"schema": "mcd-docker-runtime-preflight-terminal-v1", "contract_version": "1.0", "job_id": job_id, "request_id": request.get("request_id"), "probe_id": request.get("probe_id"), "status": "failed", "idempotency_key": job_id, "error": type(exc).__name__}
            with sqlite3.connect(self.path) as db:
                db.execute("UPDATE mcd_control_jobs SET receipt_json=?,updated_at=? WHERE job_id=?", (json.dumps(terminal, sort_keys=True), time.time(), job_id)); db.commit()
            out.append(terminal)
        return out
