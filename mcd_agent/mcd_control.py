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
