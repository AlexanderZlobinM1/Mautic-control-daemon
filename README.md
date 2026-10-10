# MCD Agent

Changelog: `control-plane/agent/CHANGELOG.md`

MCD (MauticControlDaemon) is a host-level service that can run in two modes:
- standalone (without MCC)
- connected (managed by MCC over SSH and event callbacks)

## Current capabilities

- Mautic 7 dependency planning preserves every published due customer-visible
  segment as explicit work. A terminal lead-list dependency may serialize the
  chain but never counts the referenced segment as rebuilt or removes it from
  the scheduler plan.
- `mcd-cli report:contact-field-metadata --root <instance> --json` publishes
  `mcd-contact-field-metadata-v1`: instance-local Mautic contact field aliases,
  labels, native/custom classification, types and safe physical column sizes.
  It never reads contact rows, field values, defaults, properties or options;
  see `docs/contact-field-metadata-v1.md` for the JSON and error contract.
- CLI entrypoint
- Config loader (TOML)
- Mautic instance discovery from web roots
- Root-owned Docker runtime descriptor discovery from `/etc/mcd/instances.d`;
  descriptor-backed console operations are routed through an exact scoped
  `docker exec`, host-side SQL uses the descriptor's private host endpoint, and
  filesystem repair preserves the numeric container owner; host-native
  instances retain the original path. Descriptor removal is reconciled on the
  next inventory rescan.
- Two independent instance dimensions are recorded and evaluated for every
  operation: runtime (`host` or `docker`) and installation layout (`zip` or
  `composer`). The four matrix cells have separate MCC presentation and share
  one capability-derived allowlist; Docker never implies Composer and
  Composer never implies Docker.
- Docker descriptors may expose a host bind-mounted plugin path. MCD then
  inventories, installs, removes and fixes ownership for plugins through that
  exact path and still runs post-install console work through the container.
  Missing descriptor capabilities fail closed instead of falling back to
  `<host_root>/plugins` or `www-data`.
- Cross-host Docker migration is delegated to a root-owned executable under
  `/usr/local/libexec/mcd-runtime-adapters`. A target is eligible only when
  Docker is running, the exact named adapter is installed and the source image
  reference is available locally. Adapter/image validation precedes every
  destructive target cleanup.
- Instance uid is domain-based (from active nginx/apache vhost), fallback to root-based short id
- Mautic versions supported now: 4, 5, 6, 7
- Image provisioning uses the standard HTTPS path: after the HTTP vhost is
  created, Certbot must obtain the domain certificate, install the HTTPS
  vhost and pass the nginx validation/reload checks before deployment is
  considered successful. Do not use `--no-certbot` for normal or test
  instance creation; it is reserved only for an explicitly documented
  recovery case.
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
- Runtime concurrency controls for campaigns and segments; limits are worker
  ceilings, while automatic scheduler dispatch claims at most one new queued
  task per pass to avoid burst-starting all free workers at once
- Round-robin segment scheduling so all eligible segments are processed over time
- Priority/regular circles for both segments and campaigns with dynamic weights
- Queue-based throttling using DB queue metrics (`message_queue` for Mautic 5+)
- Segment scheduler modes:
  - `id_weighted` (per-id weighted circles)
  - `classic_loop` (full `mautic:segments:update` each daemon cycle)
- Cron replacement workers:
  - use `[[jobs]]` in config for interval-based independent tasks
  - examples: `mautic:email:fetch` every 900 sec and `mautic:broadcasts:send` every 60 sec
- Mautic 5/6/7 message-queue worker:
  - MCC stores a per-instance `enabled` flag and interval for
    `mautic:messages:send`; default is disabled and 3600 seconds;
  - existing direct or wrapper cron is commented and migrated on the first
    supporting agent run, while an existing `[[jobs]]` entry is imported when
    no canonical MCC setting exists;
  - after migration, generic jobs cannot bypass the per-instance checkbox;
  - Mautic 4 is not changed by this worker.
- Catalog-driven plugin operations:
  - MCC sends only operations declared for bundles installed on each instance;
  - MCD generically schedules typed `mautic_console` operations from
    `runtime.plugin_operation_instance_settings`;
  - catalog rules comment/remove matching legacy cron, migrate its cadence and
    preserve all values from a previous tile/runtime schema;
  - plugin titles, bundle IDs, commands, defaults and cron tokens live in the
    MCC plugin catalog, not in MCD scheduler branches.
- Scheduler model:
  - single daemon loop
  - DB/config refresh on `poll_interval_sec`
  - dispatcher refill on `dispatch_interval_sec` (keeps target parallelism over
    time, but starts automatic ring work one task at a time)
  - dependent segment chains share one worker lane; unrelated chains may still
    occupy other segment workers
  - campaign-pressure segment throttling is threshold-based: queued or
    short-running campaigns do not throttle segments by themselves; pressure
    starts when `campaign_pressure_min_running_sec` or
    `campaign_pressure_min_running_count` is reached
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
- inventory and MCC-safe state include the instance runtime, container id,
  runtime paths/user, ZIP/Composer layout, declared capabilities, migration
  adapter and immutable image reference
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
  - apply path switches the source tree and synchronizes the actual venv
    `mcd_agent` package/console scripts from that exact source; metadata and
    imported code are verified before restart.
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
- `name = "custom|tiny|mini|midi|maxi|hiload|ultra|farm-tiny|farm-mini|farm-midi|farm-maxi|farm-hiload|farm-ultra"`

Selection authority:
- a fresh installation whose profile is still `passive` starts in `auto` mode;
- on every MCD service start, `auto` chooses the lower capacity class reported
  by logical CPU and physical RAM: `1/<4 GiB=tiny`, `2/4 GiB=mini`,
  `4/8 GiB=midi`, `8/16 GiB=maxi`, `16+/32+ GiB=hiload`, and
  `24+/96+ GiB=ultra`;
- RAM class boundaries allow for the normal firmware/kernel reservation visible
  in Linux `MemTotal` (for example, nominal 4 GiB is commonly about 3.7 GiB);
- `mcd-cli profile <name>` and an MCC profile change persist `manual` mode, and
  hardware detection no longer changes that host;
- `mcd-cli profile auto` is the explicit way to return to hardware-managed
  selection; `mcd-cli profile status` displays both authority and recommendation;
- an active profile that predates this state marker is preserved as manual
  during upgrade, preventing surprise changes on existing hosts.

Preset rules:
- `tiny`: single ring, no throttle, no whitelists, segments `1`, periodic full segment scan every `60s`; campaigns use one worker with actual trigger-due campaigns first and rebuild-due campaigns second, newest-first published list.
- `mini`: single ring, no throttle, no whitelists, segments `4`, periodic full segment scan every `120s`, campaign trigger `2`, campaign rebuild `1`, shared campaign cap `1`.
- `midi`: dual ring, no throttle, whitelists enabled, priority size `10`, parallel `3+1` for segments, updates, triggers; periodic full segment scan every `300s`.
- `maxi`: dual ring, throttle `200/5m`, whitelists enabled, segments `5+1`, triggers `3+1`, rebuilds `2+1`; periodic full segment scan every `300s`; during throttle only whitelist segments run in `1` stream.
- `hiload`: dual ring, throttle `200/5m`, whitelists enabled, segments `6+2`, triggers `4+2`, rebuilds `3+1`; during throttle only whitelist segments run in `2` streams and non-whitelist running segments are killed and queued to resume first after throttle ends.
- `ultra`: high-capacity dual ring for hosts with at least 24 CPUs and 96 GiB RAM; hardware-derived limits remain authoritative.
- `farm-tiny` through `farm-ultra`: manually selected high-density hardware line
  for many small, separate Mautic databases. MCC selects the class from actual
  CPU/RAM as a recommendation; an operator-selected profile and its slot counts
  remain effective even when hardware differs. All farm profiles periodically
  scan published segments every `300s`; hardware mismatch is a warning, not a
  runtime slot clamp.
- `custom`: uses explicit `[runtime]` values.

Scheduler host/instance capacity presets are literal MCD values: `passive=0/0`,
`tiny=1/1`, `mini=4/1`, `midi=4/2`, `maxi=8/4`, `hiload=12/6`,
`ultra=24/12`; farm presets are `farm-tiny=1/1`, `farm-mini=2/1`,
`farm-midi=4/2`, `farm-maxi=8/4`, `farm-hiload=12/6`, and `farm-ultra=24/12`.
Custom profiles retain explicit operator values. Hardware affects only the
recommendation and warning, never the selected profile's effective capacity.

All active profiles use one elastic host budget. Segment rebuilds may borrow idle
capacity but leave one emergency slot when the host has at least two slots;
campaign and import work may claim that slot immediately. The fairness watchdog
promotes any instance whose queued work has waited for five minutes. Every
instance owns one segment/import slot and one combined campaign
trigger/rebuild slot independently of shared-pool occupancy. Additional work
borrows the dynamic host pool. The oldest promoted owner also protects one
shared slot until it launches; a successful launch resets its wait age so the
next overdue instance advances. Queue throttle can reduce additional segment
capacity to whitelist-only, but retains one automatic segment baseline per
instance. Baseline starts in each lane are globally spaced by five seconds in
deterministic scheduler order. Pending imports are dispatched before segment
rings and own the shared segment/import baseline while active; segments may use
only remaining dynamic capacity. Exact-task locks, plugin operations,
migrations and the isolated recurring segment priority lane keep their existing
safety rules.

Segment stale-priority rule (all non-passive profiles):
- segments with `last_built_date` older than 24h (or missing) are force-added to priority ring;
- this rule is independent from normal weight threshold/top-N ranking;
- if regular ring is empty, its slot is reused by priority ring automatically until regular items appear.

Per-instance whitelist entries may define an opt-in `realtime` subset. These
IDs retain normal whitelist membership while also receiving dedicated executor
capacity that regular priority work cannot consume. Segment entries accept
`ids`, `interval_sec` and `parallel`; campaign entries accept `ids`, separate
`rebuild_interval_sec`/`trigger_interval_sec`, and separate
`rebuild_parallel`/`trigger_parallel`. Exact-task and campaign-root locks remain
shared with normal dispatch, so realtime capacity cannot duplicate a native
command already running for the same entity.

SQL segment auto-promotion:
- when `segment_sql_ring_enabled` and `segment_sql_auto_enabled` are true, MCD can rebuild SQL-safe segments directly in DB and remove them from native Mautic segment rings;
- repeated recent native segment failures/timeouts still promote SQL-safe segments into the regular SQL ring;
- SQL-safe page-hit segments are promoted into a dedicated long SQL ring after a successful native `mautic:segments:update -i <id>` takes at least `segment_sql_auto_long_native_min_duration_sec` seconds within `segment_sql_auto_long_native_history_sec`, even when the regular due-segment query would not select them;
- the long SQL ring runs before the regular SQL ring and is limited by `segment_sql_long_ring_max_per_tick`;
- direct SQL rebuilds update `lead_lists_leads`, `lead_lists.last_built_date`, build time metadata, and Mautic's segment count cache so the Mautic UI sees the segment as rebuilt.

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

Notes:
- `php_fpm` apply includes FPM pool/opcache/redis tuning. Global managed `98-mcd-php.ini` baseline is no longer used; legacy files are removed on apply if present.
- APT profile includes one-time Zabbix DB monitor bootstrap (`zbx_monitor@127.0.0.1`) with marker tracking and manual override via `mcd-cli zabbix bootstrap-mysql-user --force`.
- Zabbix monitors host services and hardware only. It does not receive per-instance Mautic version cache files; MCD keeps its internal, non-migrated state cache under `/opt/mcd/generated/`.
- Inventory keeps that cache lightweight, but invalidates it when local Composer or release metadata proves the installation advanced; the authoritative runtime is then probed before the next state payload.
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
- Cluster replica freshness checks:
  - `runtime.cluster_replica_freshness_enabled = true` enables application-table
    freshness checks on async replica nodes.
  - `runtime.cluster_replica_freshness_max_age_sec` is the default allowed age
    for the newest row in checked tables.
  - `runtime.cluster_replica_freshness_checks` is a JSON list of checks with
    `database`, `table`, `column`, optional `order_column` and optional
    `max_age_sec`. Prefer `order_column = "id"` for large append-only metric
    tables so the check uses the newest row by index instead of a full `MAX()`
    scan.
- Cluster Galera route/source eligibility is stricter than a TCP or systemd
  health check. A Galera node is marked eligible only when it reports
  `wsrep_cluster_status=Primary`, `wsrep_local_state_comment=Synced`,
  `wsrep_connected=ON` and `wsrep_ready=ON`. Donor, joiner and non-Primary
  states are reported as blocked even when MySQL is reachable.
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
- `mcd-cli profile auto`
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
- Non-passive profiles (`tiny|mini|midi|maxi|hiload|ultra|farm-tiny|farm-mini|farm-midi|farm-maxi|farm-hiload|farm-ultra|custom`) dispatch Mautic tasks.
- Automatic selection is persisted separately in
  `/opt/mcd/var/profile-selection.json`; it never overrides a manual selection.
- `mcd-cli profile auto` enables hardware-managed selection and immediately
  applies the current CPU/RAM recommendation.
- `mcd-cli profile passive`:
  - switches profile to `passive`,
  - restores cron from pre-active backups (or from MCD markers if backup missing),
  - restarts `mcd`.
- `mcd-cli profile <tiny|mini|midi|maxi|hiload|ultra|farm-tiny|farm-mini|farm-midi|farm-maxi|farm-hiload|farm-ultra|custom>`:
  - applies selected non-passive profile,
  - comments managed cron lines (`segments:update`, `campaigns:update`, `campaigns:trigger`, `campaigns:rebuild`, `import`),
  - always leaves `mautic:emails:send` under cron ownership; MCD does not migrate or schedule file-spool delivery,
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
- Per-instance PHP runtime:
  - Mautic and PHP CLI configuration remain the source of truth for timezone and limits; MCD does not override PHP values at command launch.
  - `mcd-cli instance-runtime status --json` reports which nginx vhost files can be materialized.
  - `mcd-cli instance-runtime apply` generates per-instance PHP-FPM pools in `/opt/mcd/generated/php/<version>/fpm/pools/`.
  - MCD connects those pools through one FPM include file `/etc/php/<version>/fpm/pool.d/99-mcd.conf` containing `include=/etc/php/<version>/fpm/pool.d/mcd/*.conf`.
  - `/etc/php/<version>/fpm/pool.d/mcd` is a symlink to the generated pools directory; individual pool files are not scattered in `/etc`.
  - Matching nginx vhosts are rewritten from the shared socket (`/run/php/php<version>-fpm.sock`) to the instance socket (`/run/php/php<version>-fpm-mcd-<slug>.sock`).
  - MCD runs every `bin/console` command through the configured host PHP binary (normally `/usr/bin/php`). It never creates `{instance}/.mcd`, a PHP wrapper, or CLI `-d` overrides.
  - Global CLI `conf.d/*.ini` files are host-wide and are the only source for PHP CLI limits and timezone.
  - Apply validates `php-fpm<version> -t` and `nginx -t` before reload and restores snapshots on validation failure.
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
  - `runtime.campaign_pressure_min_running_sec` defaults to `120`; a single
    running campaign must live at least this long before segment throttling is
    treated as campaign pressure.
  - `runtime.campaign_pressure_min_running_count` defaults to `2`; this many
    simultaneous campaign workers trigger campaign pressure immediately. Set to
    `0` to disable the count rule.
  - Active MCD profiles run the campaign safety audit every 60 seconds. It
    evaluates published campaigns against Mautic's due-work conditions and
    runs `mautic:campaigns:trigger -i ID` only for eligible IDs; passive
    profiles keep external cron ownership.
  - When audit or due-work planning places a campaign in the priority ring but
    its priority worker count is zero, spill capacity dispatches that priority
    candidate before continuing an always-populated regular ring. This bounds
    detection-to-rebuild admission by the next scheduler pass while retaining
    host, instance, campaign and fairness limits.
  - on weak hosts start lower (e.g. `1000`) so one long campaign does not block full daemon cycle for too long.
- Runtime tuning for catalog plugin operations:
  - canonical per-instance values live under `runtime.plugin_operation_instance_settings`;
  - scheduled operations default to disabled when neither old state nor a
    matching legacy cron exists;
  - explicit old enabled/disabled state and every other mapped field are
    canonicalized before cron migration; a cron enables the operation only
    when no older state exists.
- Self-update safety:
  - `runtime.mcd_update_defer_during_campaigns = true` prevents MCD self-update while campaign trigger/rebuild/update console jobs are running.
  - daemon auto-update keeps a short cooldown after campaign console activity to avoid restarting between batch passes.
- Retry and watchdog:
  - `runtime.task_retry_max`, `runtime.task_retry_delay_sec` control retries for concrete command execution.
  - `runtime.task_retry_max` semantics:
    - `1` = no retry (only initial attempt),
    - `>1` = bounded retries (attempt cap),
    - `0` or negative = unlimited immediate retries (with `task_retry_delay_sec` pause).
  - `runtime.segment_retry_count` is the host-profile policy for automatic
    segment commands and counts retries after the initial attempt: `0` means
    one start, `3` (the default) means at most four starts. A non-zero Mautic
    exit is recorded as the segment's terminal outcome after the bound; it is
    not a daemon failure, and the dispatcher continues with the next eligible
    item while the failed item remains due for the next cycle.
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
  - `mcc_runtime_desired_state` (revision, provenance, instance revisions, and last confirmed apply state)
  - `active_runtime` (last runtime apply metadata)

## Runtime Sync (MCC <-> MCD)
- Canonical desired state:
  - MCC stores revisioned host state plus per-instance state in `desired_runtime_states`.
  - Instance records use immutable `instance_uid`, not a root path or the current host, and therefore follow an installation after a migration.
  - Legacy host JSON remains a compatibility mirror. MCC promotes legacy root/domain keyed instance maps to UID records on the next agent/API contact.
  - Each state row records source, origin host and agent version, modification time, revision, last applied revision, actual apply status, error, and the host that confirmed application.
  - MCD writes desired changes using optimistic revision matching. A disconnected or reinstalled agent with no matching base revision receives MCC state instead of overwriting newer confirmed state.
  - Static agent configuration continues to use MCC's existing desired config snapshot and startup recovery path; dynamic settings use the revisioned live protocol above. Together they let a reinstall recover its config at startup and apply post-start changes without a restart.
- Source of truth split:
  - `desired` runtime overrides are revisioned in MCC and mirrored to the legacy host table (`runtime_overrides_json`) for compatibility.
  - `observed` runtime overrides are pushed by MCD and stored separately in MCC (`observed_runtime_overrides_json`).
- Template runtime keys:
  - `runtime.host_template=true` marks host as template source.
  - `runtime.template_autopromote_on_clone=true` enables clone autopromote to new host identity in MCC when local hostname differs from configured `[mcc].host_name`.
- MCC -> MCD:
  - normal path: daemon polls MCC runtime endpoint.
  - immediate path: `mcc_cli host-runtime set/unset` triggers `mcd-cli runtime-overrides trigger`, daemon consumes trigger and pulls immediately.
  - per-instance `segment_special_rings_v1` is translated into the existing
    priority/super-priority instance settings by immutable UID; validated
    `slots_override` controls that lane's parallel limit, and malformed data
    is rejected rather than silently dropped.
  - the selected MCC workload profile is kept as canonical provenance across
    every poll/trigger rebase; a stale observed `passive` state cannot erase an
    active farm profile, while an explicit desired profile can change it.
  - a successful live apply is acknowledged back to MCC; no daemon restart is required.
- MCD -> MCC:
  - MCD state push contains `runtime_overrides` and extracted instance-scoped values keyed by `instance_uid`.
  - daemon also watches local mutable runtime section fingerprint and pushes to MCC immediately when it changes.
  - any mutating command that already does immediate state push updates observed runtime view in MCC without separate polling.
  - `runtime.tasks_compact_*` controls quiet-window compaction cadence (`DELETE` + optional `VACUUM`).
  - systemd service uses `KillMode=control-group` and `TimeoutStopSec=15` so a
    restart cannot leave child Mautic commands orphaned while preserving the
    normal per-task process-group timeout behavior.
- Plugin interactive sync:
- MCD reads `manifest.json` from MCC plugin repo
  - shows status table (`OK`, `UPDATE`, `MISSING`, `BROKEN`) plus local-only rows (`-`)
  - table columns: installed version (from `plugins/<Bundle>/Config/config.php`) and server version (from manifest)
  - applies selected plugin operations, then runs `cache:clear` and `mautic:plugin:install`
  - after install/replace sets ownership to `www-data:www-data` for the updated bundle directory

- Backup module:
  - section `[backup]` in system config
  - host-level direct write to remote share via `sshfs` (no local dump staging)
  - opt-in instance backup transport `[backup.storage].kind = "local"` writes
    directly to an already-mounted absolute `local_path`; MCD does not manage
    mounts, encryption, credentials or storage lifecycle
  - local targets are root-owned, non-symlink, non-world-writable active
    mountpoints by default; unsafe broad paths, traversal and instance/target
    overlap fail closed
  - instance restore uses the generation marker to restore persistent files to
    the exact managed instance root and Myloader data to the exact tenant DB;
    missing targets or artifacts fail instead of returning a zero-work success
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
  - guarded Composer Mautic 6 to 7 upgrades require an explicit target and
    `--allow-major`; MCC runs a Storage Box instance backup first and starts the
    upgrade only after that backup succeeds
  - the 6 to 7 path is unavailable unless the active database is independently
    reported as MySQL 8.4+ or MariaDB 10.11+; MCD does not install or upgrade a
    database because a host-level database change can affect other instances
  - the Mautic 6 to 7 Composer path prepares Composer and Node 20, updates
    `composer.json` to the selected Mautic 7 target, runs Composer with
    dependencies, clears cache, finishes the Mautic updater, and reconciles
    known Doctrine migration metadata drift before the final migration check
  - `--with-system-upgrade` is available for a guarded 6-to-7 release transition
    and for an MCC-preflighted, operator-acknowledged strictly forward manual
    Mautic 7.x upgrade. The manual path requires `--backup` and the immutable
    selected patch plan/run ID, stays in the same maintenance session, and runs
    only after Mautic reaches the selected target.
  - The PHP 8.4 stage installs PHP 8.4 packages,
    migrates custom PHP ini files such as `60-custom.ini` and
    `90-redis-sessions.ini`, rewrites nginx PHP-FPM socket references from
    8.3 to 8.4, validates nginx, restarts services, and purges PHP 8.3 only
    after all other discovered host instances are Mautic 7-compatible
  - ZIP-to-Composer migration rewrites active root/www-data cron paths to the
    Composer root, disables the removed `mautic:emails:send` command on Mautic
    5+, and fails with rollback if an active source-root cron entry remains
  - MCD repairs the Mautic 7 GrapesJS CKEditor GPL-license fallback on every
    inventory cycle, including passive hosts, and reapplies it after plugin or
    Mautic updates overwrite either the source or published builder artifact;
    the scoped builder URL revision also invalidates previously cached broken JS
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
  - `mautic:email:fetch` (`interval_sec = 900`)
- Mautic 5/6/7 message queue via dedicated MCC/MCD settings:
  - `message_queue_instance_settings[<instance>].enabled = false` by default
  - `message_queue_instance_settings[<instance>].interval_sec = 3600`
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
- MCC-managed own-host mail uses `bounce@<instance>` as its return path and
  accepts only `bounce@`, `fbl@` and `abuse@` on the instance MX. Delivery
  reports and ARF complaints are parsed immediately into email DNC state for
  that instance; no IMAP mailbox credentials are required. External SMTP,
  Amazon SES and SendGrid profiles keep their Mautic return path unchanged,
  including an intentionally empty value.
- While at least one own-host domain is active, MCD maintains one exact,
  comment-tagged TCP/25 firewall rule through `mcd-local-mail-firewall.service`.
  Disabling the final own-host domain removes only that managed rule.

## Benchmark notes (current host)
- Baseline standard command (scheduler paused):  
  `sudo -u www-data php /var/www/mautic/bin/console mautic:segments:update --batch-limit=1000`
  - measured on `2026-02-17`: `elapsed=18.48s`
- Previous measurement before latest fixes on same host: `elapsed=22.84s`
- MCD scheduler now launches independent task types in parallel (confirmed in one cycle): `segment`, `campaign_update`, `campaign_trigger`, `job:broadcasts-send`, `job:messages-send`.
