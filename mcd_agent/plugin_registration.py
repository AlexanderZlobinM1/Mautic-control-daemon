"""Mautic registration lifecycle. Settings values remain owned by plugins."""
from __future__ import annotations

from typing import Any


def _tables(db: Any) -> tuple[str, str, str]:
    return tuple(db._safe_table(f"{db.cfg.table_prefix}{name}") for name in (
        "plugins", "plugin_integration_settings", "mcd_plugin_registration"
    ))


def _ensure_journal(cur: Any, journal: str, *, allow_create: bool) -> None:
    cur.execute("SELECT 1 FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s", (journal,))
    if cur.fetchone():
        return
    if not allow_create:
        raise RuntimeError("Plugin registration journal is missing; cluster schema provisioning requires approved maintenance")
    # Registration references only: never copy keys, feature settings or secrets.
    cur.execute(f"""CREATE TABLE IF NOT EXISTS `{journal}` (
        integration_id INT NOT NULL PRIMARY KEY,
        bundle VARCHAR(255) NOT NULL,
        KEY bundle_idx (bundle)
    ) ENGINE=InnoDB""")


def unregister(db: Any, bundles: list[str], *, purge: bool = False, allow_schema_creation: bool = True) -> int:
    """Remove registration atomically, preserving settings unless purge is explicit."""
    bundles = sorted(set(bundles))
    if not bundles:
        return 0
    plugins, integrations, journal = _tables(db)
    marks = ','.join(['%s'] * len(bundles))
    with db._connect() as conn:
        with conn.cursor() as cur:
            if not purge:
                cur.execute(f"SELECT id FROM `{plugins}` WHERE bundle IN ({marks}) LIMIT 1", bundles)
                if not cur.fetchone():
                    return 0
            _ensure_journal(cur, journal, allow_create=allow_schema_creation)
            conn.begin()
            try:
                cur.execute(f"SELECT id FROM `{plugins}` WHERE bundle IN ({marks}) FOR UPDATE", bundles)
                cur.fetchall()
                cur.execute(f"""INSERT INTO `{journal}` (integration_id, bundle)
                    SELECT i.id, p.bundle FROM `{integrations}` i
                    JOIN `{plugins}` p ON p.id=i.plugin_id WHERE p.bundle IN ({marks})
                    ON DUPLICATE KEY UPDATE bundle=VALUES(bundle)""", bundles)
                if purge:
                    cur.execute(f"""DELETE i FROM `{integrations}` i
                        JOIN `{journal}` j ON j.integration_id=i.id
                        LEFT JOIN `{plugins}` p ON p.id=i.plugin_id
                        WHERE j.bundle IN ({marks}) AND (i.plugin_id IS NULL OR p.bundle IN ({marks}))""", bundles + bundles)
                    changed = int(cur.rowcount)
                    cur.execute(f"DELETE FROM `{journal}` WHERE bundle IN ({marks})", bundles)
                else:
                    cur.execute(f"""UPDATE `{integrations}` i JOIN `{plugins}` p ON p.id=i.plugin_id
                        SET i.plugin_id=NULL WHERE p.bundle IN ({marks})""", bundles)
                    changed = int(cur.rowcount)
                cur.execute(f"DELETE FROM `{plugins}` WHERE bundle IN ({marks})", bundles)
                changed += int(cur.rowcount)
                conn.commit()
                return changed
            except Exception:
                conn.rollback()
                raise


def restore(db: Any, bundles: set[str], *, allow_schema_creation: bool = True) -> int:
    """Reattach retained registration references after native plugin installation."""
    if not bundles:
        return 0
    plugins, integrations, journal = _tables(db)
    marks = ','.join(['%s'] * len(bundles))
    with db._connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s", (journal,))
            if not cur.fetchone():
                return 0
            cur.execute(f"""UPDATE `{integrations}` i
                JOIN `{journal}` j ON j.integration_id=i.id
                JOIN `{plugins}` p ON BINARY p.bundle=BINARY j.bundle
                SET i.plugin_id=p.id WHERE i.plugin_id IS NULL AND p.bundle IN ({marks})""", sorted(bundles))
            return int(cur.rowcount)
