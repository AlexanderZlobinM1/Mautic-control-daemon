from __future__ import annotations

import json
import sqlite3
import time
import hashlib
from pathlib import Path

from mcd_agent.config import AgentConfig
from mcd_agent.discovery import discover_mautic
from mcd_agent.instance_uid import build_domain_uid
from mcd_agent.models import DBConfig, MauticInstall
from mcd_agent.secret_store import SecretStore


_ENC_PREFIX = "enc:v1:"


class InstanceInventory:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        key_path = str(Path(db_path).parent / "inventory-secrets.key")
        self._secrets = SecretStore(key_path=key_path)
        self._init_schema()

    def _encrypt_password(self, plain: str | None) -> str | None:
        if plain is None:
            return None
        value = str(plain)
        if not value:
            return None
        if value.startswith(_ENC_PREFIX):
            return value
        return _ENC_PREFIX + self._secrets.encrypt(value)

    def _decrypt_password(self, raw: str | None) -> str | None:
        if raw is None:
            return None
        value = str(raw)
        if not value:
            return None
        if not value.startswith(_ENC_PREFIX):
            return value
        token = value[len(_ENC_PREFIX) :]
        try:
            return self._secrets.decrypt(token)
        except Exception:
            return None

    def _init_schema(self) -> None:
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS instances (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              instance_uid TEXT UNIQUE,
              name TEXT NOT NULL,
              root TEXT NOT NULL UNIQUE,
              primary_domain TEXT,
              domains_json TEXT,
              console_path TEXT NOT NULL,
              local_php_path TEXT,
              mautic_timezone TEXT,
              mautic_major INTEGER,
              source TEXT NOT NULL,
              db_host TEXT,
              db_port INTEGER,
              db_name TEXT,
              db_user TEXT,
              db_password TEXT,
              db_table_prefix TEXT,
              updated_at REAL NOT NULL
            )
            """
        )
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_instances_source ON instances(source)")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_instances_name ON instances(name)")

        cols = self.conn.execute("PRAGMA table_info(instances)").fetchall()
        names = {str(r["name"]) for r in cols}
        name_is_pk = any(str(r["name"]) == "name" and int(r["pk"] or 0) == 1 for r in cols)
        if "mautic_timezone" not in names:
            self.conn.execute("ALTER TABLE instances ADD COLUMN mautic_timezone TEXT")
        if "instance_uid" not in names:
            self.conn.execute("ALTER TABLE instances ADD COLUMN instance_uid TEXT")
        if "primary_domain" not in names:
            self.conn.execute("ALTER TABLE instances ADD COLUMN primary_domain TEXT")
        if "domains_json" not in names:
            self.conn.execute("ALTER TABLE instances ADD COLUMN domains_json TEXT")
        # Migration from legacy schema where name was PRIMARY KEY.
        if name_is_pk:
            self._migrate_instances_legacy_pk_name()
        self.conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_instances_uid ON instances(instance_uid)")
        self._ensure_instance_uids()
        self._migrate_db_passwords_encrypted()
        self.conn.commit()

    def _migrate_db_passwords_encrypted(self) -> None:
        rows = self.conn.execute("SELECT id, db_password FROM instances WHERE db_password IS NOT NULL").fetchall()
        for r in rows:
            current = str(r["db_password"] or "")
            if not current:
                continue
            if current.startswith(_ENC_PREFIX):
                continue
            enc = self._encrypt_password(current)
            self.conn.execute("UPDATE instances SET db_password=? WHERE id=?", (enc, int(r["id"])))

    def _ensure_instance_uids(self) -> None:
        rows = self.conn.execute("SELECT id, name, root, instance_uid FROM instances ORDER BY id ASC").fetchall()
        for r in rows:
            cur = str(r["instance_uid"] or "").strip()
            if cur:
                continue
            uid = build_domain_uid(domain=None, root=str(r["root"]), name=str(r["name"]))
            self.conn.execute("UPDATE instances SET instance_uid=? WHERE id=?", (uid, int(r["id"])))
        # Ensure uniqueness even if legacy generated duplicates.
        rows2 = self.conn.execute("SELECT id, root, instance_uid FROM instances ORDER BY id ASC").fetchall()
        seen: set[str] = set()
        for r in rows2:
            uid = str(r["instance_uid"] or "").strip()
            if not uid:
                continue
            if uid in seen:
                root = str(r["root"])
                suffix = hashlib.blake2s(root.encode("utf-8"), digest_size=2).hexdigest()
                self.conn.execute("UPDATE instances SET instance_uid=? WHERE id=?", (f"{uid}-{suffix}", int(r["id"])))
                continue
            seen.add(uid)

    def _migrate_instances_legacy_pk_name(self) -> None:
        self.conn.execute("ALTER TABLE instances RENAME TO instances_legacy")
        self.conn.execute(
            """
            CREATE TABLE instances (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              instance_uid TEXT UNIQUE,
              name TEXT NOT NULL,
              root TEXT NOT NULL UNIQUE,
              primary_domain TEXT,
              domains_json TEXT,
              console_path TEXT NOT NULL,
              local_php_path TEXT,
              mautic_timezone TEXT,
              mautic_major INTEGER,
              source TEXT NOT NULL,
              db_host TEXT,
              db_port INTEGER,
              db_name TEXT,
              db_user TEXT,
              db_password TEXT,
              db_table_prefix TEXT,
              updated_at REAL NOT NULL
            )
            """
        )
        self.conn.execute(
            """
            INSERT OR REPLACE INTO instances(
              instance_uid, name, root, primary_domain, domains_json, console_path, local_php_path, mautic_timezone, mautic_major, source,
              db_host, db_port, db_name, db_user, db_password, db_table_prefix, updated_at
            )
            SELECT
              NULL, name, root, NULL, NULL, console_path, local_php_path, mautic_timezone, mautic_major, source,
              db_host, db_port, db_name, db_user, db_password, db_table_prefix, updated_at
            FROM instances_legacy
            ORDER BY updated_at ASC
            """
        )
        self.conn.execute("DROP TABLE instances_legacy")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_instances_source ON instances(source)")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_instances_name ON instances(name)")
        self.conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_instances_uid ON instances(instance_uid)")

    def _resolve_unique_uid(self, uid: str, root: str) -> str:
        row = self.conn.execute("SELECT root FROM instances WHERE instance_uid=?", (uid,)).fetchone()
        if row is None or str(row["root"]) == root:
            return uid
        suffix = hashlib.blake2s(root.encode("utf-8"), digest_size=2).hexdigest()
        return f"{uid}-{suffix}"

    def _upsert_install(self, inst: MauticInstall, source: str | None = None) -> None:
        db = inst.db
        uid = self._resolve_unique_uid(inst.instance_uid or build_domain_uid(domain=inst.primary_domain, root=inst.root, name=inst.name), inst.root)
        self.conn.execute(
            """
            INSERT INTO instances(
              instance_uid, name, root, primary_domain, domains_json, console_path, local_php_path, mautic_timezone, mautic_major, source,
              db_host, db_port, db_name, db_user, db_password, db_table_prefix, updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(root) DO UPDATE SET
              instance_uid=excluded.instance_uid,
              name=excluded.name,
              root=excluded.root,
              primary_domain=excluded.primary_domain,
              domains_json=excluded.domains_json,
              console_path=excluded.console_path,
              local_php_path=excluded.local_php_path,
              mautic_timezone=excluded.mautic_timezone,
              mautic_major=excluded.mautic_major,
              source=excluded.source,
              db_host=excluded.db_host,
              db_port=excluded.db_port,
              db_name=excluded.db_name,
              db_user=excluded.db_user,
              db_password=excluded.db_password,
              db_table_prefix=excluded.db_table_prefix,
              updated_at=excluded.updated_at
            """,
            (
                uid,
                inst.name,
                inst.root,
                inst.primary_domain,
                json.dumps(list(inst.domains or []), ensure_ascii=False) if (inst.domains or []) else None,
                inst.console_path,
                inst.local_php_path,
                inst.mautic_timezone,
                inst.mautic_major,
                source or inst.source,
                db.host if db else None,
                db.port if db else None,
                db.name if db else None,
                db.user if db else None,
                self._encrypt_password(db.password) if db else None,
                db.table_prefix if db else None,
                time.time(),
            ),
        )

    def list_instances(self) -> list[MauticInstall]:
        rows = self.conn.execute("SELECT * FROM instances ORDER BY root, name").fetchall()
        out: list[MauticInstall] = []
        for r in rows:
            db = None
            db_password = self._decrypt_password(str(r["db_password"]) if r["db_password"] is not None else None)
            if r["db_host"] and r["db_name"] and r["db_user"] and db_password:
                db = DBConfig(
                    host=str(r["db_host"]),
                    port=int(r["db_port"] or 3306),
                    name=str(r["db_name"]),
                    user=str(r["db_user"]),
                    password=str(db_password),
                    table_prefix=str(r["db_table_prefix"] or ""),
                )
            out.append(
                MauticInstall(
                    instance_uid=str(r["instance_uid"] or build_domain_uid(domain=str(r["primary_domain"] or ""), root=str(r["root"]), name=str(r["name"]))),
                    name=str(r["name"]),
                    root=str(r["root"]),
                    primary_domain=str(r["primary_domain"]) if r["primary_domain"] else None,
                    console_path=str(r["console_path"]),
                    local_php_path=str(r["local_php_path"]) if r["local_php_path"] else None,
                    mautic_timezone=str(r["mautic_timezone"]) if r["mautic_timezone"] else None,
                    mautic_major=int(r["mautic_major"]) if r["mautic_major"] is not None else None,
                    db=db,
                    source=str(r["source"]),
                    markers=[],
                    domains=(
                        [str(x).strip().lower() for x in json.loads(str(r["domains_json"] or "[]")) if str(x).strip()]
                        if str(r["domains_json"] or "").strip()
                        else ([str(r["primary_domain"]).strip().lower()] if r["primary_domain"] else [])
                    ),
                )
            )
        return out

    def count(self) -> int:
        row = self.conn.execute("SELECT COUNT(*) AS cnt FROM instances").fetchone()
        return int(row["cnt"]) if row else 0

    def rescan(self, config: AgentConfig, *, preserve_manual: bool = False) -> int:
        installs = discover_mautic(
            config.discovery_roots,
            config.exclude_path_contains,
            config.supported_mautic_majors,
            config.custom_instances,
        )
        if preserve_manual:
            self.conn.execute("DELETE FROM instances WHERE source='autodiscovery'")
        else:
            self.conn.execute("DELETE FROM instances WHERE source IN ('autodiscovery','manual')")
        for inst in installs:
            self._upsert_install(inst, source=inst.source or "autodiscovery")
        self.conn.commit()
        return len(installs)

    def add_or_update_manual(
        self,
        *,
        name: str,
        root: str,
        console_path: str,
        local_php_path: str | None,
        mautic_major: int | None,
        db_host: str | None,
        db_port: int | None,
        db_name: str | None,
        db_user: str | None,
        db_password: str | None,
        db_table_prefix: str | None,
    ) -> None:
        db = None
        if db_host and db_name and db_user and db_password:
            db = DBConfig(
                host=db_host,
                port=db_port or 3306,
                name=db_name,
                user=db_user,
                password=db_password,
                table_prefix=db_table_prefix or "",
            )
        inst = MauticInstall(
            instance_uid=build_domain_uid(domain=None, root=root, name=name),
            name=name,
            root=root,
            primary_domain=None,
            console_path=console_path,
            local_php_path=local_php_path,
            mautic_timezone=None,
            mautic_major=mautic_major,
            db=db,
            source="manual",
            markers=[],
        )
        self._upsert_install(inst, source="manual")
        self.conn.commit()

    def remove(self, name: str) -> bool:
        cur = self.conn.execute("DELETE FROM instances WHERE root=?", (name,))
        if int(cur.rowcount) <= 0:
            cur = self.conn.execute("DELETE FROM instances WHERE instance_uid=?", (name,))
        if int(cur.rowcount) <= 0:
            cur = self.conn.execute("DELETE FROM instances WHERE name=?", (name,))
        self.conn.commit()
        return int(cur.rowcount) > 0


def ensure_seeded(inventory: InstanceInventory, config: AgentConfig) -> int:
    if inventory.count() > 0:
        return inventory.count()
    return inventory.rescan(config)
