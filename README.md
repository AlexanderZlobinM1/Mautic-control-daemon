# MCD Agent

Changelog: `control-plane/agent/CHANGELOG.md`

MCD (MauticControlDaemon) is a host-level service that can run in two modes:
- standalone (without MCC)
- connected (managed by MCC over SSH and event callbacks)

## Current capabilities
- CLI entrypoint
- Config loader (TOML)
- Mautic instance discovery from web roots
- Instance uid is domain-based (from active nginx/apache vhost), fallback to root-based short id
- Mautic versions supported now: 4, 5, 6, 7
- DB settings extraction from Mautic `local.php`:
  - `db_host`
  - `db_table_prefix`
  - `db_port`
  - `db_name`
  - `db_user`
  - `db_password`
- Optional manual instance definitions for non-standard/container paths
- Remote direct host-level backups via `sshfs + mydumper` with state semaphores (`last_success`, `last_status`, `history`)
- Full host restore (`files archive + myloader DB restore`) from selected backup date/path
- Encrypted backup profile vault in local MCD SQLite (credentials stored encrypted, not plain)
- Lightweight critical host signal snapshot (`mcd-cli signals`) for MCC cache:
  - OOM kills (kernel)
  - MySQL/MariaDB critical patterns
  - php-fpm `max_children` pressure
  - web `5xx` spikes from service journal
  - scheduler drift (`tracked running` vs real processes)
  - stuck PHP console workers
  - swap pressure level
- DB-driven task polling and command execution:
  - segment updates by `id` (`mautic:segments:update -i <id>`)
  - campaigns update/trigger by `id`
  - import execution on pending import queue
  - optional periodic contacts cleanup (`{prefix}leads` rows with empty email+phone fields)
- Segment whitelist policy during active campaigns
- Runtime concurrency controls for campaigns and segments
- Round-robin segment scheduling so all eligible segments are processed over time
- Priority/regular circles for both segments and campaigns with dynamic weights
- Queue-based throttling using DB queue metrics (`message_queue` for Mautic 5+)
- Segment scheduler modes:
  - `id_weighted` (per-id weighted circles)
  - `classic_loop` (full `mautic:segments:update` each daemon cycle)
- Cron replacement workers:
  - use `[[jobs]]` in config for interval-based independent tasks
  - examples: `mautic:email:fetch` every 900 sec, `mautic:broadcasts:send` every 60 sec, `mautic:messages:send` every 60 sec
- Viber stats worker:
  - if an instance has a Viber plugin installed, active profiles run `viber:stats:update` through MCD every 600 sec by default
  - MCC can override the interval per instance through `runtime.viber_stats_instance_settings`
  - matching cron lines are commented while MCD manages tasks and restored when the host profile returns to `passive`
- Scheduler model:
  - single daemon loop
  - DB/config refresh on `poll_interval_sec`
  - dispatcher refill on `dispatch_interval_sec` (keeps target parallelism continuously)
  - two circles for segments and campaigns (`priority` + `regular`) with separate parallel limits
  - `spawn-and-release`: daemon starts command and does not wait for completion
  - process status is tracked asynchronously by PID monitor
  - mini SQLite state DB keeps running/finished/failed/timeout task history with bounded retention
  - weight cache stored in SQLite (`weight_cache`), recalculated by `weights_recalc_interval_sec` and on active-id set change
  - state DB tables:
    - `tasks` (task execution history / running rows)
    - `weight_cache` (segment/campaign computed weights)
    - `instances` (local Mautic inventory + DB connection metadata)
- Mautic instance discovery is not executed every tick
- instance list is loaded from local inventory (SQLite) and can be refreshed on demand
- MCC push model:
  - periodic push to MCC (`/api/v1/agent/state`) every 5 minutes by default
  - apt state is refreshed at `mcc.push_apt_state_interval_sec` (default 120 sec) and also refreshed immediately when local APT/DPKG state changes
  - extra push on state change
  - extra push on alert signal changes
  - mutating CLI operations push immediately (for example `service-profile apply`, `env ipv6 enable|disable`)
  - push includes host `config_state` snapshot (`schema_version`, `customized`, `sha256`, full TOML) so MCC stores exact observed behavior
- MCD self-update model:
  - MCC returns build plan (`test|approved|lts`) via authenticated API.
  - MCD performs update locally (download/stage/atomic source switch/restart) and reports result back to MCC.
  - apply path does not run `pip install`; host update is source-switch only.
  - MCC limits concurrent update sessions (`10` by default); extra nodes receive wait/retry signal.
  - MCD auto-cleans old self-update artifacts (`/opt/mcd/var/updates` archives + `/opt/mcd/var/backup/mcd-src-preupdate-*`) by retention policy.
    - default: keep last `3` archives and `3` preupdate backups, max age `30` days, cleanup once per day.
  - MCD keeps local config history (`10` snapshots by default).
- MCC-driven dynamic service profiles:
  - service profile payload is stored on MCC and can be changed without MCD release rebuild.
  - MCD pulls and auto-applies host-specific profile by hardware plan (`php-fpm`, `mysql`, `apt` components) on the normal daemon loop by default.
  - manual fetch/apply remains available through `mcd-cli service-profile`.
- Transitional shared agent-state backend for all installations:
  - optional `state.backend = "mysql_hybrid"` stores outbound events + latest state snapshot in MySQL/MariaDB,
  - agent uses dedicated state DB (`state.mysql_database`, default `mcd_state`) and auto-creates it if missing,
  - local SQLite remains as minimal fallback queue when shared DB is unavailable,
  - keeps current scheduler/task runtime stable while moving state to DB-backed mode.

## Profiles
Set in config:
- `[profile]`
- `name = "custom|tiny|mini|midi|maxi|hiload"`

Preset rules:
- `tiny`: single ring, no throttle, no whitelists, segments `1`; campaigns use one worker with actual trigger-due campaigns first and rebuild-due campaigns second, newest-first published list.
- `mini`: single ring, no throttle, no whitelists, segments `4`, campaign trigger `2`, campaign rebuild `1`, shared campaign cap `1`.
- `midi`: dual ring, no throttle, whitelists enabled, priority size `10`, parallel `3+1` for segments, updates, triggers.
- `maxi`: dual ring, throttle `200/5m`, whitelists enabled, segments `5+1`, triggers `3+1`, rebuilds `2+1`; during throttle only whitelist segments run in `1` stream.
- `hiload`: dual ring, throttle `200/5m`, whitelists enabled, segments `6+2`, triggers `4+2`, rebuilds `3+1`; during throttle only whitelist segments run in `2` streams and non-whitelist running segments are killed and queued to resume first after throttle ends.
- `custom`: uses explicit `[runtime]` values.

Segment stale-priority rule (all non-passive profiles):
- segments with `last_built_date` older than 24h (or missing) are force-added to priority ring;
- this rule is independent from normal weight threshold/top-N ranking;
- if regular ring is empty, its slot is reused by priority ring automatically until regular items appear.

## Split Config
Recommended layout:
- entrypoint: `/opt/mcd/etc/mcd.toml` (small, package-safe)
- package defaults: `/opt/mcd/src/etc/mcd-agent.system.example.toml`
- package defaults: `/opt/mcd/src/etc/mcd-agent.operator.example.toml`
- host overrides: `/opt/mcd/etc/mcd.local.toml`

Entrypoint file uses:
- `[include].files = ["/opt/mcd/src/etc/mcd-agent.system.example.toml", "/opt/mcd/src/etc/mcd-agent.operator.example.toml", "/opt/mcd/etc/mcd.local.toml"]`

Merge and precedence:
1. include files are merged in listed order
2. values from entrypoint file override includes
3. profile baseline is applied
4. manually set `[runtime]` values override profile baseline

Why this layout:
- package update can safely replace `/opt/mcd/src` defaults;
- host custom behavior stays in `/opt/mcd/etc/mcd.local.toml` and is not overwritten by code update.

## Commands
Production CLI (recommended):
- `mcd-cli` (no args -> interactive menu)
- `mcd-cli health`
- `mcd-cli discover`
- `mcd-cli run`
- `mcd-cli run-once`
- `mcd-cli segments:update -i 5`
- `mcd-cli campaigns:trigger -i 83`
- `mcd-cli import`
- `mcd-cli plugins`
- `mcd-cli mautic-upgrade` (interactive)
- `mcd-cli mautic-upgrade check`
- `mcd-cli mautic-upgrade apply --mode zip --backup --yes`
- `mcd-cli backup profile-show --json`
- `cat backup-profile.json | mcd-cli backup profile-set --profile-json-stdin`
- `mcd-cli backup profile-set --profile-json-file /root/backup-profile.json`

Source/dev equivalent (same command surface):
- `python -m mcd_agent <same args as mcd-cli>`
- `python3 -m pip install -r requirements.txt` (only when running from source tree)
- `python -m mcd_agent interactive --config ./etc/mcd-agent.example.toml`
  - interactive menu uses one active instance for operational actions
  - use `Select Active Instance` to switch target without restarting CLI
  - includes `Cache` menu:
  - `Soft Clear` (`cache:clear`)
  - `Warmup` (`cache:warmup`)
  - `Hard Clear` (delete `var/cache/prod`)
- `mcd-cli` wrapper notes:
  - `plugin` alias is supported for `plugins`
  - help aliases supported: `mcd-cli /?`, `mcd-cli instances /?`, `mcd-cli plugins /?`

Manual command behavior:
- In active profiles, `exec` and shorthand commands are scheduler-aware:
  - request is queued into local state DB and picked by daemon on next dispatch cycle;
  - launch is immediate relative to dispatch tick and can temporarily exceed ring slot formula by one manual task;
  - scheduler then holds new auto launches until total active tasks return to configured profile limits.
- If daemon does not pick queued request quickly, CLI cancels queue row and falls back to direct one-shot execution.
- `python -m mcd_agent instances --config ./etc/mcd-agent.example.toml list`
- `python -m mcd_agent env ipv6 status`
- `python -m mcd_agent env ipv6 disable`
- `python -m mcd_agent env ipv6 enable`
- `python -m mcd_agent env policy show`
- `python -m mcd_agent env policy plan --policy-file ./policy.json --component all`
- `python -m mcd_agent signals --window-min 15 --json`
- `python -m mcd_agent self-update --config ./etc/mcd-agent.example.toml status --json`
- `python -m mcd_agent self-update --config ./etc/mcd-agent.example.toml check --json`
- `python -m mcd_agent self-update --config ./etc/mcd-agent.example.toml apply --yes`
- `python -m mcd_agent service-profile --config ./etc/mcd-agent.example.toml status --json`
- `python -m mcd_agent service-profile --config ./etc/mcd-agent.example.toml fetch --component php_fpm --json`
- `python -m mcd_agent service-profile --config ./etc/mcd-agent.example.toml apply --component php_fpm`
- `python -m mcd_agent service-profile --config ./etc/mcd-agent.example.toml fetch --component mysql --json`
- `python -m mcd_agent service-profile --config ./etc/mcd-agent.example.toml apply --component mysql`
- `python -m mcd_agent service-profile --config ./etc/mcd-agent.example.toml fetch --component apt --json`
- `python -m mcd_agent service-profile --config ./etc/mcd-agent.example.toml apply --component apt --dry-run`
- `python -m mcd_agent service-profile --config ./etc/mcd-agent.example.toml rescan --component apt`
- `python -m mcd_agent zabbix --config ./etc/mcd-agent.example.toml status --json`
- `python -m mcd_agent zabbix --config ./etc/mcd-agent.example.toml bootstrap-mysql-user`
- `python -m mcd_agent zabbix --config ./etc/mcd-agent.example.toml refresh-mautic-version-cache`
- `python -m mcd_agent zabbix --config ./etc/mcd-agent.example.toml install-mautic-version-cache`

Notes:
- `php_fpm` apply includes FPM pool/opcache/redis tuning. Global managed `98-mcd-php.ini` baseline is no longer used; legacy files are removed on apply if present.
- APT profile includes one-time Zabbix DB monitor bootstrap (`zbx_monitor@127.0.0.1`) with marker tracking and manual override via `mcd-cli zabbix bootstrap-mysql-user --force`.
- MCD writes each discovered Mautic version to `<instance-root>/.mcd/mautic.version`. The Zabbix `mautic.version[*]` helper installed by `install-mautic-version-cache` reads only that cache file and never runs `bin/console`.
- APT profile includes modular one-time repo profiles with local markers (`/opt/mcd/var/apt-repo-profiles.json`):
  - `db_repo_profile` (auto-detect: MariaDB/Percona/MySQL families),
  - `ondrej_php_profile`,
  - `nginx_official_stable_profile` (official stable `nginx.org` repo, disables Ondrej nginx source),
  - `ondrej_nginx_profile` (legacy; disabled when official nginx profile is enabled).
  Automatic checks stop after successful apply/verify for the same MCC profile hash and re-run when MCC changes the profile; use `service-profile rescan --component apt` for manual recheck/fix.
- APT profile can also manage unattended-upgrades policy dynamically:
  - `unattended_upgrade_mode=off|security|all`,
  - `unattended_upgrade_schedule_cron` (host local cron),
  - `unattended_upgrade_blacklist` (excluded package patterns).
- Additional runtime protection keys for scheduler/pressure handling:
  - `scheduler_reconcile_interval_sec`
  - `php_console_stuck_sec`
  - `host_pressure_pause_enabled`
  - `host_pressure_php_stuck_pause_threshold`
  - `host_pressure_swap_level_pause_threshold`
- `mcd-cli signals` now also reports:
  - `scheduler_state_drift`
  - `scheduler_duplicate_task_keys`
  - `php_console_stuck`
  - `swap_pressure_level`
- `python -m mcd_agent runtime-overrides --config ./etc/mcd-agent.example.toml show`
- `python -m mcd_agent runtime-overrides --config ./etc/mcd-agent.example.toml fetch --json`
- `python -m mcd_agent runtime-overrides --config ./etc/mcd-agent.example.toml push --json`
- `python -m mcd_agent runtime-overrides --config ./etc/mcd-agent.example.toml trigger`
- `python -m mcd_agent state-db --config ./etc/mcd-agent.example.toml status --json`
- `printf 'ROOT_DB_PASSWORD' | python -m mcd_agent state-db --config ./etc/mcd-agent.example.toml init --admin-user root --admin-password-stdin --admin-unix-socket /var/run/mysqld/mysqld.sock --json`
- `python -m mcd_agent maintenance --config ./etc/mcd-agent.example.toml status`
- `python -m mcd_agent maintenance --config ./etc/mcd-agent.example.toml on --kill-orphans --grace-sec 10`
- `python -m mcd_agent maintenance --config ./etc/mcd-agent.example.toml off`
- `python -m mcd_agent instances --config ./etc/mcd-agent.example.toml rescan`
- `python -m mcd_agent instances --config ./etc/mcd-agent.example.toml add --name m1 --root /var/www/m1 --console-path /var/www/m1/bin/console`
- `python -m mcd_agent instances --config ./etc/mcd-agent.example.toml remove --name m1`
- `python -m mcd_agent reload-config --config ./etc/mcd-agent.example.toml`
- `python -m mcd_agent time-check --config ./etc/mcd-agent.example.toml`
- `python -m mcd_agent profile --config ./etc/mcd-agent.example.toml status`
- `python -m mcd_agent profile --config ./etc/mcd-agent.example.toml tiny --yes`
- `python -m mcd_agent profile --config ./etc/mcd-agent.example.toml passive --yes`
- `python -m mcd_agent uninstall --yes`
- `python -m mcd_agent backup --config ./etc/mcd-agent.example.toml run`
- `python -m mcd_agent backup --config ./etc/mcd-agent.example.toml status --json`
- `python -m mcd_agent backup --config ./etc/mcd-agent.example.toml history --json`
- `python -m mcd_agent backup --config ./etc/mcd-agent.example.toml prune`
- `python -m mcd_agent backup --config ./etc/mcd-agent.example.toml restore --date 2026-03-01`
- `python -m mcd_agent backup --config ./etc/mcd-agent.example.toml profile-show`
- `cat backup-profile.json | python -m mcd_agent backup --config ./etc/mcd-agent.example.toml profile-set --profile-json-stdin`
- `python -m mcd_agent backup --config ./etc/mcd-agent.example.toml profile-set --profile-json-file ./backup-profile.json`

Same operations via wrapper (`mcd-cli`):
- `mcd-cli runtime-overrides show`
- `mcd-cli runtime-overrides fetch --json`
- `mcd-cli runtime-overrides push --json`
- `mcd-cli runtime-overrides trigger`
- `mcd-cli state-db status --json`
- `printf 'ROOT_DB_PASSWORD' | mcd-cli state-db init --admin-user root --admin-password-stdin --admin-unix-socket /var/run/mysqld/mysqld.sock --json`
- `mcd-cli maintenance status`
- `mcd-cli maintenance on --kill-orphans --grace-sec 10`
- `mcd-cli maintenance off`
- `mcd-cli instances rescan`
- `mcd-cli instances add --name m1 --root /var/www/m1 --console-path /var/www/m1/bin/console`
- `mcd-cli instances remove --name m1`
- `mcd-cli reload-config`
- `mcd-cli time-check`
- `mcd-cli profile status`
- `mcd-cli profile tiny --yes`
- `mcd-cli profile passive --yes`
- `mcd-cli uninstall --yes`
- `mcd-cli backup run`
- `mcd-cli backup status --json`
- `mcd-cli backup history --json`
- `mcd-cli backup prune`
- `mcd-cli backup restore --date 2026-03-01`
- `mcd-cli backup profile-show`
- `cat backup-profile.json | mcd-cli backup profile-set --profile-json-stdin`
- `mcd-cli backup profile-set --profile-json-file /root/backup-profile.json`

## Profile Model
- State is profile-based (`[profile].name`).
- `profile=passive`:
  - MCD runs in planning/statistics mode only (no Mautic task dispatch),
  - cron is expected to remain active.
- Non-passive profiles (`tiny|mini|midi|maxi|hiload|custom`) dispatch Mautic tasks.
- `mcd-cli profile passive`:
  - switches profile to `passive`,
  - restores cron from pre-active backups (or from MCD markers if backup missing),
  - restarts `mcd`.
- `mcd-cli profile <tiny|mini|midi|maxi|hiload|custom>`:
  - applies selected non-passive profile,
  - comments managed cron lines (`segments:update`, `campaigns:update`, `campaigns:trigger`, `campaigns:rebuild`, `import`),
  - restarts `mcd`.
- `mcd-cli maintenance on|off|status`:
  - temporary maintenance mode without profile change,
  - `on` pauses scheduler launches and can stop running Mautic console tasks,
  - `off` resumes scheduler launches only.

## Cron replacement focus (phase now)
Replaced by daemon logic:
- `mautic:segments:update` (DB-selected segments only, with `-i id`)
- `mautic:campaigns:rebuild` and `mautic:campaigns:trigger` (only active/published campaigns from DB)
- `mautic:import` (runs when pending jobs appear)

Campaign command note:
- In MCD scheduler, `mautic:campaigns:update` is treated as a synonym of `mautic:campaigns:rebuild`.
- Only one pre-trigger campaign pass is scheduled (`campaigns:rebuild`) to avoid duplicate work.

Still left to cron for now:
- cache clear/warm
- maintenance cleanup
- SQL cleanup and other housekeeping tasks

Important:
- SQL selectors are configurable in `[sql]` for different Mautic schemas.
- Time in SQL should use daemon-provided UTC placeholders:
  - `{now_utc}` for point-in-time checks
  - `{window_start_utc_24h}` for 24h windows
  - this avoids dependency on MySQL/PHP server timezone settings.
- Console command templates are configurable in `[commands]` for different Mautic CLI variants.
- Config path detection:
  - Mautic 4: `app/config/local.php`
  - Mautic 5/6/7: `config/local.php`
- Mautic timezone:
  - parsed from `local.php` (`default_timezone`/`timezone`) and stored in instance inventory
  - used for quiet-window jobs (contacts cleanup) so daemon behavior follows instance timezone.
- Runtime execution user:
  - `runtime.mautic_run_as_user` (default `www-data`) is used for Mautic console commands.
- Filesystem permissions watchdog:
  - `runtime.fs_permissions_guard_enabled` enables periodic owner/mode guard for critical Mautic paths.
  - `runtime.fs_permissions_guard_interval_sec` controls per-instance check interval.
  - `runtime.fs_permissions_guard_paths` defines relative instance paths to enforce (`var/cache`, `var/logs`, `var/spool`, `var/tmp`, media/config paths).
  - `runtime.fs_permissions_guard_fix_console_exec` forces `bin/console` executable bit (`chmod ug+x`) and runtime owner.
  - `runtime.fs_permissions_guard_console_relpath` allows custom console location (default `bin/console`).
  - `runtime.db_watchdog` adds DB processlist watchdog policy (observe-first):
    - `enabled`, `interval_sec`, `observe_only`, `processlist_limit`, `sample_limit`
    - `global_rules` for shared defaults
    - `host_rules` for host-specific overrides (host patch overrides global rules by rule `id`)
    - telemetry is pushed to MCC in `signals.totals` and `signals.details.db_watchdog_recent` (no kill action while `observe_only=true`)
  - guard runs in all profiles, including `passive` (planning-only mode still keeps filesystem ownership healthy).
- Runtime tuning for large campaigns:
  - `runtime.campaign_limit` controls per-run trigger batch size.
  - `runtime.campaign_limit = 0` (or `off` / `unlimited` via MCC runtime override) omits `--campaign-limit`, so one trigger run can process the whole campaign.
  - `runtime.campaign_trigger_audit_interval_sec` bounds the safety audit that
    periodically enqueues published campaigns as explicit
    `mautic:campaigns:trigger -i ID` runs. Set to `0` to disable.
  - on weak hosts start lower (e.g. `1000`) so one long campaign does not block full daemon cycle for too long.
- Runtime tuning for Viber stats:
  - `runtime.viber_stats_enabled = true` enables the built-in `viber:stats:update` scheduler for instances where a Viber plugin is installed.
  - `runtime.viber_stats_interval_sec = 600` is the default interval.
  - `runtime.viber_stats_instance_settings` can override specific instances by `instance_uid`, root, name, or domain.
  - when the active profile is not `passive`, MCD comments matching `viber:stats:update` cron lines to avoid duplicate execution.
- Self-update safety:
  - `runtime.mcd_update_defer_during_campaigns = true` prevents MCD self-update while campaign trigger/rebuild/update console jobs are running.
  - daemon auto-update keeps a short cooldown after campaign console activity to avoid restarting between batch passes.
- Retry and watchdog:
  - `runtime.task_retry_max`, `runtime.task_retry_delay_sec` control retries for concrete command execution.
  - `runtime.task_retry_max` semantics:
    - `1` = no retry (only initial attempt),
    - `>1` = bounded retries (attempt cap),
    - `0` or negative = unlimited immediate retries (with `task_retry_delay_sec` pause).
  - global default: `runtime.command_timeout_sec = 0` and `runtime.worker_watchdog_sec = 0` (long-running tasks are not killed by timeout).
  - `runtime.worker_stuck_policy = skip|restart` and `runtime.worker_stuck_restart_limit` control reaction on stuck processes.
  - `runtime.state_db_path` sets SQLite process-state storage path (default `/opt/mcd/var/mcd-state.db`).
  - optional `[state]` section enables shared state backend:
    - `backend = "sqlite|mysql_hybrid"`
    - `mysql_host/mysql_port/mysql_database/mysql_user/mysql_password`
    - `mysql_unix_socket` (optional explicit socket path for local auth)
    - `mysql_table_prefix`, `mysql_*_timeout_sec`
    - `mysql_snapshot_enabled`
  - runtime hot-apply keys from MCC include `state_backend` and `state_mysql_*` (including `state_mysql_unix_socket`).
  - when host is local and password is empty, agent auto-detects common MySQL unix sockets for local auth.
  - in `mysql_hybrid` mode, agent attempts to create state DB automatically; on failure it keeps legacy SQLite behavior and reports init error to MCC (`state_backend` payload).
  - in `mysql_hybrid` mode, task/state runtime tables (`tasks`, `manual_requests`, `weight_cache`, `runtime_sync`) are primary in MySQL.
  - local SQLite stays as failover-only shadow (running/pending minimum) and is pruned after successful migration.
  - first successful MySQL bootstrap performs one-time SQLite -> MySQL migration for these runtime tables.
  - manual DB bootstrap is available via `mcd-cli state-db init` and is allowed for legacy mode when DB is missing or inaccessible.
  - bootstrap uses temporary admin credentials only for init, creates dedicated `mcd_state` runtime DB user, and persists only runtime credentials.
  - `runtime.tasks_history_keep_days` sets retention depth for non-running task rows
    in the live operational slice (default: 2 days).
  - `runtime.tasks_history_max_rows` sets hard cap for historical non-running rows
    in the live operational slice (default: 25000 rows).
  - `runtime.tasks_archive_enabled`, `runtime.tasks_archive_dir`, and
    `runtime.tasks_archive_keep_days` control compressed JSONL postmortem
    archive of task rows removed from live state (default: 14 days). Scheduling
    logic must not depend on archived rows.
  - runtime-sync snapshots are kept in backend runtime table `runtime_sync`:
  - `local_runtime` (runtime section from local config)
  - `mcc_runtime` (desired runtime payload fetched from MCC)
  - `active_runtime` (last runtime apply metadata)

## Runtime Sync (MCC <-> MCD)
- Source of truth split:
  - `desired` runtime overrides are stored in MCC host table (`runtime_overrides_json`).
  - `observed` runtime overrides are pushed by MCD and stored separately in MCC (`observed_runtime_overrides_json`).
- Template runtime keys:
  - `runtime.host_template=true` marks host as template source.
  - `runtime.template_autopromote_on_clone=true` enables clone autopromote to new host identity in MCC when local hostname differs from configured `[mcc].host_name`.
- MCC -> MCD:
  - normal path: daemon polls MCC runtime endpoint.
  - immediate path: `mcc_cli host-runtime set/unset` triggers `mcd-cli runtime-overrides trigger`, daemon consumes trigger and pulls immediately.
- MCD -> MCC:
  - MCD state push payload contains `runtime_overrides` from local config runtime section.
  - daemon also watches local mutable runtime section fingerprint and pushes to MCC immediately when it changes.
  - any mutating command that already does immediate state push updates observed runtime view in MCC without separate polling.
  - `runtime.tasks_compact_*` controls quiet-window compaction cadence (`DELETE` + optional `VACUUM`).
  - systemd service uses `KillMode=process` and `TimeoutStopSec=15` so restart does not kill child Mautic commands.
- Plugin interactive sync:
- MCD reads `manifest.json` from MCC plugin repo
  - shows status table (`OK`, `UPDATE`, `MISSING`, `BROKEN`) plus local-only rows (`-`)
  - table columns: installed version (from `plugins/<Bundle>/Config/config.php`) and server version (from manifest)
  - applies selected plugin operations, then runs `cache:clear` and `mautic:plugin:install`
  - after install/replace sets ownership to `www-data:www-data` for the updated bundle directory

- Backup module:
  - section `[backup]` in system config
  - host-level direct write to remote share via `sshfs` (no local dump staging)
  - one run includes all discovered instance databases (with DB creds) + optional system files archive
  - remote layout: `/<remote_root_dir>/<host_name>/<YYYY-MM-DD>/...`
  - startup hygiene: stale `/.incomplete-*` directories from failed/aborted runs are cleaned automatically before a new backup starts
  - on success writes `.mcd-backup.json` marker in backup folder
  - local state semaphore per host in `/opt/mcd/var/state/backup/host-<host>.json`
  - state includes `last_run_at`, `last_success_at`, `last_status`, `last_error`, `last_backup_path`, and recent `history`
  - restore command supports:
    - latest backup auto-select (default)
    - restore by explicit `--date YYYY-MM-DD`
    - restore by explicit backup `--path`
  - backup profile credentials can be set without shell-history exposure:
    - `backup profile-set --profile-json-stdin`
    - `backup profile-set --profile-json-file`
  - where credentials/settings are stored:
    - authoritative runtime backup profile is in local MCD state DB (`state_db_path`) table `backup_profile` as encrypted payload (`payload_enc`);
    - this is why scheduler/backup can run even if some `backup.*` keys are absent in text config;
    - explicit stable backup sections are synchronized with mutable config (`/opt/mcd/etc/mcd.toml`):
      - DB -> config on `backup profile-set` / MCC-applied backup profile changes;
      - config -> DB by daemon periodic sync (for manual operator edits in config file).
  - secret refs/config keys:
    - `[backup.secrets].key_path`
    - `[backup.storage].password_ref`
    - `[backup.mysql].password_ref`
  - scheduler support in daemon (`[backup.schedule]`):
    - quiet-window execution
    - interval-based cadence
    - runs independently from Mautic task rings
    - global backup guard:
      - while backup lock is active, no new Mautic tasks are started (`segments`, `campaigns`, `import`, scheduled jobs)
      - pre-backup window (`backup.schedule.pre_pause_sec`, default 3600s) also blocks new task launches
      - already running tasks continue until completion; they are not killed by backup guard
      - dispatch resumes automatically when backup run finishes (success or failure)
  - default dump safety profile (`[backup.mydumper]`):
    - threads: `6`
    - `kill_long_queries=false`
    - `long_query_guard=0`
    - process priority lowering enabled by default (`ionice` + `nice`)
    - transaction/lock flags are auto-selected:
      - `--sync-thread-lock-mode=AUTO` when supported by local mydumper
      - prefer `--trx-tables`, fallback to `--trx-consistency-only` on older versions

- Mautic upgrade:
  - `mautic-upgrade check` detects current version and suggests next target in chain
  - `mautic-upgrade apply` supports `zip|composer|auto`
  - optional `--backup` creates archive backup before upgrade
  - optional `--with-system-upgrade` runs php/nginx package-level migration steps
- MCD self-version checks:
  - `runtime.mcd_update_notify = true` (default): show notice if MCC has newer MCD version
  - `runtime.mcd_auto_update_enabled = false` (default): auto-update disabled; notify-only
  - `runtime.mcd_update_check_interval_sec`: check interval
  - `mcc.mcd_manifest_url` (optional): explicit MCC manifest URL

## Central Policy (Plan-Only in 0.4.0)
- MCD exposes host policy planning commands for centralized operations managed by MCC.
- Covered domains:
  - `apt`
  - `iptables`
  - `database` (MariaDB/MySQL)
  - `php` (php-fpm)
  - `web` (nginx/apache)
  - `web.cloudflare_real_ip` (nginx Cloudflare real-IP template task)
- Commands:
  - `mcd-cli env policy show`
  - `mcd-cli env policy plan --policy-file <file>`
  - `mcd-cli env policy plan --policy-json '<json>' --component php`
  - `mcd-cli env policy plan --policy-json '<json>' --component web_cf_real_ip`
- Safety:
  - this release does not apply policy changes on hosts;
  - output is execution plan only.

## Validated runtime profile (current host, 2026-02-17)
- `runtime.segment_mode = "classic_loop"` (single full `mautic:segments:update` loop through MCD)
- Campaign workers enabled:
  - `campaign_priority_parallel = 1`
  - `campaign_regular_parallel = 1`
  - `campaign_latest_priority_count = 2` (latest published campaigns are always in priority circle)
- Import worker enabled only on real pending imports:
  - `enable_import_polling = true`
  - `import_poll_interval_sec = 30`
- Cron-like minute tasks via independent `[[jobs]]` workers:
  - `mautic:broadcasts:send` (`interval_sec = 60`)
  - `mautic:messages:send` (`interval_sec = 60`)
  - `mautic:email:fetch` (`interval_sec = 900`)
- Daily cleanup via built-in contacts cleanup window:
  - `enable_contacts_cleanup = true`
  - `contacts_cleanup_interval_sec = 86400`
  - `contacts_cleanup_quiet_hour = 2`
  - `contacts_cleanup_quiet_window_min = 60`
- MCC-managed Clean Empty Contacts can run per instance inside a nightly window:
  - `empty_leads_cleanup_instance_settings[<instance>].schedule_type = "nightly_window"`
  - `window_start = "22:00"`, `window_end = "09:00"`
  - `batch_size = 50000`
  - `max_runs_per_window = 0` for no limit while the window is open
- MCC-managed Monitored Email Parser can replace `mautic:email:fetch` per instance:
  - `monitored_email_parser_interval_sec = 900`
  - `monitored_email_parser_batch_size = 100` (agent caps at 5000)
  - `monitored_email_parser_types = ["feedback_loop", "bounce", "unsubscribe"]`
  - `monitored_email_parser_delete_processed = true` deletes mailbox messages only
    after a matching contact is found and email DNC is present or inserted
  - `monitored_email_parser_whitelist = ["support@example.com"]` skips exact
    internal emails and removes existing email-DNC rows for those contacts

## Benchmark notes (current host)
- Baseline standard command (scheduler paused):  
  `sudo -u www-data php /var/www/mautic/bin/console mautic:segments:update --batch-limit=1000`
  - measured on `2026-02-17`: `elapsed=18.48s`
- Previous measurement before latest fixes on same host: `elapsed=22.84s`
- MCD scheduler now launches independent task types in parallel (confirmed in one cycle): `segment`, `campaign_update`, `campaign_trigger`, `job:broadcasts-send`, `job:messages-send`.
