from __future__ import annotations

import json
import re
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
              runtime TEXT NOT NULL DEFAULT 'host',
              runtime_id TEXT,
              runtime_root TEXT,
              runtime_user TEXT,
              runtime_php_bin TEXT,
              runtime_image_ref TEXT,
              install_type TEXT,
              runtime_capabilities_json TEXT,
              runtime_adapter TEXT,
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
        for column, sql_type in {
            "runtime": "TEXT NOT NULL DEFAULT 'host'",
            "runtime_id": "TEXT",
            "runtime_root": "TEXT",
            "runtime_user": "TEXT",
            "runtime_php_bin": "TEXT",
            "runtime_image_ref": "TEXT",
            "install_type": "TEXT",
            "runtime_capabilities_json": "TEXT",
            "runtime_adapter": "TEXT",
        }.items():
            if column not in names:
                self.conn.execute(f"ALTER TABLE instances ADD COLUMN {column} {sql_type}")
        # Migration from legacy schema where name was PRIMARY KEY.
        if name_is_pk:
            self._migrate_instances_legacy_pk_name()
        self.conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_instances_uid ON instances(instance_uid)")
        self._ensure_instance_uids()
        if "registering_operation_id" not in {str(row["name"]) for row in self.conn.execute("PRAGMA table_info(instances)")} :
            self.conn.execute("ALTER TABLE instances ADD COLUMN registering_operation_id TEXT")
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
              runtime TEXT NOT NULL DEFAULT 'host',
              runtime_id TEXT,
              runtime_root TEXT,
              runtime_user TEXT,
              runtime_php_bin TEXT,
              runtime_image_ref TEXT,
              install_type TEXT,
              runtime_capabilities_json TEXT,
              runtime_adapter TEXT,
              updated_at REAL NOT NULL
            )
            """
        )
        self.conn.execute(
            """
            INSERT OR REPLACE INTO instances(
              instance_uid, name, root, primary_domain, domains_json, console_path, local_php_path, mautic_timezone, mautic_major, source,
              db_host, db_port, db_name, db_user, db_password, db_table_prefix,
              runtime, runtime_id, runtime_root, runtime_user, runtime_php_bin, runtime_image_ref,
              install_type, runtime_capabilities_json, runtime_adapter, updated_at
            )
            SELECT
              NULL, name, root, NULL, NULL, console_path, local_php_path, mautic_timezone, mautic_major, source,
              db_host, db_port, db_name, db_user, db_password, db_table_prefix,
              'host', NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, updated_at
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
              db_host, db_port, db_name, db_user, db_password, db_table_prefix,
              runtime, runtime_id, runtime_root, runtime_user, runtime_php_bin, runtime_image_ref,
              install_type, runtime_capabilities_json, runtime_adapter, updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
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
              runtime=excluded.runtime,
              runtime_id=excluded.runtime_id,
              runtime_root=excluded.runtime_root,
              runtime_user=excluded.runtime_user,
              runtime_php_bin=excluded.runtime_php_bin,
              runtime_image_ref=excluded.runtime_image_ref,
              install_type=excluded.install_type,
              runtime_capabilities_json=excluded.runtime_capabilities_json,
              runtime_adapter=excluded.runtime_adapter,
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
                str(inst.runtime or "host"),
                inst.runtime_id,
                inst.runtime_root,
                inst.runtime_user,
                inst.runtime_php_bin,
                inst.runtime_image_ref,
                inst.install_type,
                json.dumps(sorted(set(inst.runtime_capabilities or [])), ensure_ascii=True),
                inst.runtime_adapter,
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
                    runtime=str(r["runtime"] or "host"),
                    runtime_id=str(r["runtime_id"]) if r["runtime_id"] else None,
                    runtime_root=str(r["runtime_root"]) if r["runtime_root"] else None,
                    runtime_user=str(r["runtime_user"]) if r["runtime_user"] else None,
                    runtime_php_bin=str(r["runtime_php_bin"]) if r["runtime_php_bin"] else None,
                    runtime_image_ref=(
                        str(r["runtime_image_ref"]) if r["runtime_image_ref"] else None
                    ),
                    install_type=str(r["install_type"]) if r["install_type"] else None,
                    runtime_capabilities=(
                        [str(x) for x in json.loads(str(r["runtime_capabilities_json"] or "[]"))]
                        if str(r["runtime_capabilities_json"] or "").strip()
                        else []
                    ),
                    runtime_adapter=str(r["runtime_adapter"]) if r["runtime_adapter"] else None,
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
        tokens = {str(row["root"]): row["registering_operation_id"] for row in self.conn.execute("SELECT root, registering_operation_id FROM instances")}
        previous = {inst.root: inst for inst in self.list_instances()}
        seen_roots = set()
        for inst in installs:
            if inst.root in seen_roots:
                raise ValueError("Ambiguous discovered Mautic root")
            seen_roots.add(inst.root)
            old = previous.get(inst.root)
            if old and old.runtime == inst.runtime:
                inst.instance_uid = old.instance_uid
                if inst.runtime == "host":
                    inst.runtime_php_bin = inst.runtime_php_bin or old.runtime_php_bin
                    inst.runtime_user = inst.runtime_user or old.runtime_user
        # Explicit runtime registration must survive a pre-vhost rescan.
        installs.extend(old for root, old in previous.items()
                        if root not in seen_roots and old.source == "manual"
                        and old.runtime == "host" and old.runtime_php_bin and old.runtime_user)
        with self.conn:
            if preserve_manual:
                self.conn.execute(
                    "DELETE FROM instances WHERE source IN ('autodiscovery','runtime-descriptor')"
                )
            else:
                self.conn.execute(
                    "DELETE FROM instances WHERE source IN ('autodiscovery','manual','runtime-descriptor')"
                )
            for inst in installs:
                self._upsert_install(inst, source=inst.source or "autodiscovery")
                if tokens.get(inst.root):
                    self.conn.execute("UPDATE instances SET registering_operation_id=? WHERE root=?", (tokens[inst.root], inst.root))
        return len(installs)

    def add_or_update_manual(self, *, registration_receipt: bool = False, registering_operation_id: str | None = None, **kwargs):
        with self.conn:
            self.conn.execute("BEGIN IMMEDIATE")
            previous = self.conn.execute("SELECT * FROM instances WHERE root=?", (kwargs["root"],)).fetchone()
            if registration_receipt:
                if not isinstance(registering_operation_id, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.:-]{0,254}", registering_operation_id):
                    raise ValueError("Registration receipt requires the existing operation ID")
                if not all(kwargs.get(key) for key in ("instance_uid", "runtime_php_bin", "runtime_user")):
                    raise ValueError("Registration receipt requires UID and complete runtime")
                if previous:
                    for key in ("instance_uid", "console_path", "runtime_php_bin", "runtime_user"):
                        if previous[key] and previous[key] != kwargs[key]:
                            raise ValueError("Existing registration runtime conflicts with declared facts")
                if previous:
                    php, user = kwargs["runtime_php_bin"], kwargs["runtime_user"]
                    if not Path(php).is_absolute() or not re.fullmatch(r"[a-zA-Z0-9_.-]+", user):
                        raise ValueError("Invalid registered runtime PHP or user")
                    self.conn.execute("UPDATE instances SET runtime_php_bin=?, runtime_user=? WHERE root=?", (php, user, kwargs["root"]))
                    return {"status": "ok", "instance_uid": kwargs["instance_uid"], "root": kwargs["root"],
                            "created": False, "registering_operation_id": None}
            self._add_or_update_manual(**kwargs)
            if not registration_receipt:
                return None
            token = registering_operation_id if previous is None else None
            if token:
                self.conn.execute("UPDATE instances SET registering_operation_id=? WHERE instance_uid=? AND root=?", (token, kwargs["instance_uid"], kwargs["root"]))
            return {"status": "ok", "instance_uid": kwargs["instance_uid"], "root": kwargs["root"],
                    "created": previous is None, "registering_operation_id": token}

    def remove_owned_registration(self, *, instance_uid: str, root: str, registering_operation_id: str) -> bool:
        if not instance_uid or not root or not registering_operation_id:
            raise ValueError("Owned removal requires UID, root and registration token")
        with self.conn:
            cursor = self.conn.execute("DELETE FROM instances WHERE instance_uid=? AND root=? AND registering_operation_id=?", (instance_uid, root, registering_operation_id))
            return cursor.rowcount == 1

    def _add_or_update_manual(
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
        instance_uid: str | None = None,
        runtime_php_bin: str | None = None,
        runtime_user: str | None = None,
    ) -> None:
        if bool(runtime_php_bin) != bool(runtime_user):
            raise ValueError("Runtime PHP and user must be supplied together")
        if runtime_php_bin and (not Path(runtime_php_bin).is_absolute() or not re.fullmatch(r"[a-zA-Z0-9_.-]+", runtime_user or "")):
            raise ValueError("Invalid registered runtime PHP or user")
        if instance_uid and not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,254}", instance_uid):
            raise ValueError("Invalid stable Mautic instance UID")
        if (instance_uid or runtime_php_bin) and (not Path(root).is_absolute() or not Path(console_path).is_absolute()):
            raise ValueError("Registered runtime paths must be absolute")
        existing = next((item for item in self.list_instances() if item.root == root), None)
        uid = instance_uid or (existing.instance_uid if existing else build_domain_uid(domain=None, root=root, name=name))
        if existing and instance_uid and existing.instance_uid != uid:
            raise ValueError("Root already belongs to another stable instance UID")
        collision = self.conn.execute("SELECT root FROM instances WHERE instance_uid=?", (uid,)).fetchone()
        if collision and str(collision["root"]) != root:
            raise ValueError("Stable instance UID already belongs to another root")
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
            instance_uid=uid,
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
            runtime_php_bin=runtime_php_bin or (existing.runtime_php_bin if existing else None),
            runtime_user=runtime_user or (existing.runtime_user if existing else None),
        )
        self._upsert_install(inst, source="manual")

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
