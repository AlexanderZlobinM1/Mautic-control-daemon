import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest

from mcd_agent import instance_delete
from mcd_agent.config import runtime_overrides_from_config_file


TARGET = "fresh.example.invalid"
KEEP = "keep.example.invalid"


def _db(path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(path)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute(
        "CREATE TABLE runtime_sync (key TEXT PRIMARY KEY,payload_json TEXT NOT NULL,updated_at REAL NOT NULL)"
    )
    db.execute("CREATE TABLE commercial_meta (key TEXT PRIMARY KEY,value TEXT NOT NULL)")
    return db


def test_cleanup_removes_exact_target_state_and_physical_wal_bytes(tmp_path):
    path = tmp_path / "mcd-state.db"
    config_path = tmp_path / "mcd.toml"
    config_path.write_text(
        "[runtime]\n"
        f'message_queue_instance_settings = {{ "{TARGET}" = {{ workers = 2 }}, '
        f'"{KEEP}" = {{ workers = 7, sentinel = "config-preserve" }} }}\n'
        "jobs_max_workers = 9\n",
        encoding="utf-8",
    )
    db = _db(path)
    local_runtime = {
        "message_queue_instance_settings": {
            TARGET: {"workers": 2},
            KEEP: {"workers": 7, "sentinel": "preserve-me"},
        },
        "host_setting": "unchanged",
    }
    commercial = {
        "values": {
            "segment_priority_instance_settings": {
                TARGET: {"segments": [1]},
                KEEP: {"segments": [9], "sentinel": "commercial-preserve"},
            }
        }
    }
    db.execute(
        "INSERT INTO runtime_sync VALUES(?,?,?)",
        ("local_runtime", json.dumps(local_runtime), 1.0),
    )
    db.execute(
        "INSERT INTO runtime_sync VALUES(?,?,?)",
        (
            f"plugin_inventory:{TARGET}",
            json.dumps({"instance_uid": TARGET, "root": "/var/www/fresh/public_html"}),
            1.0,
        ),
    )
    db.execute(
        "INSERT INTO runtime_sync VALUES(?,?,?)",
        (f"plugin_inventory:{KEEP}", json.dumps({"instance_uid": KEEP}), 1.0),
    )
    db.execute(
        "INSERT INTO commercial_meta VALUES(?,?)",
        ("commercial_standalone_settings_snapshot_v1", json.dumps(commercial)),
    )
    db.execute(
        "INSERT INTO commercial_meta VALUES(?,?)",
        ("unrelated", json.dumps({"sentinel": "leave-this-row-byte-for-byte"})),
    )
    db.commit()
    db.close()

    result = instance_delete._cleanup_instance_state(
        SimpleNamespace(state_db_path=str(path), config_file_path=str(config_path)), [TARGET]
    )

    assert result["status"] == "ok"
    assert result["target_refs_before"] > 0
    assert result["target_refs_after"] == 0
    assert result["physical_target_refs"] == {"db": 0, "wal": 0, "shm": 0}
    assert result["unrelated_preserved"] is True
    assert result["config_cleanup"]["status"] == "ok"
    assert result["config_cleanup"]["target_refs_before"] == 1
    assert result["config_cleanup"]["target_refs_after"] == 0
    assert runtime_overrides_from_config_file(str(config_path)) == {
        "jobs_max_workers": 9,
        "message_queue_instance_settings": {
            KEEP: {"sentinel": "config-preserve", "workers": 7}
        },
    }
    assert result["deleted_keys"] == [f"plugin_inventory:{TARGET}"]
    assert result["updated_keys"] == [
        "commercial_standalone_settings_snapshot_v1",
        "local_runtime",
    ]

    db = sqlite3.connect(path)
    runtime = json.loads(
        db.execute("SELECT payload_json FROM runtime_sync WHERE key='local_runtime'").fetchone()[0]
    )
    assert runtime == {
        "host_setting": "unchanged",
        "message_queue_instance_settings": {
            KEEP: {"sentinel": "preserve-me", "workers": 7}
        },
    }
    assert db.execute(
        "SELECT COUNT(*) FROM runtime_sync WHERE key=?", (f"plugin_inventory:{TARGET}",)
    ).fetchone()[0] == 0
    assert json.loads(db.execute(
        "SELECT payload_json FROM runtime_sync WHERE key=?", (f"plugin_inventory:{KEEP}",)
    ).fetchone()[0]) == {"instance_uid": KEEP}
    cleaned_commercial = json.loads(db.execute(
        "SELECT value FROM commercial_meta WHERE key='commercial_standalone_settings_snapshot_v1'"
    ).fetchone()[0])
    assert cleaned_commercial["values"]["segment_priority_instance_settings"] == {
        KEEP: {"segments": [9], "sentinel": "commercial-preserve"}
    }
    assert db.execute(
        "SELECT value FROM commercial_meta WHERE key='unrelated'"
    ).fetchone()[0] == json.dumps({"sentinel": "leave-this-row-byte-for-byte"})
    db.close()

    for suffix in ("", "-wal", "-shm"):
        item = Path(str(path) + suffix)
        raw = item.read_bytes().lower() if item.exists() else b""
        assert TARGET.encode() not in raw


def test_cleanup_fails_closed_on_unknown_target_reference_shape(tmp_path):
    path = tmp_path / "mcd-state.db"
    db = _db(path)
    payload = json.dumps({"unowned_scalar": TARGET, "sentinel": "preserve"})
    db.execute("INSERT INTO runtime_sync VALUES(?,?,?)", ("unknown_shape", payload, 1.0))
    db.commit()
    db.close()

    with pytest.raises(RuntimeError, match="unsupported target state reference"):
        instance_delete._cleanup_instance_state(
            SimpleNamespace(state_db_path=str(path)), [TARGET]
        )

    db = sqlite3.connect(path)
    assert db.execute(
        "SELECT payload_json FROM runtime_sync WHERE key='unknown_shape'"
    ).fetchone()[0] == payload
    db.close()
