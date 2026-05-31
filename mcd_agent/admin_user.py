from __future__ import annotations

import json
import os
import subprocess
import tempfile
import textwrap
from typing import Any

from mcd_agent.config import AgentConfig
from mcd_agent.db import MauticDB
from mcd_agent.inventory import InstanceInventory, ensure_seeded
from mcd_agent.models import MauticInstall


def _select_instance(cfg: AgentConfig, root: str | None) -> MauticInstall:
    inv = InstanceInventory(cfg.state_db_path)
    ensure_seeded(inv, cfg)
    installs = inv.list_instances()
    if root:
        for inst in installs:
            if inst.root == root or inst.instance_uid == root:
                return inst
        raise RuntimeError(f"Mautic install not found for root: {root}")
    if not installs:
        raise RuntimeError("No Mautic install found")
    if len(installs) > 1:
        roots = ", ".join(x.root for x in installs)
        raise RuntimeError(f"Multiple installs found, pass --root: {roots}")
    return installs[0]


def reset_admin_password(
    cfg: AgentConfig,
    *,
    root: str | None,
    username: str,
    email: str,
    first_name: str,
    last_name: str,
    password_hash: str,
) -> dict[str, Any]:
    inst = _select_instance(cfg, root)
    if inst.db is None:
        raise RuntimeError(f"Database credentials not found for instance: {inst.root}")

    username_clean = str(username or "").strip()
    email_clean = str(email or "").strip()
    first_name_clean = str(first_name or "").strip()
    last_name_clean = str(last_name or "").strip()
    password_hash_clean = str(password_hash or "").strip()
    if not username_clean:
        raise RuntimeError("username is required")
    if not email_clean:
        raise RuntimeError("email is required")
    if not password_hash_clean:
        raise RuntimeError("password_hash is required")

    prefix = str(inst.db.table_prefix or "")
    users_table = f"`{prefix}users`"
    roles_table = f"`{prefix}roles`"
    db = MauticDB(inst.db)

    with db._connect() as conn:
        with conn.cursor() as cur:
            cur.execute(f"SELECT `id` FROM {roles_table} WHERE `is_admin`=1 ORDER BY `id` ASC LIMIT 1")
            role_row = cur.fetchone() or {}
            role_id = int(role_row.get("id") or 0)
            if role_id <= 0:
                raise RuntimeError("admin role not found")

            cur.execute(
                f"SELECT `id`,`timezone`,`locale`,`date_added` "
                f"FROM {users_table} "
                f"WHERE `username`=%s OR `email`=%s "
                f"ORDER BY `id` ASC",
                (username_clean, email_clean),
            )
            matches = list(cur.fetchall() or [])
            keep_row = matches[0] if matches else None
            keep_id = int((keep_row or {}).get("id") or 0)

            if len(matches) > 1:
                dup_ids = [int((r or {}).get("id") or 0) for r in matches[1:]]
                dup_ids = [x for x in dup_ids if x > 0]
                if dup_ids:
                    placeholders = ",".join(["%s"] * len(dup_ids))
                    cur.execute(f"DELETE FROM {users_table} WHERE `id` IN ({placeholders})", dup_ids)

            timezone = str((keep_row or {}).get("timezone") or "UTC").strip() or "UTC"
            locale = str((keep_row or {}).get("locale") or "en_US").strip() or "en_US"
            if keep_id > 0:
                cur.execute(
                    f"UPDATE {users_table} "
                    f"SET `role_id`=%s, `username`=%s, `password`=%s, `first_name`=%s, `last_name`=%s, "
                    f"`email`=%s, `timezone`=%s, `locale`=%s, `is_published`=1, `last_login`=NULL "
                    f"WHERE `id`=%s",
                    (
                        role_id,
                        username_clean,
                        password_hash_clean,
                        first_name_clean,
                        last_name_clean,
                        email_clean,
                        timezone,
                        locale,
                        keep_id,
                    ),
                )
                action = "updated"
                user_id = keep_id
            else:
                cur.execute(
                    f"INSERT INTO {users_table} "
                    f"(`role_id`,`username`,`password`,`first_name`,`last_name`,`email`,`timezone`,`locale`,`is_published`,`date_added`,`last_login`) "
                    f"VALUES (%s,%s,%s,%s,%s,%s,%s,%s,1,NOW(),NULL)",
                    (
                        role_id,
                        username_clean,
                        password_hash_clean,
                        first_name_clean,
                        last_name_clean,
                        email_clean,
                        "UTC",
                        "en_US",
                    ),
                )
                action = "inserted"
                user_id = int(cur.lastrowid or 0)

            cur.execute(
                f"SELECT `id`,`username`,`email`,`role_id`,`is_published` "
                f"FROM {users_table} WHERE `id`=%s",
                (user_id,),
            )
            row = cur.fetchone() or {}

    return {
        "status": "ok",
        "action": action,
        "instance": inst.instance_uid,
        "root": inst.root,
        "db_name": inst.db.name,
        "table_prefix": prefix,
        "user": {
            "id": int((row or {}).get("id") or 0),
            "username": str((row or {}).get("username") or ""),
            "email": str((row or {}).get("email") or ""),
            "role_id": int((row or {}).get("role_id") or 0),
            "is_published": int((row or {}).get("is_published") or 0),
        },
    }


def _resolve_hostnet_bundle_dir(root: str) -> str | None:
    base = str(root or "").strip()
    if not base:
        return None
    candidates = [
        os.path.join(base, "plugins", "HostnetAuthBundle"),
        os.path.join(base, "docroot", "plugins", "HostnetAuthBundle"),
        os.path.join(base, "public", "plugins", "HostnetAuthBundle"),
    ]
    for path in candidates:
        if os.path.isfile(os.path.join(path, "HostnetAuthBundle.php")):
            return path
    return None


def _hostnet_php_runner_script() -> str:
    return textwrap.dedent(
        """\
        <?php
        declare(strict_types=1);

        function emit(array $payload, int $rc = 0): void {
            echo json_encode($payload, JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE);
            exit($rc);
        }

        $opt = getopt('', ['root:', 'username:', 'email:', 'action:']);
        $root = rtrim((string) ($opt['root'] ?? ''), '/');
        $username = trim((string) ($opt['username'] ?? ''));
        $email = trim((string) ($opt['email'] ?? ''));
        $action = trim((string) ($opt['action'] ?? 'status'));

        if ($root === '' || $username === '' || $email === '') {
            emit(['status' => 'error', 'reason' => 'root, username and email are required'], 1);
        }

        $appRoot = null;
        $rootCandidates = [$root];
        if (is_dir($root.'/docroot')) {
            $rootCandidates[] = $root.'/docroot';
        }
        if (is_dir($root.'/public')) {
            $rootCandidates[] = $root.'/public';
        }

        foreach ($rootCandidates as $candidate) {
            if (is_file($candidate.'/app/AppKernel.php')) {
                $appRoot = $candidate;
                break;
            }
        }

        if ($appRoot === null) {
            emit(['status' => 'error', 'reason' => 'AppKernel.php not found'], 1);
        }

        $autoloadCandidates = [
            $appRoot.'/autoload.php',
            $appRoot.'/app/autoload.php',
            $appRoot.'/vendor/autoload.php',
            dirname($appRoot).'/vendor/autoload.php',
        ];
        foreach ($autoloadCandidates as $autoloadPath) {
            if (is_file($autoloadPath)) {
                require_once $autoloadPath;
            }
        }
        require_once $appRoot.'/app/AppKernel.php';

        if (!class_exists('AppKernel')) {
            emit(['status' => 'error', 'reason' => 'AppKernel class is unavailable'], 1);
        }

        defined('IN_MAUTIC_CONSOLE') || define('IN_MAUTIC_CONSOLE', 1);
        chdir($appRoot);

        try {
            $kernel = new AppKernel('prod', false);
            $kernel->boot();
            $container = $kernel->getContainer();
            $entityManager = $container->get('doctrine.orm.entity_manager');
            $userRepo = $entityManager->getRepository('Mautic\\\\UserBundle\\\\Entity\\\\User');

            $user = $userRepo->findOneBy(['username' => $username]);
            if (!$user && $email !== '') {
                $user = $userRepo->findOneBy(['email' => $email]);
            }
            if (!$user) {
                emit([
                    'status' => 'ok',
                    'applicable' => false,
                    'plugin' => 'HostnetAuth',
                    'plugin_installed' => true,
                    'plugin_published' => false,
                    'user_found' => false,
                    'active_mfa' => false,
                    'trusted_browser_count' => 0,
                    'message' => 'matching Mautic user not found',
                ]);
            }

            $integrationHelper = $container->get('mautic.helper.integration');
            $integration = $integrationHelper->getIntegrationObject('HostnetAuth');
            if (!$integration) {
                emit([
                    'status' => 'ok',
                    'applicable' => false,
                    'plugin' => 'HostnetAuth',
                    'plugin_installed' => false,
                    'plugin_published' => false,
                    'user_found' => true,
                    'active_mfa' => false,
                    'trusted_browser_count' => 0,
                    'message' => 'HostnetAuth plugin is not available on this instance',
                ]);
            }

            $userId = (int) $user->getId();
            $reflectionChain = new \\ReflectionClass($integration);
            while ($reflectionChain) {
                foreach ([
                    'user' => $user,
                    'status_field' => 'scanned_'.$userId,
                    'secret_field' => 'secret_'.$userId,
                    'cookie_field' => 'cookie_'.$userId,
                ] as $propertyName => $propertyValue) {
                    if ($reflectionChain->hasProperty($propertyName)) {
                        $prop = $reflectionChain->getProperty($propertyName);
                        $prop->setAccessible(true);
                        $prop->setValue($integration, $propertyValue);
                    }
                }
                $reflectionChain = $reflectionChain->getParentClass();
            }

            $settings = $integration->getIntegrationSettings();
            $published = $settings && method_exists($settings, 'getIsPublished')
                ? (bool) $settings->getIsPublished()
                : false;

            $refl = new \\ReflectionClass($integration);
            $keys = [];
            while ($refl) {
                if ($refl->hasProperty('keys')) {
                    $prop = $refl->getProperty('keys');
                    $prop->setAccessible(true);
                    $raw = $prop->getValue($integration);
                    if (is_array($raw)) {
                        $keys = $raw;
                    }
                    break;
                }
                $refl = $refl->getParentClass();
            }

            $statusKey = 'scanned_'.$userId;
            $trustedDisabledKey = 'trusted_browser_disabled_'.$userId;
            $activeMfa = !empty($keys[$statusKey]);
            $trustedBrowserAllowed = empty($keys[$trustedDisabledKey]);
            $trustedBrowserCount = 0;

            if (class_exists('MauticPlugin\\\\HostnetAuthBundle\\\\Entity\\\\AuthBrowser')) {
                $browserClass = 'MauticPlugin\\\\HostnetAuthBundle\\\\Entity\\\\AuthBrowser';
                $authBrowserRepository = $entityManager->getRepository($browserClass);
                if ($authBrowserRepository && method_exists($authBrowserRepository, 'countByUserIds')) {
                    $counts = $authBrowserRepository->countByUserIds([$userId]);
                    $trustedBrowserCount = (int) ($counts[$userId] ?? 0);
                }
            }

            if ($action === 'clear') {
                $deletedBrowsers = 0;
                if (class_exists('MauticPlugin\\\\HostnetAuthBundle\\\\Entity\\\\AuthBrowser')) {
                    $browserClass = 'MauticPlugin\\\\HostnetAuthBundle\\\\Entity\\\\AuthBrowser';
                    $authBrowserRepository = $entityManager->getRepository($browserClass);
                    if ($authBrowserRepository && method_exists($authBrowserRepository, 'deleteByUserId')) {
                        $deletedBrowsers = (int) $authBrowserRepository->deleteByUserId($userId);
                    }
                }

                $changed = false;
                if (!empty($keys[$statusKey])) {
                    $keys[$statusKey] = 0;
                    $changed = true;
                    $activeMfa = false;
                }

                if ($changed) {
                    $integration->encryptAndSetApiKeys($keys, $settings);
                    $integration->persistIntegrationSettings();
                }

                emit([
                    'status' => 'ok',
                    'applicable' => false,
                    'plugin' => 'HostnetAuth',
                    'plugin_installed' => true,
                    'plugin_published' => $published,
                    'user_found' => true,
                    'active_mfa' => false,
                    'trusted_browser_count' => 0,
                    'trusted_browser_allowed' => $trustedBrowserAllowed,
                    'deleted_trusted_browsers' => $deletedBrowsers,
                    'message' => $changed || $deletedBrowsers > 0
                        ? 'MFA and remembered browsers cleared for this user'
                        : 'Nothing to clear for this user',
                    'user' => [
                        'id' => $userId,
                        'username' => method_exists($user, 'getUsername') ? (string) $user->getUsername() : '',
                        'email' => method_exists($user, 'getEmail') ? (string) $user->getEmail() : '',
                    ],
                ]);
            }

            emit([
                'status' => 'ok',
                'applicable' => $published && $activeMfa,
                'plugin' => 'HostnetAuth',
                'plugin_installed' => true,
                'plugin_published' => $published,
                'user_found' => true,
                'active_mfa' => $activeMfa,
                'trusted_browser_count' => $trustedBrowserCount,
                'trusted_browser_allowed' => $trustedBrowserAllowed,
                'message' => $published && $activeMfa
                    ? 'HostnetAuth MFA is active for this user'
                    : ($published ? 'HostnetAuth MFA is not active for this user' : 'HostnetAuth is not published'),
                'user' => [
                    'id' => $userId,
                    'username' => method_exists($user, 'getUsername') ? (string) $user->getUsername() : '',
                    'email' => method_exists($user, 'getEmail') ? (string) $user->getEmail() : '',
                ],
            ]);
        } catch (\\Throwable $e) {
            emit([
                'status' => 'error',
                'reason' => $e->getMessage(),
                'type' => get_class($e),
            ], 1);
        }
        """
    )


def _run_hostnet_mfa_helper(
    cfg: AgentConfig,
    *,
    root: str | None,
    username: str,
    email: str,
    action: str,
) -> dict[str, Any]:
    inst = _select_instance(cfg, root)
    bundle_dir = _resolve_hostnet_bundle_dir(inst.root)
    if bundle_dir is None:
        return {
            "status": "ok",
            "applicable": False,
            "plugin": "HostnetAuth",
            "plugin_installed": False,
            "plugin_published": False,
            "user_found": False,
            "active_mfa": False,
            "trusted_browser_count": 0,
            "message": "HostnetAuth plugin files are not present on this instance",
            "instance": inst.instance_uid,
            "root": inst.root,
        }

    script_path = ""
    try:
        with tempfile.NamedTemporaryFile("w", suffix="-mcd-hostnet-mfa.php", delete=False, encoding="utf-8") as fh:
            fh.write(_hostnet_php_runner_script())
            script_path = fh.name
        os.chmod(script_path, 0o644)
        cmd = [cfg.php_bin, script_path, f"--root={inst.root}", f"--username={username}", f"--email={email}", f"--action={action}"]
        run_as = str(cfg.mautic_run_as_user or "").strip()
        if run_as:
            cmd = ["sudo", "-u", run_as] + cmd
        proc = subprocess.run(
            cmd,
            cwd=inst.root,
            capture_output=True,
            text=True,
            timeout=max(120, int(getattr(cfg, "command_timeout_sec", 1800) or 1800)),
        )
    finally:
        if script_path:
            try:
                os.unlink(script_path)
            except Exception:
                pass

    stdout = str(proc.stdout or "").strip()
    stderr = str(proc.stderr or "").strip()
    raw = stdout or stderr or f"hostnet mfa helper failed rc={proc.returncode}"
    if proc.returncode != 0:
        raise RuntimeError(raw)
    try:
        payload = json.loads(stdout)
    except Exception as e:
        raise RuntimeError(f"invalid hostnet mfa helper response: {e}: {raw}") from e
    if not isinstance(payload, dict):
        raise RuntimeError("invalid hostnet mfa helper response: expected JSON object")
    payload.setdefault("instance", inst.instance_uid)
    payload.setdefault("root", inst.root)
    payload.setdefault("plugin", "HostnetAuth")
    return payload


def hostnet_auth_mfa_status(
    cfg: AgentConfig,
    *,
    root: str | None,
    username: str,
    email: str,
) -> dict[str, Any]:
    username_clean = str(username or "").strip()
    email_clean = str(email or "").strip()
    if not username_clean:
        raise RuntimeError("username is required")
    if not email_clean:
        raise RuntimeError("email is required")
    return _run_hostnet_mfa_helper(cfg, root=root, username=username_clean, email=email_clean, action="status")


def clear_hostnet_auth_mfa(
    cfg: AgentConfig,
    *,
    root: str | None,
    username: str,
    email: str,
) -> dict[str, Any]:
    username_clean = str(username or "").strip()
    email_clean = str(email or "").strip()
    if not username_clean:
        raise RuntimeError("username is required")
    if not email_clean:
        raise RuntimeError("email is required")
    return _run_hostnet_mfa_helper(cfg, root=root, username=username_clean, email=email_clean, action="clear")
