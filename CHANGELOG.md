# MCD Changelog

## 0.9.106 - 2026-05-31

- Changed: plugin install/update no longer patches Mautic
  `PluginBundle/Helper/ReloadHelper.php` for missing plugin metadata. MCD now
  adds an empty `metadata` array to the selected plugin's installed
  `Config/config.php` before running Mautic plugin reload, keeping the
  workaround in the installer/plugin layer instead of Mautic core.
- Fixed: plugin metadata DB repair now checks whether `{prefix}plugins.metadata`
  exists before running repair SQL, avoiding noisy warnings on Mautic schemas
  without that column.

## 0.9.105 - 2026-05-30

- Fixed: IPv6 status now reports a persistent disable intent separately and
  treats hosts with unreadable/missing runtime IPv6 sysctl keys as disabled when
  the MCD persistent disable file is present and requests all/default/lo=1.
  This covers systems where IPv6 sysctl paths disappear after disablement.

## 0.9.104 - 2026-05-30

- Fixed: IPv6 disabled detection now tolerates vanished transient interface
  entries reported as `?` when the persistent disable intent exists and all
  readable runtime IPv6 keys are disabled. This prevents MCC remediation rules
  from repeatedly targeting already-remediated hosts.
- Added: regression tests for the transient-interface IPv6 status case.

## 0.9.103 - 2026-05-30

- Changed: composer-based Mautic upgrades now use normal `bin/console
  cache:clear` first. Hard deletion of `var/cache/prod` is reserved for
  fallback recovery when the standard cache clear fails.
- Added: regression tests proving successful standard cache clear does not run
  the hard-clear path.

## 0.9.102 - 2026-05-30

- Fixed: `mautic-upgrade apply` now clears `var/cache/prod` using an atomic
  rename/recreate flow. Live web traffic can no longer fail the upgrade with
  `Directory not empty` while PHP-FPM is regenerating cache files.
- Added: regression tests for live cache-clear races during Mautic upgrades.

## 0.9.101 - 2026-05-30

- Fixed: `mautic-upgrade apply` now wraps the upgrade in an MCD maintenance
  guard: scheduler dispatch is paused, cron is stopped before the upgrade, and
  both are restored to their original ownership state after success or failure.
- Added: maintenance guard tests covering owned and pre-existing maintenance
  states, so passive/maintenance hosts do not get cron restarted incorrectly.

## 0.9.100 - 2026-05-30

- Added: `mcd-cli mautic-upgrade apply --target <version>` can now run an
  explicit same-major target selected by MCC.
- Changed: patch upgrades remain the default. One-step minor upgrades such as
  `5.1.x -> 5.2.x` or `7.0.x -> 7.1.x` require the explicit `--allow-minor`
  flag; major upgrades and multi-minor jumps remain blocked.

## 0.9.99 - 2026-05-30

- Fixed: successful `mautic-upgrade apply` now verifies the final Mautic
  runtime version and refreshes the local `.mcd/mautic.version` cache
  immediately. Agent state pushes no longer report the previous Mautic patch
  version after an upgrade completed successfully.

## 0.9.98 - 2026-05-30

- Fixed: `self-update status` normalizes stale
  `last_cluster_update_result=cluster update: waiting ...` once the local host
  has already applied the target version successfully.

## 0.9.97 - 2026-05-30

- Fixed: `self-update status` no longer reports stale
  `active_campaign_processes` after a deferred update has already succeeded or
  a later status check sees no live campaign process.

## 0.9.96 - 2026-05-30

- Fixed: successful cluster self-update now refreshes the local
  `last_cluster_update_result`, so `mcd-cli self-update status` does not keep a
  stale `waiting for peer install` message after the shared coordinator is
  already done.
- Fixed: cluster backup local-date checks now honor an explicit timezone-aware
  reference time, making the safety test stable on UTC and non-UTC MCC runners.

## 0.9.95 - 2026-05-30

- Fixed: agent state and update checks now report the actually running MCD
  version as the canonical `agent_version`; the source-tree version remains a
  diagnostic field only. This prevents MCC from treating stale `/opt/mcd/src`
  metadata as the live agent version.
- Fixed: Mautic 7 segment planning now suppresses explicit parent segment
  launches already covered by Mautic's built-in leadlist dependency cascade,
  avoiding repeated parent/child rebuild loops.
- Changed: self-update cleanup defaults now remove old update archives and
  pre-update source backups after successful updates instead of keeping a trail
  of old agent packages.

## 0.9.94 - 2026-05-29

- Fixed: segment whitelist IDs are now force-added to the current segment
  planning set when the segments are published, so instance whitelists work
  even when the normal `segments_due` SQL does not return those IDs.

## 0.9.93 - 2026-05-29

- Changed: segment whitelist common files now support scoped lines in the
  format `<instance-key>: <segment ids>`, so the file remains readable while
  preserving instance-level whitelist behavior.
- Added: legacy one-id-per-line segment whitelist files are converted once
  into scoped format; single-instance hosts use the instance key and
  multi-instance hosts use `default` to preserve the previous host-wide
  behavior.
- Added: MCD synchronizes the common segment whitelist file from
  per-instance runtime settings after startup and runtime override updates.

## 0.9.92 - 2026-05-29

- Fixed: instance-scoped segment whitelist lookup no longer reads the legacy
  host-level whitelist file when a matching instance override exists.

## 0.9.91 - 2026-05-29

- Changed: segment whitelist settings from MCC are now scoped per Mautic
  instance, so multi-instance hosts no longer share one host-wide segment
  whitelist from the instance operations tile.
- Fixed: segment planning and throttle-only dispatch now resolve whitelist IDs
  from the active instance key/root/domain before weighting, priority splitting,
  and throttled whitelist-only launches.

## 0.9.90 - 2026-05-28

- Fixed: monitored email parser now runs on `tiny` profile instances before
  the tiny campaign scheduler skips the generic multi-ring path.

## 0.9.89 - 2026-05-28

- Added: MCD-managed monitored mailbox parser with per-instance UI settings
  for interval, batch size, enabled email types, force-seen scanning, and
  delete-after-DNC behavior.
- Added: feedback-loop, bounce, and unsubscribe parsing that inserts matching
  Mautic contacts into email DNC from monitored mailbox messages, including
  Mail.ru-style FBL messages that only expose the recipient through the
  embedded original message.
- Added: exact internal email whitelist for manually forwarded problem emails;
  whitelisted contacts are skipped and any existing email-DNC rows for them are
  removed during parser runs.
- Changed: when the MCD monitored-email parser is enabled for an instance,
  daemon-managed and legacy cron `mautic:email:fetch` jobs are skipped/commented
  so the broken standard parser does not race the MCD parser.

## 0.9.88 - 2026-05-28

- Changed: import polling now claims the next available segment-ring slot
  globally instead of running on a separate scheduler lane. Pending imports get
  priority for the next freed segment slot, while existing segment work keeps
  running and remaining capacity can still be used by segment rings.

## 0.9.87 - 2026-05-28

- Fixed: migration-created nginx vhosts now point to the detected Mautic web
  document root (`docroot`/`public` when present) instead of always using the
  application root, avoiding 404s on composer/docroot installs.

## 0.9.86 - 2026-05-28

- Added: live instance migration relay now streams matching LetsEncrypt
  certificate material through MCC before target nginx finalization, preserving
  HTTPS without requiring source-to-target network access or MCC archive staging.

## 0.9.85 - 2026-05-28

- Changed: instance migration now supports MCC relay streaming. Source MCD
  streams files/database to stdout, MCC pipes the stream in real time, and
  target MCD receives from stdin. Target hosts no longer need direct SSH/network
  access to source hosts.
- Added: relay helper commands for source file/database streams and target
  preflight, file receive, database import, and finalization.

## 0.9.84 - 2026-05-27

- Changed: live instance migration now creates a clean target layout
  (`/var/www/<short>/public_html`, `baza_<short>`, `korisnik_<short>`) and
  patches target `local.php` after the final file sync. Source DB names such as
  `baza_ss` no longer have to exist or remain identical on the target host.
- Fixed: target migration preflight no longer treats the source root/DB name as
  the target artifact names. Existing unrelated Mautic installs on the target
  host can coexist; only the newly derived target root/DB are protected from
  overwrite.

## 0.9.83 - 2026-05-27

- Changed: keep the approved Mautic 6 `PluginUpdateEvent` metadata hotfix as
  the only daemon-applied Mautic core patch.
- Removed: retired the Mautic 7 campaign timezone and page-hit cascade core
  patch applicators. The daemon now restores those retired MCD patches from
  their backups when found.
- Added: page-hit queue cleanup now runs in MCD housekeeping by deleting stale
  Doctrine `PageHitNotification` rows that reference missing `page_hits`
  records, without modifying Mautic core.
- Kept: campaign trigger/rebuild scheduling remains handled by MCD SQL using
  both UTC and instance-local clocks, without patching Mautic campaign files.

## 0.9.82 - 2026-05-27

- Added: `mcd-cli instance-migrate source-probe --root ... --json` reports
  per-instance file size, database size, DB engine/version, binlog catch-up
  readiness, and target free-space estimate without exposing DB credentials.
- Added: host signals now include cached filesystem free-space data for MCC
  migration target selection.
- Added: same-engine instance migration runner. The target host pulls files and
  database from the source over temporary SSH access, performs a final
  source-maintenance sync/import, verifies the target, and returns
  `catchup_ok=true` before MCC may switch DNS.

## 0.9.81 - 2026-05-27

- Fixed: Mautic image deployments now detect an existing local MySQL admin
  defaults file when root socket/no-password access is disabled. Database
  preflight, schema creation, SQL imports, and myloader imports reuse that
  credentials file without putting passwords on the command line.
- Fixed: gzip SQL import failures now report the MySQL stderr/stdout that made
  the client exit instead of masking the cause as a Python `BrokenPipeError`.
- Fixed: SQL image imports rewrite dump INSERTs for tables with generated
  columns so MySQL 8 can recalculate virtual values instead of rejecting
  explicit generated-column data from older/default image dumps.

## 0.9.80 - 2026-05-24

- Fixed: cluster plugin removal now deletes both the manifest bundle path and
  the runtime install alias, verifies delete propagation even when the
  reference node already has no local files, and runs reference-node sync/cache
  checks locally instead of enqueueing a manual request to itself.

## 0.9.79 - 2026-05-24

- Fixed: cluster backup status now fails the aggregate backup state when the
  last offsite files archive marker points to a missing archive. MCC no longer
  receives an overall `ok` backup status while
  `last_offsite_files_archive_ok=false`.

## 0.9.78 - 2026-05-24

- Fixed: segment scheduler now skips segments with unsafe bare relative date
  expressions such as `today -1`, and repeated segment failures enter a short
  cooldown instead of respawning indefinitely. This stops retry storms while
  leaving the bad business filter visible for manual correction.

## 0.9.77 - 2026-05-24

- Fixed: Mautic 7 campaign date-action editor now treats operator input as
  instance-local time and stores the model value as UTC. This prevents
  `campaign_events.trigger_date` from drifting by the instance timezone offset
  on save/display for Mautic 7 campaigns such as Grubin.

## 0.9.76 - 2026-05-24

- Fixed: MCD now reports the installed source-tree version separately from the
  running daemon version. MCC uses the installed version for host inventory and
  update decisions, preventing rollback/service-mismatch cases from showing a
  stale newer version such as `0.9.75` while `/opt/mcd/src` is actually older.
- Fixed: self-update failure/defer paths now release MCC update sessions with
  the installed source version instead of the in-memory daemon version, and a
  source/running mismatch schedules an MCD service restart to converge.

## 0.9.75 - 2026-05-23

- Fixed: campaign trigger repeat guard is now backed by persisted task history
  as well as in-memory state. Restarting MCD during rollout no longer lets the
  same per-campaign trigger ID relaunch before the configured audit/retry
  interval.

## 0.9.74 - 2026-05-23

- Fixed: campaign trigger repeat guard now uses the campaign audit interval
  as a floor. This prevents the same published campaign from being relaunched
  every few seconds when SQL still sees it as due after a per-campaign trigger
  pass.

## 0.9.73 - 2026-05-23

- Fixed: campaign trigger audit no longer keeps audit-only published campaign
  IDs spinning in the trigger ring every few seconds. Campaign trigger/rebuild
  IDs are removed from the current ring after one launch and are re-added only
  by the next planner pass if real due work still exists. This preserves
  per-campaign `-i ID` dispatch while preventing no-op trigger storms.

## 0.9.71 - 2026-05-22

- Fixed: campaign trigger planning now catches published campaigns whose
  contacts are already present in `campaign_leads` but whose root action event
  log has not been bootstrapped yet. MCD still dispatches only per-campaign
  `mautic:campaigns:trigger -i ID`; it no longer misses fresh root-immediate
  campaigns such as MiA Maya campaign 385.

## 0.9.70 - 2026-05-19

- Fixed: per-instance recovery/pre-delete backups now use `mydumper` as the
  primary and required dump engine, matching the standard host backup path.
  MCD auto-installs the configured `mydumper` package when missing instead of
  silently using slow `mysqldump` for instance backups.
- Added: image restore can import database artifacts produced by `mydumper`
  through `myloader`, installing the configured package when required.

## 0.9.69 - 2026-05-19

- Fixed: instance backup profiles now honor `remote_root_dir`, so MCC recovery
  and pre-delete backups are written under the operator-selected storage
  folder instead of the default `backup/...` root.
- Fixed: instance backup auto-install now covers the local MySQL dump client
  (`default-mysql-client`) in addition to `sshfs`, preventing small hosts from
  failing after storage setup because `mysqldump`/`mariadb-dump` is missing.

## 0.9.68 - 2026-05-18

- Fixed: mailer dependency preflight no longer runs `composer require` inside
  an invalid or non-Mautic `composer.json`. This prevents zip-based Mautic
  installs from having their `vendor` tree pruned by an unrelated Composer
  project created during sender dependency recovery.
- Guarded: mailer dependency preflight now verifies `bin/console --version`
  before and after Composer changes, failing loudly instead of silently masking
  a broken Mautic runtime.

## 0.9.67 - 2026-05-18

- Fixed: cluster-channel self-update now reconciles an active Galera-backed
  rollout even while the local MCC check timer is sleeping. Nodes that already
  run the target version immediately mark their shared download/install state
  as complete, so older peers do not wait for a stale `pending` package
  download.
- Fixed: completed or failed cluster update plans are no longer treated as
  active work during local timer reconciliation, avoiding unnecessary shared
  state writes after rollout completion.

## 0.9.66 - 2026-05-17

- Fixed: Mautic 4 transaction compatibility patch now also runs as a recovery
  step when a selected plugin is already at the target version. This recovers
  plugin updates where files were copied but `mautic:plugins:reload` previously
  failed with `There is no active transaction`.

## 0.9.65 - 2026-05-17

- Fixed: plugin compatibility patches now run even when the selected plugin is
  already at the target version. This recovers prior failed updates where plugin
  files were copied successfully but Mautic reload failed, leaving future retries
  in `OK` state without executing the recovery patch.

## 0.9.64 - 2026-05-17

- Fixed: Mautic 5/6/7 plugin update post-step now patches
  `PluginBundle/Helper/ReloadHelper.php` so missing plugin metadata is passed to
  `PluginUpdateEvent` as an empty array instead of `null`. This prevents bundle
  updates from failing with `Argument #3 ($metadata) must be of type array, null
  given`.

## 0.9.63 - 2026-05-17

- Fixed: plugin post-step now repairs NULL or malformed rows in
  `{prefix}plugins.metadata` before running `mautic:plugins:reload`, not only
  after a failed reload. This prevents Mautic 5/6/7 updates from failing with
  `PluginUpdateEvent metadata must be of type array, null given`.

## 0.9.62 - 2026-05-17

- Fixed: Mautic 4 plugin update post-step now hardens
  `IntegrationsBundle/Migration/Engine.php` against Doctrine/PDO transaction
  state drift. `mautic:plugins:reload` no longer fails with `There is no active
  transaction` after a bundle has already been copied.

## 0.9.61 - 2026-05-17

- Fixed: cluster file backup producer no longer includes Gluster internals or
  runtime/system paths such as `/var/lib/glusterd`, `/var/lib/mysql`, `/run`,
  `/var/log`, `/tmp`, `/mnt`, and `/var`. This keeps backup-layer Syncthing from
  scanning Gluster metadata and other non-backup system state.
- Fixed: cluster file backup producer excludes root-only/sensitive files such
  as `/etc/shadow`, `/etc/gshadow`, `/etc/mysql/debian.cnf`, SSH host private
  keys, and `/root/*` paths so Syncthing does not repeatedly fail on unreadable
  backup-layer entries.
- Fixed: cluster file backup producer normalizes permissions inside the
  generated Syncthing layer, without changing source files, so ordinary service
  configs can be transported even when source permissions are root-only.
- Changed: default cluster node file set now keeps only curated host/service
  config paths and Syncthing metadata needed for restore diagnostics; full
  `/etc` and root crontab trees are no longer copied into the Syncthing layer.

## 0.9.60 - 2026-05-17

- Fixed: MCC runtime saves now apply to the active daemon scheduler
  immediately, not only to the persisted TOML file. Service cleanup schedules
  reset their in-process cursors after cleanup-related runtime changes, so a
  newly enabled cleanup starts on the next scheduler tick without requiring an
  MCD restart.

## 0.9.59 - 2026-05-17

- Changed: orphan page-hits cleanup per-instance controls now match empty
  contacts cleanup: schedule, batch size, max runs, and enabled state. Grace,
  max-runtime, and batch sleep remain internal host safety defaults and are no
  longer exposed or saved as per-instance MCC settings.
- Fixed: orphan page-hits cleanup UI no longer keeps stale hidden fields in the
  save payload and now aligns the operator field order with empty contacts
  cleanup.

## 0.9.58 - 2026-05-17

- Changed: orphan page-hits cleanup now uses the same schedule/session model as
  empty contacts cleanup: interval, cron, or nightly window, with one SQL batch
  per dispatch tick until the schedule run is empty or the configured max-run
  limit is reached.
- Changed: legacy orphan page-hits `quiet_hour`/`quiet_window_min` settings are
  read as a compatible nightly-window schedule.

## 0.9.57 - 2026-05-17

- Changed: Leuchtfeuer Housekeeping plugin commands now use the same
  per-instance service cleanup fair queue as empty contacts cleanup and orphan
  page-hits cleanup. MCD treats the plugin command as a normal Mautic
  maintenance command, so it no longer starts in parallel with competing
  cleanup work for the same instance.

## 0.9.56 - 2026-05-17

- Fixed: SQL maintenance cleanups now use a per-instance fair queue. Empty
  contacts cleanup and orphan page-hits cleanup no longer monopolize the same
  Mautic database cleanup lane; when both are due, each dispatch tick runs only
  one short batch from one cleanup type and round-robins the next due type.
- Fixed: orphan page-hits cleanup keeps its configured multi-batch behavior only
  when it is the only due cleanup task. When another service cleanup is due, it
  yields after one batch to avoid lock storms and deadlocks.

## 0.9.55 - 2026-05-17

- Fixed: empty contacts cleanup now uses the same drain-loop policy for
  interval, cron, and nightly-window schedules. Each schedule occurrence runs
  one SQL batch per daemon dispatch tick until a pass deletes zero rows or the
  configured repeat limit is reached; `interval_sec` only defines interval-mode
  schedule starts, not an inner throttle.
- Fixed: empty contacts cleanup no longer applies the hidden
  `empty_leads_cleanup_max_batches_per_run` multiplier inside one dispatch
  pass; the UI repeat limit is the single operator-visible limiter.

## 0.9.54 - 2026-05-17

- Fixed: empty contacts cleanup in `nightly_window` mode now behaves as a
  drain loop. During the configured window it retries every daemon dispatch
  until a successful pass deletes zero rows; `interval_sec` is no longer used
  as an attempt throttle for window-based cleanup.

## 0.9.53 - 2026-05-16

- Fixed: campaign scheduler keeps the due-ring architecture strictly per
  campaign. The follow-up audit setting is bounded by campaign id and never
  falls back to global `mautic:campaigns:trigger` without `-i`.
- Added: `runtime.campaign_trigger_audit_interval_sec` as the safety interval
  for explicit per-campaign trigger auditing.

## 0.9.52 - 2026-05-16

- Fixed: campaign trigger due SQL is now strictly driven by existing due
  `campaign_lead_event_log` rows. Missing root-action logs were not a stable
  trigger signal: Mautic can return success without changing state, causing
  tiny-profile hosts to loop one campaign and starve real due campaigns.
- Tests: added regression coverage for long-running campaigns and for avoiding
  root-action trigger loops without event-log evidence.

## 0.9.51 - 2026-05-16

- Fixed: campaign trigger/rebuild due SQL no longer applies any age-based lower
  bound to campaign event `trigger_date` checks. Long-running campaigns such as
  welcome flows and abandoned-cart flows can receive contacts months after
  creation, so due detection must be based on Mautic state, not event age.
- Fixed: persisted trigger/rebuild SQL with old seven-day lower bounds on
  `campaign_events.trigger_date` is migrated to the packaged default.

## 0.9.50 - 2026-05-16

- Fixed: campaign trigger due SQL no longer ignores old pending
  `campaign_lead_event_log` rows. The lower seven-day bound was valid for
  root-action bootstrap protection, but it incorrectly skipped real backlog
  events that Mautic's global `mautic:campaign:trigger` still processes.
- Fixed: persisted trigger SQL with the old seven-day lower bound is migrated
  to the packaged default during config load, so existing hosts stop missing
  old but valid campaign work without manual config edits.

## 0.9.49 - 2026-05-16

- Hardened: cluster plugin coordination now uses existing MCD state tables only
  and fails closed instead of bootstrapping/upgrading Galera-backed state schema
  during plugin operations.
- Hardened: plugin sync-check nodes write their status to node-scoped
  `runtime_sync` rows; only the reference node owns the shared operation row.
- Hardened: cluster plugin `pre_sql` DDL guard now catches leading comments,
  MySQL executable comments, and dangerous later statements in multi-statement
  strings.

## 0.9.48 - 2026-05-16

- Fixed: campaign trigger due SQL no longer loops on old contacts that already
  have a campaign event log for the current rotation but are missing a log for
  one specific root action. This prevents empty `mautic:campaigns:trigger -i`
  storms such as alex-personal campaign 45 while still bootstrapping genuinely
  new campaign contacts with no event log at all.
- Fixed: persisted trigger due SQL with the older event-specific root-action
  log check is migrated to the packaged safe default during config load.

## 0.9.47 - 2026-05-16

- Changed: plugin install/update/remove is cluster-coordinated when MCD runs
  with a Galera-backed `mysql_hybrid` state backend. Any node may receive the
  request, but it delegates execution to the reference node (first cache-route
  host).
- Added: cluster plugin workflow phases in the existing `runtime_sync` state:
  reference-only file changes and SQL tweaks, per-node file digest sync checks,
  per-node cache clear fan-out, and reference-only `mautic:plugin:install`.
- Guarded: cluster plugin operations no longer run file/DB changes on multiple
  nodes, and runtime coordination uses existing host-scoped MCD state rows
  without runtime DDL or shared writes without node identity.
- Guarded: manifest `pre_sql` in cluster mode blocks DDL/table-maintenance
  statements such as `ALTER`, `DROP`, `TRUNCATE`, `OPTIMIZE`, and `REPAIR`.

## 0.9.46 - 2026-05-16

- Added: per-instance runtime overrides for orphan `page_hits` cleanup, so MCC
  can tune cleanup pace per Mautic installation instead of only per host.
- Added: scheduled Leuchtfeuer Housekeeping support for Mautic 4/5/6/7 instances with
  `LeuchtfeuerHousekeepingBundle`, routed through the normal persistent MCD task
  queue and quiet-window guards.

## 0.9.45 - 2026-05-15

- Fixed: plugin conflict cleanup now protects the selected runtime install path
  when multiple manifest variants share the same `install_bundle`. Updating the
  stable Amazon SES bundle no longer removes `AmazonSesBundle` while cleaning
  `AmazonSesBundleDev`/`AmazonSesOriginalBundle` aliases.

## 0.9.44 - 2026-05-15

- Changed: MCC-managed empty contact cleanup can run through a nightly window
  such as `22:00-09:00`, repeating batches while the window is open instead of
  stopping after one cron minute.
- Added: per-instance cleanup batch size and optional max-runs-per-window
  controls; `0` or an empty value means no run limit inside the window.
- Fixed: empty contact cleanup now treats both SQL `NULL` and empty strings as
  empty email/mobile values, and pauses cleanly while the backup guard is active.

## 0.9.43 - 2026-05-15

- Fixed: campaign trigger/rebuild due SQL no longer references
  `campaigns.deleted`, which is absent on Mautic 4 installations and caused the
  active campaign rings to stop planning triggers after the 0.9.42 rollout.
- Fixed: persisted campaign due SQL that already contains `campaigns.deleted`
  is now treated as unsafe legacy SQL and migrated back to the packaged
  compatibility-safe default during config load.

## 0.9.42 - 2026-05-15

- Fixed: migration of persisted campaign trigger SQL now also catches hosts
  that already had the root-action recovery branch but still used the old
  `trigger_mode=immediate` before `trigger_date` semantics.

## 0.9.41 - 2026-05-15

- Fixed: root campaign actions with a stored `trigger_date` are now treated as
  date-gated even when Mautic stores `trigger_mode=immediate`. This prevents
  the MCD root-action recovery branch from starting scheduled Mautic 7
  campaigns early while still catching them after the configured time if Mautic
  failed to create event-log rows.

## 0.9.40 - 2026-05-15

- Fixed: package metadata now allows Python 3.10 hosts. The 0.9.39 campaign
  scheduler fix was runtime-compatible with Python 3.10, but `requires-python`
  incorrectly blocked normal pip-based rollout on Ubuntu 22.04 agents.

## 0.9.39 - 2026-05-15

- Fixed: persisted `campaign_triggers_due` SQL from the intermediate
  UTC+local scheduler fix is now migrated when it lacks the Mautic 7 root-action
  branch. This restores trigger due detection for campaigns where active
  `campaign_leads` exist but root action event-log rows were never created.

## 0.9.38 - 2026-05-15

- Changed: automatic campaign trigger runs are unlimited by default
  (`campaign_limit=0`), so large campaigns are allowed to process the full
  audience instead of stopping at the old 60k cap. `batch_limit` still controls
  per-batch memory/DB pressure.
- Fixed: legacy `campaign_trigger_template` values containing
  `--campaign-limit={campaign_limit}` are normalized to the current
  `{campaign_limit_arg}` form during config load, so `0/off/unlimited` really
  removes the option even on hosts with old local `mcd.toml`.
- Fixed: the historical local/runtime value `campaign_limit=60000` is migrated
  to unlimited. This removes the old implicit cap from existing hosts without
  requiring manual runtime cleanup.

## 0.9.37 - 2026-05-15

- Fixed: campaign trigger planning now catches published campaigns whose active
  `campaign_leads` already exist but whose root action event logs were never
  seeded. This closes the Mautic 7 gap where contacts entered a campaign but no
  trigger worker started because `campaign_lead_event_log` was still empty.
- Fixed: campaign rebuild planning no longer tries to seed missing root action
  event logs. Mautic 7 can mark due root actions as triggered during rebuild
  without sending mail, so root actions are routed to the trigger lane instead.
- Fixed: campaign trigger/rebuild due SQL now accepts both UTC and
  instance-local DATETIME windows generated from the Mautic `default_timezone`.
  This avoids hard-coded timezone offsets and covers installations that store
  naive campaign trigger dates in either clock.

## 0.9.36 - 2026-05-14

- Added: shared campaign SQL time-context helper with explicit UTC/local
  semantics. Mautic campaign event/log trigger dates stay compared against UTC,
  while publish windows use the instance `default_timezone` from `local.php`.
- Added: regression tests for multiple IANA timezones and DST transitions so
  campaign scheduling cannot regress to a hard-coded +/- timezone offset.

## 0.9.35 - 2026-05-14

- Fixed: campaign trigger planning no longer starts campaigns merely because
  new campaign leads exist without event-log rows. Trigger workers now only run
  campaigns with due `campaign_lead_event_log` rows; rebuild workers remain
  responsible for creating missing event logs when date-based actions are due.
- Result: date-scheduled campaign actions are not fired early by the active
  MCD ring after a campaign is created or rebuilt.

## 0.9.34 - 2026-05-14

- Fixed: cluster offsite backup now detects stale temporary prepared MySQL
  processes from previous `mcd-offsite-mysql-*` runs, terminates them when their
  prepared datadir is already gone, and refuses to delete a prepared datadir
  that is still held by a live temp MySQL process.
- Result: deleted prepared offsite backup files cannot keep local backup disk
  space pinned and break the next local full backup with `No space left on
  device`.

## 0.9.33 - 2026-05-13

- Changed: cluster backup policy is deliberately simple and deterministic:
  start local full at the configured full window, assemble files after success,
  then start the Storage Box offsite stage immediately.
- Changed: cluster daytime incrementals can be scheduled hourly and now have a
  hard local free-space guard. By default, incrementals are skipped with a
  warning if the local backup filesystem has less than 300 GiB free.

## 0.9.32 - 2026-05-13

- Fixed: cluster local full backup scheduling is calendar-window based again.
  A late full from the previous day no longer pushes the next day's 01:00 full
  forward by the old 23-hour duplicate guard.
- Fixed: cluster offsite backup scheduler no longer skips a valid same-day
  local full just because the full completed after the daytime incremental
  window started. This prevents missing Storage Box backups after delayed full
  runs, incidents, or agent restarts.
## 0.9.31 - 2026-05-13

- Fixed: segment dependency follow-up no longer re-queues children for the
  whole "recently finished parent" window after the child has already been
  rebuilt after that parent. This prevents repeated empty dependent segment
  runs from starving unrelated due segments.

## 0.9.30 - 2026-05-13

- Fixed: newly discovered due segments are inserted at the front of segment
  rings instead of waiting at the tail of an already half-consumed cycle. This
  makes new or newly-due segments start on the next available segment slot when
  dependency rules allow it.

## 0.9.29 - 2026-05-12

- Fixed: segment rings now understand segment-to-segment dependencies from
  Mautic leadlist filters. When a base segment is rebuilt, dependent segments
  are queued as follow-up work and are blocked from launching in parallel with
  their parent segment.
- Result: changing a base segment no longer leaves include/exclude child
  segments with stale membership counts until a manual rebuild.

## 0.9.28 - 2026-05-12

- Fixed: `php_fpm` service-profile apply now writes the profile's
  `realpath_cache_size_kb` and `realpath_cache_ttl_sec` into the managed PHP
  ini override. Large Mautic web nodes no longer keep the PHP defaults
  (`4096K`/`120s`) after hardware tuning, which reduces path lookup pressure
  under high tracking traffic.

## 0.9.27 - 2026-05-12

- Fixed: Mautic image installs now generate nginx vhosts against the actual
  extracted web root. Composer/recommended-project images use `docroot` (or
  `public`) instead of exposing the project root.
- Hardened: image-created vhosts now deny dotfiles and project internals such
  as `.env`, `composer.lock`, `vendor`, `config`, `var`, and test/build files.

## 0.9.26 - 2026-05-12

- Fixed: `mcd-cli instance-delete --dry-run` no longer reports a synthetic
  `/etc/nginx/sites-enabled/<domain>.conf` path when that symlink/file does not
  exist. Delete planning now lists only real enabled vhost entries matched by
  exact file name or `server_name`, while preserving `sites-available` configs.

## 0.9.25 - 2026-05-12

- Safety: cluster-mode agents now refuse automatic DB-heavy service-profile
  components (`mysql`, `mautic_db_indexes`) unless an operator explicitly runs
  `mcd-cli service-profile apply --allow-cluster-db-maintenance` in a planned
  maintenance window.
- Result: standalone hosts still receive MySQL/index profile automation, while
  Galera/PXC clusters cannot be put into TOI/NBO DDL pressure by daemon
  auto-apply.

## 0.9.24 - 2026-05-12

- Fixed: cluster offsite backup success now persists `last_files_archive_path`
  and `last_files_bytes` in MCD state. Existing offsite directories also refresh
  those fields from their marker instead of leaving a stale archive path from an
  older backup.
- Added: `backup cluster-status` verifies the latest offsite file archive path
  from the final marker and reports stable `last_offsite_*` fields, preventing
  later local full/incremental jobs from making the offsite file archive marker
  look stale or healthy incorrectly.

## 0.9.23 - 2026-05-12

- Fixed: `page_hits_orphan_cleanup_*` runtime overrides are now persisted into
  `mcd.toml` together with other stable cleanup settings. This prevents orphan
  page-hit cleanup from being lost after runtime sync, daemon reload, or a
  cleanup deadlock/retry cycle.

## 0.9.22 - 2026-05-11

- Added: per-instance backup command `mcd-cli backup instance-run --root ...`
  that writes one restorable artifact: selected instance files, selected
  database dump, `mcc-backup-manifest.json`, and a storage-box index card.
- Changed: instance backup artifacts are image-compatible, so MCC can import
  them into the image catalog and restore them on another supported host with a
  newly generated database name such as `baza_<short_name>`.

## 0.9.21 - 2026-05-11

- Added: successful host and cluster offsite backups now write
  `mcc-backup-manifest.json` next to the backup and a root
  `mcc-backups-index.d/*.json` card on the storage box. MCC can list backups
  instantly without walking the whole storage tree.

## 0.9.20 - 2026-05-11

- Fixed: date-based campaign trigger detection now catches due event-log rows
  by `date_triggered IS NULL` + due `trigger_date`, even when Mautic does not
  keep `is_scheduled=1` in the expected shape.
- Fixed: due date-based campaign work can be recovered for up to seven days
  after `publish_down`. This prevents short campaign windows from being missed
  permanently when MCD only performed the early scheduling pass before the
  actual trigger time.
- Fixed: legacy explicit `campaigns_due`, `campaign_triggers_due`, and
  `campaign_rebuilds_due` SQL overrides with old trigger-date semantics are
  migrated back to the current safe defaults.

## 0.9.19 - 2026-05-11

- Fixed: successful cluster-channel self-update now finalizes the Galera-backed
  rollout coordinator for the local node. This prevents peers from waiting on a
  node that already installed the target version after a retry or coordinator
  deadlock.

## 0.9.18 - 2026-05-11

- Fixed: cluster local full scheduling now treats a recent successful full as
  already done even when the replica host timezone differs from the cluster
  business timezone. This prevents a daemon restart from launching a second
  same-day xtrabackup and pre-pruning the freshly completed full.

## 0.9.17 - 2026-05-11

- Fixed: cluster file-layer producer identity now includes local IP-derived MCC
  host ids such as `host-37-27-135-183`. This makes nodes whose local hostname
  differs from the MCC host id produce the expected Syncthing backup layer
  instead of being skipped as an unexpected node.
- Fixed: MCC state push change detection ignores volatile probe timestamps
  (`maintenance_state.checked_at_utc` and
  `mautic_install_readiness.checked_at_utc`) so agents do not push every
  dispatch tick when no real state changed.

## 0.9.16 - 2026-05-10

- Added: daemon-side automatic Mautic inventory rescan. By default MCD refreshes
  autodiscovered instances hourly and forces the next MCC state push when the
  inventory changes, so instances created from images become visible without a
  manual MCC rescan.
- Safety: automatic rescans preserve locally added manual inventory rows; the
  existing explicit `mcd-cli instances rescan` behavior is unchanged.
- Added: runtime overrides `inventory_auto_rescan_enabled` and
  `inventory_auto_rescan_interval_sec` for tuning or disabling this behavior.

## 0.9.15 - 2026-05-10

- Added: empty contacts cleanup now supports both interval scheduling and
  five-field cron-style scheduling.
- Fixed: legacy monthly cron cleanup such as `0 2 1 * *` is migrated as a cron
  schedule instead of being flattened to the default 900-second interval.
- Fixed: existing MCD-managed commented legacy cleanup cron lines can repair
  older runtime settings that were migrated before cron scheduling existed.

## 0.9.14 - 2026-05-10

- Fixed: empty contacts cleanup no longer exposes or executes the broad
  `email IS NULL OR mobile IS NULL` mode. When both conditions are selected,
  MCD uses the original safe predicate:
  `email IS NULL AND mobile IS NULL`.
- Safety: stale runtime settings saved with the temporary
  `email_or_mobile_null` mode are interpreted as `both_null`.

## 0.9.13 - 2026-05-10

- Added: `mautic_db_indexes` now also manages `leads(mobile)` globally for
  faster import/mobile lookups. If MySQL reports the index limit while adding a
  managed leads index, MCD safely drops existing fax-only/fax-named indexes and
  retries the managed index add.
- Added: per-instance empty contacts cleanup. MCD can periodically delete
  contacts with `email IS NULL`, `mobile IS NULL`, or both conditions selected
  as `email IS NULL AND mobile IS NULL` by MCC runtime settings. Legacy direct SQL crons for
  `email IS NULL AND mobile IS NULL` are commented on active profiles and
  migrated into MCD-owned scheduling.

## 0.9.12 - 2026-05-10

- Added: `mautic_db_indexes` now also manages `leads(email)` and
  `campaign_lead_event_log(is_scheduled, trigger_date, id)` for faster email
  lookups and scheduled campaign event scans.

## 0.9.11 - 2026-05-10

- Fixed: `mautic_db_indexes --dry-run` now reports the aggregate result as
  `planned` instead of `applied`, while still showing already-present indexes
  per instance.

## 0.9.10 - 2026-05-10

- Fixed: `mautic_db_indexes` connects to local Mautic databases through the
  MySQL unix socket when `local.php` uses `localhost`, matching the behavior of
  the `mysql` CLI and avoiding false TCP access-denied failures.

## 0.9.9 - 2026-05-10

- Added: built-in `mautic_db_indexes` service-profile component for Mautic DB
  indexes used by segment due/ring queries:
  `lead_lists_leads(leadlist_id, date_added, manually_removed, lead_id)`,
  `lead_lists_leads(leadlist_id, manually_removed, date_added, lead_id)`, and
  `audit_log(bundle, object, object_id, action, date_added)`. The apply path
  uses online DDL and defers instead of blocking when tables are busy.
- Fixed: MySQL state backend no longer treats `root` without a password as a
  valid runtime backend. If state MySQL credentials are missing or unsafe, MCD
  stays on sqlite and does not repeatedly attempt passwordless MySQL root.

## 0.9.8 - 2026-05-10

- Fixed: nginx baseline now normalizes legacy enabled site entries without a
  `.conf` suffix into `.conf` symlinks. This keeps vhosts loaded after the
  Ondrej/Sury nginx package is replaced by official `nginx.org` nginx, where
  MCD-managed nginx config includes only `/etc/nginx/sites-enabled/*.conf`.

## 0.9.7 - 2026-05-10

- Added: nginx baseline now installs a shared Mautic hardening snippet and
  includes it in active server blocks. The snippet denies direct access to
  project internals such as `config`, `vendor`, `node_modules`, `.git`,
  dependency lockfiles and build/test metadata, protecting both zip/root and
  composer/docroot installations.
- Added: nginx baseline safely supplements the existing security headers
  snippet with `X-Frame-Options`, `Strict-Transport-Security`, and
  `Permissions-Policy` when they are missing, so PHP/static locations that
  include `security-headers.conf` do not bypass server-level hardening.
- Fixed: hardening include insertion is idempotent per `server {}` block, so
  multi-vhost files with one already-hardened server are completed without
  duplicating includes.

## 0.9.5 - 2026-05-10

- Fixed: cluster non-authority nodes no longer run standalone backup storage
  probes. In cluster backup mode only the configured backup authority probes
  Storage Box connectivity, so PXC/web nodes do not log false
  `backup storage is not configured` warnings when shared cluster backup
  settings are present.

## 0.9.4 - 2026-05-09

- Fixed: `mcd-cli instance-delete --delete-vhost` now disables only the selected
  instance vhost by exact `server_name`/domain match in `sites-enabled`.
- Safety: vhost deletion no longer matches by application root alone and no
  longer deletes `sites-available` configs; enabled symlinks are removed while
  available configs are preserved for recovery/audit. Legacy regular files in
  `sites-enabled` are copied to `sites-available` before being disabled.

## 0.9.3 - 2026-05-09

- Fixed: `mcd-cli instance-delete` now drops the selected database using the
  instance credentials from `config/local.php` instead of trying local MySQL
  root access without a password. This keeps delete working on hosts where
  root socket/passwordless access is disabled.

## 0.9.2 - 2026-05-09

- Added: `mcd-cli instance-delete` safely removes selected Mautic instance
  artifacts from the local host: application folder, nginx vhost and local
  database can be selected independently.
- Safety: deletion requires `--yes`, validates roots under `/var/www`, refuses
  non-local database hosts, supports `--dry-run --json`, and rescans/pushes
  state after successful host-side cleanup.

## 0.9.1 - 2026-05-08

- Fixed: install-readiness now reconciles old MCD IPv6-disable intent with
  currently visible runtime interfaces before reporting compatibility. Hosts
  where previous MCD versions wrote `/etc/sysctl.d/99-disable-ipv6.conf` but
  left interfaces such as `eth0` enabled are now brought to the intended
  disabled runtime state automatically on the next state push.

## 0.9.0 - 2026-05-08

- Added: host install-readiness snapshot in the regular MCC state push. MCD now
  reports IPv6 disabled state, nginx/PHP-FPM/Composer/Node/npm/certbot presence,
  and local MySQL/MariaDB engine/version for per-Mautic-major compatibility.
- Added: `mcd-cli mautic-image install` to install a composer-based Mautic
  instance from an MCC image artifact with preflight guards for existing
  webroot, nginx vhost, symlink and database artifacts.
- Added: image install flow downloads MCC-managed files/database artifacts,
  creates `baza_<short-domain>`, patches `config/local.php`, writes a port-80
  nginx vhost, runs certbot, rescans instances and pushes fresh state to MCC.

## 0.8.196 - 2026-05-08

- Fixed: cluster rolling self-update no longer treats nodes that already
  completed install as stale health blockers. This keeps alias/backward
  compatibility rows from blocking the real rolling update.

## 0.8.195 - 2026-05-08

- Fixed: cluster rolling self-update now canonicalizes the local node identity
  to the MCC host key from the ordered cache route when `cluster_node_index` is
  configured. This prevents duplicate rollout participants like `ananas-xxl`
  plus `host-37-...` for the same machine and avoids download/install deadlocks.

## 0.8.194 - 2026-05-08

- Fixed: self-update apply now accepts MCC `update_available` decisions as an
  executable update plan when `target` and `package_url` are present. This fixes
  old agents that can see a channel update but refuse to apply it with
  `no update plan (status=update_available)`, including cluster-channel nodes.

## 0.8.193 - 2026-05-08

- Changed: task history is now treated as a short live operational slice rather
  than a long archive: defaults are 2 days and 25k non-running rows, compacted
  hourly inside the quiet window.
- Added: sqlite task compaction archives rows to compressed JSONL before removal
  and prunes those postmortem archives after 14 days. Live scheduler logic never
  depends on archived rows.
- Fixed: segment due detection now treats recent Mautic audit-log
  `lead/segment` create/update events as rebuild triggers. This covers Mautic
  saves that change segment filters but leave `lead_lists.date_modified`
  unchanged, so edited segments do not silently miss the next MCD ring.
- Fixed: campaign trigger due detection now ignores already-triggered scheduled
  event rows (`date_triggered IS NOT NULL`). Published campaigns no longer
  re-enter the trigger ring only because old scheduled event rows still exist.

## 0.8.192 - 2026-05-08

- Changed: default MCD task-history retention is now bounded for small and test
  hosts: 7 days and 50k non-running task rows instead of 14 days and 200k rows.
  This prevents long-lived segment/campaign/import rings from growing SQLite
  state into a large local history table while preserving enough evidence for
  daily diagnostics.

## 0.8.191 - 2026-05-08

- Changed: release-only rebuild after the 0.8.190 public artifact was found stale
  behind Cloudflare cache. Code is identical to 0.8.190 plus the version bump so
  cluster nodes pull a fresh uncached package URL.

## 0.8.190 - 2026-05-08

- Fixed: empty remote runtime overrides can no longer erase a configured local
  `state_mysql_unix_socket` in the daemon process. This keeps cluster nodes on
  the Galera-backed MCD state backend instead of falling back to sqlite because
  PyMySQL tried the wrong localhost socket.
- Fixed: cluster DB state collection now honors the configured state MySQL
  socket explicitly instead of relying only on auto-detection.

## 0.8.189 - 2026-05-08

- Fixed: release packaging excludes old nested `mcd-agent-*.tar.*` artifacts
  from the agent source tree so cluster channel packages stay small and clean.

## 0.8.188 - 2026-05-08

- Changed: MCC large-host MySQL defaults now keep the safe 600-connection
  envelope, reduce table caches to the measured cluster-safe range, and keep
  I/O/open-file limits high enough for the hardware without reintroducing the
  previous connection storm profile.
- Changed: PXC/Galera cluster-safe MySQL profile uses a 96 GB buffer pool on
  256 GB web+DB nodes while preserving safe connection and table-cache limits.

## 0.8.187 - 2026-05-08

- Fixed: MySQL service-profile auto-apply now detects Galera/PXC cluster
  nodes and refuses to apply standalone MySQL tuning unless the profile is
  explicitly marked cluster-safe.
- Fixed: cluster-safe MySQL profiles are clamped to the proven PXC-safe
  envelope before writing drop-ins or issuing dynamic `SET GLOBAL`, preventing
  MCC profile drift from pushing aggressive standalone values into a cluster.
- Fixed: Galera/PXC MySQL profile application no longer rewrites top-level
  `/etc/mysql/my.cnf` or `/etc/mysql/mysql.cnf`; cluster nodes are managed only
  through the explicit MCD drop-in.
- Fixed: MCC resolves cluster web/Galera nodes to a dedicated conservative
  PXC profile instead of the hardware standalone MySQL profile.

## 0.8.186 - 2026-05-08

- Fixed: SQL technical segment rebuilds now apply a per-session DB statement
  timeout (`segment_sql_statement_timeout_sec`, default 1800s) before running
  the expensive temp-table population query.
- Fixed: DB watchdog now has a built-in safety rule for stale MCD-owned
  `mcd_tmp_segment_leads` rebuild queries, so orphaned temp segment SQL cannot
  keep `page_hits` inserts locked after an agent restart or broken connection.
- Result: a pathological SQL segment cannot block web tracking writes and
  saturate PHP-FPM indefinitely, as observed on Benu.

## 0.8.185 - 2026-05-08

- Fixed: cluster self-update quorum no longer includes the backup/replica
  route. Replica nodes can intentionally run sqlite/read-only control state,
  so they must not block the Galera-backed rolling update coordinator.
- Result: cluster channel updates are coordinated only across writable
  web/Galera MCD nodes: all download first, then install one by one.

## 0.8.184 - 2026-05-08

- Fixed: cluster file-layer producer now prunes stale `.incomplete-*`
  Syncthing layer directories with an age guard, preventing replica backup
  storage from filling with abandoned temporary snapshots.
- Result: cluster backup file layers remain atomic (`current` is still swapped
  only after a completed layer), while old failed temp directories are cleaned
  by normal MCD runs instead of manual maintenance.

## 0.8.183 - 2026-05-07

- Fixed: Mautic upgrade flow now clears `var/cache/prod` before Composer
  post-update scripts, preventing stale compiled container state during core
  upgrades.
- Fixed: after Doctrine migrations run, MCD verifies
  `doctrine:migrations:up-to-date` and reconciles metadata when schema changes
  are already applied but migration rows are not marked executed.
- Fixed: if Doctrine stops on a migration whose table, column or key already
  exists, MCD now marks that exact migration version as executed and continues
  the migration run instead of aborting the whole Mautic upgrade.
- Fixed: upgrade reconcile removes stale unavailable migration metadata records
  after pending migrations are marked, so otherwise healthy schemas do not stay
  formally out-of-date.

## 0.8.182 - 2026-05-07

- Fixed: passive-profile daemon no longer logs the passive planning notice on
  every loop. The notice is rate-limited while manual request dispatch remains
  active.
- Result: passive hosts still accept manual commands and run MCC config sync,
  but logs no longer look like a scheduler storm after self-update.

## 0.8.181 - 2026-05-07

- Fixed: passive-profile planning no longer spins every loop when
  `poll_interval_sec` is very low. The passive manual-dispatch check now leaves
  regular cycles for MCC state push, config guard and self-update.
- Result: passive hosts can actually reach the new MCC config drift guard and
  self-heal local config after desired state changes.

## 0.8.180 - 2026-05-07

- Added: MCC config guard now treats desired-vs-local config SHA mismatch as
  drift, not only profile mismatch.
- Changed: `[mcc].profile_guard_enabled` defaults to enabled for new configs,
  so registered agents can recover their local config from MCC desired state.
- Changed: legacy managed configs with boolean `profile_guard_enabled = false`
  no longer hard-disable the guard when MCC URL/token are present; use string
  `"off"` only for intentional disable.
- Result: if an MCC-managed host loses or rolls back `/opt/mcd/etc/mcd.toml`,
  the daemon restores the current MCC desired config automatically after the
  state/config sync loop sees the mismatch.

## 0.8.179 - 2026-05-07

- Fixed: passive-profile daemon loops now advance the plan-refresh timer before
  sleeping, so passive hosts continue to reach periodic MCC state push.
- Result: locally successful backups on passive hosts are reported back to MCC
  instead of leaving central backup age stale after an MCD restart/update.

## 0.8.178 - 2026-05-07

- Fixed: `db_watchdog` can now execute explicitly configured `kill_query` and
  `kill_connection` actions when `observe_only=false`, instead of only
  reporting matching rules.
- Safety: destructive watchdog actions still require an explicit matching rule,
  a valid MySQL process id and a live `Query`; default behavior remains
  observe-only.
- Result: hosts can automatically interrupt known pathological long-running
  read-only report queries, such as stale `page_hits` analytics scans, without
  manual MySQL intervention.

## 0.8.177 - 2026-05-06

- Added: `mcd-cli shortner` / `mcd-cli shortener` operations for self-hosted YOURLS.
  - `detect` scans local nginx/web roots and reports YOURLS roots, site URL, version and active nginx binding.
  - `version` and `check-update` read installed version and compare it with the latest upstream GitHub release.
  - `update --yes` downloads the target upstream release, creates a local backup under `/var/backups/mcd/shortners`, and updates YOURLS core files while preserving `user/` config/plugins/custom data.

## 0.8.176 - 2026-05-06

- Fixed: `mcd-cli env ipv6 status` now includes all currently visible
  `/proc/sys/net/ipv6/conf/*/disable_ipv6` interfaces instead of only
  `all/default/lo`.
- Fixed: `mcd-cli env ipv6 disable|enable`, including the interactive menu,
  applies the runtime value to every current interface as well as the
  persistent `all/default/lo` sysctl file.
- Result: MCD no longer reports IPv6 disabled while an existing interface
  such as `eth0` remains enabled and can still affect `apt`.

## 0.8.175 - 2026-05-06

- Fixed: template-clone detection now treats `/opt/mcd/var/template_identity.json`
  as authoritative even when the cloned local config no longer has
  `host_template=true`.
- Fixed: clone startup uses the existing inventory rescan API instead of a
  missing helper, so clone discovery no longer falls back to stale donor cache.
- Result: hosts cloned from template images auto-promote under their local
  hostname, rescan fresh instances, and auto-register in MCC instead of keeping
  donor identity artifacts.

## 0.8.174 - 2026-05-05

- Fixed: daemon-side backup storage probes now skip while a host or cluster backup lock is active, so MCD does not mount/probe Storage Box in parallel with an active backup.
- Fixed: cluster backup scheduler now treats existing backup locks, including manually started offsite jobs that survived daemon restart, as busy state before scheduling full/offsite/incremental jobs.
- Fixed: cluster offsite backup failures now store compact error text in state/history, preventing huge `mydumper` stderr from breaking MCC state push with `HTTP 413`.
- Fixed: cluster backup state compacts existing history on every update, so old pre-fix oversized failure entries are pruned from future state payloads.
- Fixed: final cluster offsite markers now rewrite `files_archive_path` after the `.incomplete-*` directory is atomically renamed to the dated backup directory.
- Result: restarting MCD during a cluster offsite backup no longer creates duplicate sshfs mounts or duplicate cluster backup attempts.

## 0.8.173 - 2026-05-05

- Changed: cluster offsite backups now use the completed local `xtrabackup` full as the default database source.
- Added: MCD prepares a reflink clone of `current-full`, starts a temporary same-version local read-only MySQL over that clone, and runs `mydumper` from the static snapshot directly to the Storage Box.
- Changed: cluster offsite `mydumper` defaults to at least 16 threads and `--rows=500000` when no explicit chunking is configured, so large Mautic tracking tables are dumped in parallel instead of one giant single-threaded file.
- Result: cluster offsite dumps are no longer broken by live replica DDL such as `Table definition has changed`, without requiring a second full local database copy.

## 0.8.172 - 2026-05-05

- Fixed: import polling now treats Mautic numeric status `7` (`DELAYED`) and string `delayed` as pending work.
- Fixed: default and guard SQL for `sql.import_pending_count` now require a published, unfinished import, so delayed batch imports continue without counting completed imports forever.
- Result: large CSV imports that pause after the first batch are picked up by MCD automatically instead of requiring manual `mautic:import`.

## 0.8.171 - 2026-05-05

- Changed: MCC runtime override polling is enabled by default when `[mcc].runtime_overrides_poll_enabled` is not explicitly set, so agents pull hardware-profile runtime changes without MCC push or service restart.

## 0.8.170 - 2026-05-05

- Fixed: automatic SQL segment detection now validates lead-field filters against the actual `leads` table before moving a segment into the SQL ring, preventing invalid auto-rules for legacy/missing fields.
- Changed: auto-generated page-hit SQL segment rules now drive candidate selection from `page_hits` and join back to leads, avoiding correlated per-lead page-hit probes on very large `page_hits` tables.

## 0.8.169 - 2026-05-04

- Added: SendGrid/SNS plugin install now runs a dependency preflight before applying the plugin: installs Composer globally if missing, verifies Composer as `www-data`, ensures `symfony/sendgrid-mailer:*`, and normalizes NodeJS/npm to Node 20.
- Fixed: Composer package detection now checks package names without version constraints, so `symfony/sendgrid-mailer:*` is not repeatedly reinstalled when already present.

## 0.8.168 - 2026-05-04

- Fixed: automatic SQL segment detection now supports negative lead text filters (`!like`/`!contains`), allowing mixed page-hit segments such as “visited page in last N days and did not buy category” to move out of the standard Mautic segment ring.

## 0.8.167 - 2026-05-04

- Fixed: MySQL service-profile apply now reports `partial`/`deferred` when durable config is written but runtime `SET GLOBAL` cannot be applied, instead of masking the condition as fully applied.

## 0.8.166 - 2026-05-04

- Fixed: MySQL service-profile dynamic apply now prefers local root socket auth before `debian.cnf`, so runtime `SET GLOBAL` tuning works on hosts where `debian.cnf` lacks `SYSTEM_VARIABLES_ADMIN`/`SUPER`.

## 0.8.165 - 2026-05-04

- Fixed: SQL-driven segment rebuilds now update only metadata columns present in the target Mautic schema, supporting Mautic 4 `last_built_date` without requiring `last_built_time`.
- Fixed: MySQL service-profile apply now disables only duplicate top-level MySQL keys owned by MCD drop-ins, so old copied `my.cnf` values cannot override current hardware profiles after restart while unrelated local settings remain intact.

## 0.8.164 - 2026-05-04

- Fixed: MySQL service-profile apply now removes only legacy MCD-managed top-level MySQL tuning files so profile drop-ins have deterministic precedence.
- Added: MySQL service-profile apply now pushes dynamic runtime variables immediately and avoids surprise MySQL restarts; static settings remain durable for the next planned restart.
- Fixed: campaign rebuild due SQL no longer treats manually removed/exited campaign contacts as missing, preventing empty `campaigns:rebuild` loops.
- Changed: periodic all-published segment full scans are disabled by default; due-segment SQL and import-triggered full scans still prevent missed work.

## 0.8.163 - 2026-05-04

- Fixed: stale import-pending override warnings are now rate-limited per instance to avoid poll-cycle log spam while still exposing the bad profile SQL.

## 0.8.162 - 2026-05-04

- Fixed: import pending detection now cross-checks profile/runtime SQL with the status-based Mautic import state. A stale profile query can no longer treat completed/failed historical imports as pending and relaunch empty imports forever.

## 0.8.161 - 2026-05-04

- Fixed: import settle/backoff now clears stale pending-import cache in both planning and dispatch paths. This prevents the lower dispatch loop from launching another no-op import while settle is active.

## 0.8.160 - 2026-05-04

- Fixed: import settle/backoff is now also applied when a short-lived `mautic:import` child has already disappeared before the daemon can collect its return code.

## 0.8.159 - 2026-05-04

- Fixed: near-instant successful `mautic:import` runs now use an extended settle backoff, preventing no-op imports from relaunching every import poll interval while still allowing real long-running imports to continue in normal batches.

## 0.8.158 - 2026-05-04

- Fixed: successful `mautic:import` runs now create a short settle window that clears stale import-pending cache before the next database poll. This prevents MCD from repeatedly re-launching import after it has already drained the queue.

## 0.8.157 - 2026-05-04

- Fixed: segment and import ring launches now have daemon-side cooldown guards. Short-lived `mautic:segments:update` or `mautic:import` commands can no longer be respawned every dispatch tick while planning cache still contains the same due item.

## 0.8.156 - 2026-05-04

- Fixed: cluster offsite backup now archives the prepared file snapshot as a single `files-snapshot-<ts>.tar.gz` directly into the remote incomplete backup before `mydumper`, avoiding the previous slow `rsync` file stage after the database dump.
- Fixed: cluster offsite process detection now treats both database dump and file archive child processes as active backups across MCD restarts.
- Safety: cluster local xtrabackup now rejects local backup roots inside the MySQL datadir, including bind-mount aliases, and can pre-prune the operational local backup chain before a new full when disk space is intentionally tight.

## 0.8.155 - 2026-05-03

- Fixed: cluster file backup defaults now keep per-node layers host-specific (`/etc`, cron spool, MCD config/state, helper scripts, Syncthing identity/config and Gluster state) and store the Mautic application tree once as the shared layer.
- Added: cluster file layer source paths now support simple glob patterns such as `/root/*.sh`, allowing small root helper scripts to be captured without backing up large root dumps.
- Fixed: cluster offsite scheduler now treats daytime local full runs as manual/test runs and does not immediately trigger offsite after them; offsite waits for the nightly full chain.
- Added: `mcd-cli backup cluster-offsite-dry-run` checks authority, tools, local full, latest file snapshot and Storage Box write access without starting `mydumper`.

## 0.8.154 - 2026-05-03

- Fixed: cluster offsite backup now treats a live matching `mydumper` process as an active backup even if MCD was restarted and the original flock was released, preventing duplicate offsite dumps during agent upgrades/restarts.

## 0.8.153 - 2026-05-03

- Fixed: cluster auto-update no longer blocks the download phase on recent campaign activity; active campaign trigger/rebuild work remains an install/apply blocker.

## 0.8.152 - 2026-05-03

- Fixed: cluster offsite backup now attempts to assemble the latest local files snapshot when the snapshot is missing, instead of failing immediately.
- Fixed: cluster backup scheduler now marks local full/offsite days only after successful completion; failed attempts use short cooldowns and can retry in the same window.

## 0.8.151 - 2026-05-03

- Added: cluster file backup now uses a Syncthing data plane. Every expected cluster node prepares its own local file layer, the replica assembles received layers into `files/snapshots/latest`, and offsite backup uses that prepared snapshot.
- Added: `mcd-cli backup cluster-files-produce` and `cluster-files-assemble` for explicit producer/replica checks.
- Added: daemon-side cluster file layer producer loop; passive mode still does not run rings, but can keep cluster backup file layers fresh when cluster backup is enabled.
- Added: runtime-config keys for Syncthing file transport, expected producer nodes, shared-file producer, layer freshness and producer interval.

## 0.8.150 - 2026-05-03

- Changed: self-update blockers now include only active PHP campaign trigger/rebuild work, including `mautic:campaigns:update` because MCD treats it as rebuild. Segment updates and wrapper processes (`flock`, `timeout`, `sudo`) waiting on locks do not block agent replacement.

## 0.8.149 - 2026-05-03

- Added: cluster-channel self-update coordination through the shared Galera state. Cluster nodes download the new package one by one, wait until every expected node has the package locally, then install one by one only after cluster update health is clear.
- Safety: clustered self-updates no longer defer the download phase because of local campaign/backup activity; those blockers apply to the install phase so all nodes can stage the package without touching the running agent.
- Changed: cluster-channel agents query MCC as a release catalog only and do not acquire MCC per-host update sessions; the shared cluster coordinator owns sequencing.
- Safety: if the shared cluster coordinator is unavailable, cluster-channel agents defer updates instead of falling back to uncoordinated local installs.
- Safety: cluster update state uses the existing `runtime_sync` table, avoiding any daemon-side DDL or schema bump on Galera.

## 0.8.148 - 2026-05-03

- Fixed: passive-profile daemon loop now dispatches manual cluster-routed requests before any automatic planning, preventing route variables from being referenced before initialization and keeping passive mode non-planning.

## 0.8.147 - 2026-05-03

- Fixed: cluster-routed manual requests now match all local node aliases (`mcc_host_name`, local hostname, observed/effective hostnames) instead of only the internal state node id, so commands queued to MCC host names are consumed by the correct daemon.

## 0.8.146 - 2026-05-03

- Fixed: cluster backup is now guarded by cluster authority and can run only on the configured authority node, defaulting to the replica role.
- Added: autonomous cluster identity/authority config support through `[cluster]` and `[backup.cluster]`, so shared Galera-backed MCD config works without MCC.
- Added: autonomous cluster command routing through shared state: segments/campaigns route to the configured cron node, imports to the import node, backups to the backup node, and cache operations fan out to configured cache nodes even when invoked from any cluster node CLI.
- Changed: passive profile still accepts explicit manual/cluster-routed requests, while automatic ring planning stays disabled.
- Changed: `cluster-status` reports authority status and suppression reasons.

## 0.8.145 - 2026-05-03

- Added: cluster backup commands for replica-based two-layer backups: local xtrabackup full, local xtrabackup incremental, local hardlink file snapshot, and remote offsite mydumper.
- Added: daemon scheduling for cluster backups: daily local full, offsite after successful full and not before configured time, and daytime local incrementals.
- Safety: cluster remote retention now produces an explicit plan and refuses deletion when unknown or unmarked directories are found; manual/protected archives are never deleted.

## 0.8.144 - 2026-05-03

- Added: xtrabackup physical backups now support incremental chains.
- Changed: xtrabackup defaults are weekly full backups plus daily incrementals.
- Added: xtrabackup retention keeps 3 full backup chains and prunes incrementals older than 7 days.
- Added: when estimated storage space is insufficient, xtrabackup can free space by deleting the oldest full backup chain before starting the next run.
- Compatibility: legacy full xtrabackup directories are recognized from `xtrabackup_checkpoints` even when old markers do not contain chain metadata.

## 0.8.143 - 2026-05-02

- Added: backup method selection with `mydumper` logical dumps and `xtrabackup` physical backups.
- Added: backup preflight/dry-run command validates tools, MySQL connectivity, xtrabackup version, and storage write access without creating a real backup.
- Added: scheduled backups support exact `quiet_minute`, enabling starts such as `02:30` instead of hour-only starts.
- Added: MCD can install missing backup packages (`sshfs`, `mydumper`, `percona-xtrabackup-80`) when backup auto-install is enabled.

## 0.8.142 - 2026-05-01

- Fixed: Mautic version probes no longer run `bin/console --version` as root; console probes run as the configured Mautic runtime user (`www-data` by default).
- Changed: regular state pushes prefer the cached `.mcd/mautic.version` file and do not start Mautic console on every push.
- Safety: forced version-cache refreshes still use Mautic's own detector, but execute under the runtime user to avoid root-owned Symfony/Mautic cache files.

## 0.8.141 - 2026-05-01

- Fixed: composer-based Mautic upgrades now run an idempotent preflight that installs Composer and Node.js 20 only when missing or too old.
- Fixed: the www-data Composer cache directory is created before `composer update`, avoiding false upgrade failures on fresh migrated hosts.
- Fixed: composer upgrade post-migration command now uses the available Doctrine migrations command, with fallback for older command names.

## 0.8.140 - 2026-05-01

- Added: nginx runtime baseline convergence for the official nginx profile and `apt-upgrade` post-check.
- Added: MCD now enforces `user www-data;`, loads `/etc/nginx/sites-enabled/*.conf` when missing, and converts direct files in `sites-enabled` into symlinks backed by `sites-available`.
- Safety: nginx baseline changes are validated with `nginx -t`; changed files are rolled back if validation fails.

## 0.8.139 - 2026-04-30

- Added: daemon-side Zabbix `mautic.version` cache guard. Hosts with Zabbix Agent2 now periodically enforce the cached UserParameter helper and refresh `.mcd/mautic.version` for discovered instances, preventing old `bin/console --version` checks from returning after config/profile reapplies.

## 0.8.138 - 2026-04-28

- Added: `mcd-cli apt-upgrade` now runs a host service safety post-check after package upgrades.
- Added: after `apt-get dist-upgrade -y`, the host-side APT flow ensures `nginx`, `mysql`/`mariadb`, `cron`, and detected `php-fpm` services are enabled for autostart and active again if package changes left them stopped.
- Added: `nginx` receives extra validation via `nginx -t` after the service recovery step.
- Added: MCC web APT jobs now expose these service post-check steps in the queue output because the web button executes `mcd-cli apt-upgrade --yes` through MCD.

## 0.8.137 - 2026-04-28

- Added: Mautic upgrade post-check now runs after `mautic:update:apply` and validates runtime ownership/permissions again before declaring success.
- Added: post-upgrade verification now checks `nginx` activity, auto-starts it if it stayed stopped after package or system changes, validates `nginx -t`, and confirms at least one `php-fpm` service is active.
- Added: local origin HTTPS probe on the upgraded instance domain via `127.0.0.1` plus external HTTPS probe via the public domain, so MCC/MCD upgrade jobs fail fast if the web stack does not come back.

## 0.8.136 - 2026-04-27

- Treat old `ondrej/nginx` `.mcd-disabled-*` files as unsatisfied nginx repo profile state, so the profile cleanup runs instead of being skipped by `apply_once`.

## 0.8.135 - 2026-04-27

- Removed dead `ppa:ondrej/nginx` source files instead of renaming them to `.mcd-disabled-*`.
- Clean up old `ondrej/nginx` `.mcd-disabled-*` leftovers from `/etc/apt/sources.list.d` so `apt update` does not warn about invalid filename extensions.

## 0.8.134 - 2026-04-27

- Hardened APT repo profile convergence:
  - active `.list`/`.sources` files are now checked separately from disabled backups;
  - the official nginx profile no longer skips `apply_once` when an active Ondrej nginx source is still present;
  - this prevents stale `ppa:ondrej/nginx` 404 sources from surviving after the profile was marked applied.
- Old MCC runtime overrides with `service_profiles_components=["php_fpm","mysql"]` are migrated in-agent to include `apt`, so legacy host overrides do not block apt profile auto-apply.

## 0.8.133 - 2026-04-27

- Superseded by `0.8.134` before approval.

## 0.8.132 - 2026-04-27

- Enabled MCC-pulled service profiles by default:
  - daemon auto-applies `php_fpm`, `mysql`, and `apt` profiles on the normal poll loop;
  - hosts now pull dynamic stack parameters from MCC instead of requiring explicit MCC push/apply;
  - apt repo profile markers now include a profile hash, so one-time repo setup re-runs when MCC changes the profile.

## 0.8.131 - 2026-04-27

- Added official stable `nginx.org` APT repo profile:
  - disables the legacy Ondrej nginx PPA source when enabled;
  - installs `/usr/share/keyrings/nginx-archive-keyring.gpg`;
  - verifies fingerprint `573BFD6B3D8FBC641079A6ABABF5BD827BD9BF62`;
  - writes `/etc/apt/sources.list.d/nginx.list` for the host Ubuntu codename.
- The legacy Ondrej PHP profile remains independent; only nginx is moved to official stable packages.

## 0.8.130 - 2026-04-27

- Added cluster asset guard for Mautic `plugins` and `app/bundles` only:
  - `mcd-cli cluster-assets status|guard|fix-perms|reload` computes deterministic content digests, checks permissions, and reports Syncthing conflict files.
  - Daemon guard can repair owner/mode drift and run cache clear/warmup plus PHP-FPM reload when plugin/bundle content changes.
  - Agent state push reports asset digests to MCC so cluster nodes can be compared for plugin/bundle drift.

## 0.8.129 - 2026-04-26

- Fixed Mautic version cache source of truth:
  - Cache refresh now uses the same MCD version detector as `mautic-upgrade check`, which is what MCC already uses for the displayed instance version.
  - Zabbix still reads only `<instance-root>/.mcd/mautic.version`; `bin/console` is not run from the Zabbix UserParameter.
  - This prevents stale package metadata such as `composer.lock` from downgrading the cached value when the running Mautic version is newer.

## 0.8.128 - 2026-04-26

- Added lightweight Mautic version cache for Zabbix:
  - MCD state push now writes each instance version to `<instance-root>/.mcd/mautic.version`.
  - Version discovery is performed by MCD and cached for monitoring.
  - `mcd-cli zabbix install-mautic-version-cache` installs a `mautic.version[*]` UserParameter that reads the cache file without starting PHP/Mautic.
  - `mcd-cli zabbix refresh-mautic-version-cache` refreshes all discovered instance caches on demand.

## 0.8.127 - 2026-04-26

- Added built-in Viber stats scheduling for instances with a Viber plugin installed.
  - Active profiles run `viber:stats:update` every 10 minutes by default.
  - MCC/runtime overrides can enable/disable it and change the interval per instance.
  - Existing cron lines for `viber:stats:update` are commented while MCD manages tasks and restored when the host returns to `passive`.

## 0.8.126 - 2026-04-26

- Added unlimited campaign trigger mode:
  - `runtime.campaign_limit = 0` / `off` / `unlimited` now omits `--campaign-limit`.
  - Old persisted command templates containing `--campaign-limit={campaign_limit}` are also stripped when the limit is disabled.
- Added self-update guard:
  - automatic and manual MCD self-update is deferred while campaign trigger/rebuild/update console jobs are running.
  - daemon auto-update also waits through a short post-campaign cooldown so updates do not slip between campaign batch passes.

## 0.8.125 - 2026-04-26

- Fixed legacy MCC-persisted `campaigns_due` migration for campaign trigger rings.
  - Hosts that stored the generated trigger SQL without the old `date_triggered IS NULL` clause are now still recognized as legacy defaults.
  - This lets package defaults replace stale local SQL and apply the UTC scheduled trigger fix without manual config surgery.

## 0.8.124 - 2026-04-26

- Fixed campaign trigger ring timing for scheduled/date-based Mautic campaigns.
  - Mautic stores campaign event/log `trigger_date` in UTC, while publish windows remain local to the instance.
  - MCD now compares scheduled campaign event `trigger_date` against `now_utc` so future campaigns such as ananasmk `621` enter the trigger ring at the intended local send time instead of up to the timezone offset early.

## 0.8.123 - 2026-04-25

- Made `mysql_hybrid` state backend safe for PXC/Galera cluster runtime.
  - Normal daemon/status paths now only validate that the MySQL state database and schema already exist.
  - Runtime paths no longer create databases, create tables, or run table migrations.
  - Schema creation/migration is restricted to the explicit `mcd-cli state-db init` install/bootstrap command.
  - The runtime MySQL user is granted DML-only permissions instead of DDL permissions.
  - Bootstrap revokes existing runtime user grants before re-granting DML-only permissions, so old broad grants are not inherited.
  - Added `mcd_schema_version` validation so old or partial schemas fall back to legacy SQLite mode instead of being modified by the daemon.

## 0.8.122 - 2026-04-25

- Migrated old MCC-generated `campaigns_due` SQL variants that still contained `campaign_lead_event_log.date_triggered IS NULL`.
  - Hosts with an already-persisted legacy SQL now switch back to the current campaign trigger default during config load.
  - This fixes scheduled Mautic 4 campaign triggers on hosts such as ananasmk where the stale SQL was stored in `/opt/mcd/etc/mcd.toml`.

## 0.8.121 - 2026-04-25

- Fixed campaign trigger due detection for scheduled/date-based Mautic 4 campaigns.
  - MCD now treats `campaign_lead_event_log.is_scheduled = 1` plus `trigger_date <= now` as pending work.
  - It no longer requires `date_triggered IS NULL`, because Mautic 4 fills `date_triggered` when the future event is scheduled, which made campaigns like ananasmk `622` invisible at the actual trigger time.

## 0.8.120 - 2026-04-25

- Kept UI/CLI manual segment, campaign rebuild, and campaign trigger launches inside the MCD manual request queue for active profiles.
  - If the daemon does not pick up the request immediately, `mcd-cli` now leaves the request queued instead of falling back to a direct console run.
  - This preserves scheduler slot accounting: manual UI launches can temporarily overfill slots, and normal ring dispatch resumes only after running work drops back under configured limits.

## 0.8.119 - 2026-04-25

- Moved stale Mautic lock cleanup to a lightweight all-day hourly schedule by default.
  - Default `mautic_lock_cleanup_interval_sec` remains `3600`, but the default quiet window is now effectively always open (`00:00–24:00`).
  - Backup guard remains active, so cleanup still backs off while backup is running or its protected pre-window is active.
  - This keeps old `checked_out` segment/campaign locks from accumulating during the day without turning the task into a heavy maintenance job.
- Tightened external console task detection.
  - Child `php` processes of already tracked MCD jobs are no longer counted as external manual work.
  - This keeps scheduler slot accounting correct on busy hosts and preserves the intended “manual jobs occupy real slots” behavior only for jobs truly started outside MCD.

## 0.8.117 - 2026-04-25

- Counted real manual Mautic console jobs in the same concurrency budget as MCD-managed jobs.
  - The daemon now observes live external `mautic:segments:update`, `mautic:campaigns:rebuild/update`, and `mautic:campaigns:trigger` / `mautic:campaign:trigger` console processes on managed instance roots.
  - These external jobs now occupy the same segment / rebuild / trigger slots as native MCD launches, so MCD no longer over-dispatches when someone starts a real console job by hand.
  - Externally started entity-specific jobs are also marked as executed in the current ring pass, so the same ID is not immediately relaunched by MCD in that cycle.
  - Wrapper processes like `sudo`/`timeout` are ignored, so one real manual console launch consumes one slot instead of being double-counted.
- Operator visibility:
  - `mcd-cli scheduler status` now reports both tracked and external running tasks.
  - maintenance status/kill logic now treats these observed external Mautic console jobs as managed work rather than orphan processes.

## 0.8.113 - 2026-04-23

- Added install type to `mcd-cli instances list`.
  - Instance rows now include `install_type=composer|zip`.
  - This lets MCC persist and display install type in instance properties without waiting for a later full state push.

## 0.8.112 - 2026-04-23

- Fixed `mcd-cli instances rescan` crashing in inventory persistence.
  - Added the missing `json` import in `inventory.py` so autodiscovered instances with domains are saved correctly during rescan.
  - This restores real agent-side inventory rescans used by MCC when new instances are added to an existing host.

## 0.8.111 - 2026-04-22

- Reworked HostnetAuth MFA helper to avoid console security token dependencies on Mautic 6/7.
  - The helper now loads the HostnetAuth integration object and retargets its per-user fields via reflection instead of trying to impersonate a user through Symfony security token storage.
  - This keeps MFA status/clear compatible with Mautic 6/7 console containers where token services differ from older builds.

## 0.8.110 - 2026-04-22

- Fixed HostnetAuth MFA helper security token storage detection on Mautic 6/7 console containers.
  - The helper now tries both `security.token_storage` and `security.untracked_token_storage` instead of requiring one public service id.
  - This fixes live MFA status/clear actions on newer Mautic builds where the token storage service is private/aliased differently.

## 0.8.109 - 2026-04-22

- Fixed HostnetAuth MFA helper compatibility across Symfony security token variants.
  - The helper now tries multiple supported authentication token constructor shapes instead of assuming one `UsernamePasswordToken` signature.
  - This fixes live HostnetAuth MFA actions on instances where the older token constructor expects a different argument layout.

## 0.8.108 - 2026-04-22

- Fixed HostnetAuth MFA helper temp PHP script permissions.
  - `admin:mfa-status` and `admin:mfa-clear` now make the generated helper script readable by the Mautic runtime user (`www-data`).
  - This fixes live MCC instance MFA actions failing with `Could not open input file`.

## 0.8.107 - 2026-04-22

- Added HostnetAuth MFA self-service helpers for MCC-driven instance operations.
  - New `mcd-cli admin:mfa-status` inspects whether the matching MCC user exists on the target instance and whether HostnetAuth MFA is currently active for that user.
  - New `mcd-cli admin:mfa-clear` disables HostnetAuth MFA for that user and deletes remembered browser records.
- Implementation detail:
  - the helper boots the local Mautic runtime and applies the change through HostnetAuth's own integration APIs, instead of hard-coding brittle integration-setting table formats.

## 0.8.106 - 2026-04-22

- Disabled swap-only global dispatch pausing by default.
  - `host_pressure_swap_level_pause_threshold` now defaults to `0`, so elevated swap usage alone no longer freezes all MCD task lanes.
  - `php_console_stuck` protection remains active, so genuinely wedged PHP console workloads can still pause dispatch when needed.
- Outcome:
  - campaign rebuild/trigger lanes keep running on hosts with persistent swap usage,
  - MCD no longer silently starves daytime campaign automation just because swap pressure is high.

## 0.8.105 - 2026-04-22

- Added plugin `display_name` support to the MCD plugin catalog.
  - `plugins --catalog-json` now carries human-readable `display_name` from the manifest when available.
  - Instance plugin pickers in MCC can now present versioned variants like stable/dev/original with clean operator-facing labels while still selecting by bundle key under the hood.
- Outcome:
  - one plugin family can expose multiple nice names in the install UI,
  - operators no longer need to parse raw bundle names to choose the right variant.

## 0.8.104 - 2026-04-22

- Added manifest-level `install_bundle` support for plugin variants that share one real install directory.
  - MCD plugin catalog/apply now honors explicit manifest `install_bundle` instead of only the old hardcoded `*Dev -> canonical` alias rule.
  - This allows multiple selectable repo variants of the same plugin family to install into the same runtime path without code duplication in the installer.
- Outcome:
  - repo can expose stable/dev/original variants of Amazon SES as separate choices,
  - all mutually exclusive variants still install into the canonical `plugins/AmazonSesBundle` path.

## 0.8.103 - 2026-04-21

- Fixed quiet-window handling for windows that cross midnight.
  - `_in_daily_quiet_window()` now correctly treats windows like `23:00–08:00` as active on both sides of midnight instead of only on the start day.
  - This directly affects orphan `page_hits` cleanup and SQL page-hit quiet-window rules, making overnight maintenance windows usable as configured.

## 0.8.102 - 2026-04-21

- Added explicit backup guard for orphan `page_hits` cleanup.
  - The new built-in orphan cleanup now skips its run while backup is running, while the pre-backup pause window is active, or while backup slot is still pending for the current local day.
  - This prevents background `page_hits` batched deletes from competing with host backup IO and DB activity during backup windows.

## 0.8.101 - 2026-04-21

- Added built-in orphan `page_hits` cleanup housekeeping in the agent daemon.
  - New runtime-configurable task can periodically remove `page_hits` rows where `lead_id IS NULL`, without relying on external scripts.
  - Cleanup is gated by a quiet window, interval, grace period for recent rows, max runtime budget, and small batched deletes to stay safe on very large tables.
  - The implementation avoids full-table `COUNT(*)` scans; each run only previews and deletes the next bounded candidate batch through indexed `lead_id/date_hit` selection when available.
- Added new runtime knobs for orphan `page_hits` cleanup.
  - `enable_page_hits_orphan_cleanup`
  - `page_hits_orphan_cleanup_interval_sec`
  - `page_hits_orphan_cleanup_quiet_hour`
  - `page_hits_orphan_cleanup_quiet_window_min`
  - `page_hits_orphan_cleanup_batch_size`
  - `page_hits_orphan_cleanup_batches_per_run`
  - `page_hits_orphan_cleanup_sleep_sec`
  - `page_hits_orphan_cleanup_grace_min`
  - `page_hits_orphan_cleanup_max_run_sec`
- Outcome:
  - hosts like `ananasmk` can keep trimming large orphan `page_hits` tails automatically during night windows,
  - cleanup becomes a standard MCD capability instead of per-host shell glue,
  - operators can enable and tune it globally or per-host through runtime config.

## 0.8.100 - 2026-04-21

- Added automatic SQL-ring detection for simple but heavy segments.
  - The agent now inspects eligible segment filter definitions and auto-promotes directly translatable rules into the existing `segment_sql` ring.
  - Initial supported auto-detection covers simple lead-field filters plus page-hit/url-title contains rules, which are the main starvation source on large hosts like `ananasmk`.
  - Default clause budget was raised to `24` supported clauses so larger but still straightforward OR-based page-hit segments (for example `ananasmk` segment `93`) are still eligible for SQL handling.
  - Auto-promoted segments are selected conservatively: only when they are SQL-translatable and show signs of being heavy/problematic (page_hits, checked_out, or repeated recent segment failures/timeouts).
- Fixed SQL time window generation for `*_in_last_N_days` auto-managed segment rules.
  - Generated SQL now uses the instance-local planning time (`{now_local}`) instead of `CURDATE()`, so the direct-DB rebuild matches Mautic's rolling time window semantics instead of snapping to midnight.
- Fixed recent problem window length for auto-promotion heuristics.
  - `tasks_history_keep_days` is now interpreted in real days (`* 86400`) instead of hours, so auto-detection sees the intended failure history before promoting a segment into the SQL ring.
- Outcome:
  - heavy, simple page-hit segments can be diverted away from the regular Mautic segment ring,
  - regular segments stop being starved by the same repeatedly failing IDs,
  - SQL-managed segments still update Mautic membership and rebuild metadata so the UI sees them as rebuilt.

## 0.8.99 - 2026-04-20

- Fixed backup storage auth preservation in MCC-driven host backup settings.
  - Host backup save now recovers missing storage auth from linked hosts when the selected storage comes from discovered/shared storage data.
  - Remote `profile-set` is now applied with the merged storage payload, so existing password/key auth is not dropped during host-side backup storage rebinding.
- Added lightweight periodic backup storage probing in the agent daemon.
  - Every two hours, backup-enabled hosts now probe storage mount/auth non-destructively and push fresh storage status/usage back to MCC.
  - Probe failures are recorded even when no full backup runs, which makes broken storage auth and mount errors visible earlier.
- Fixed early backup preflight failures leaving stale success state behind.
  - Validation/inventory errors now write a fresh failed backup state instead of silently keeping the previous `ok`.

## 0.8.98 - 2026-04-20

- Added instance-level admin password reset command for MCC-driven rescue access.
  - New `mcd-cli admin:reset-password` resolves the target instance from inventory, uses the correct database/table prefix, and updates or creates the matching admin user.
  - Duplicate username/email matches are collapsed before the final row is updated.
  - Existing user timezone/locale are preserved on update; fresh rescue users default to `UTC` / `en_US`.
- Outcome:
  - MCC can now delegate password reset entirely through `MCC -> MCD`,
  - no direct DB work is performed by MCC itself.

## 0.8.97 - 2026-04-20

- Rebuilt the date-based campaign scheduler fix from the correct agent package path.
  - Carries the `campaign_rebuilds_due` date-trigger fix from `0.8.96`.
  - Ensures released source tree updates `mcd_agent/config.py` and `mcd_agent/__init__.py` consistently during self-update validation.
- Outcome:
  - test rollout validates the actual running agent code,
  - date-based campaign auto-start fix is now delivered through a clean package.

## 0.8.96 - 2026-04-20

- Fixed campaign rebuild due-selection for date-based actions.
  - `campaign_rebuilds_due` now also selects published campaigns whose date-triggered action time has already arrived and whose active `campaign_leads` still have no initialized event log.
  - This closes the gap where contacts entered the campaign more than 24 hours earlier, so the trigger ring saw nothing and the rebuild ring never seeded the event log.
- Outcome:
  - date-based campaigns like `ananasMK` campaign `607` are picked up automatically at scheduled time,
  - manual `campaign_rebuild` / `campaign_trigger` is no longer required just because leads were added before the recent 24h window.

## 0.8.95 - 2026-04-20

- Fixed CLI regression in `mcd-cli signals`.
  - Restored missing `--config` parser option after signals collector started loading runtime config explicitly.
  - Result: `signals --json` works again both locally and through `MCC -> MCD host-run`.

## 0.8.94 - 2026-04-20

- Added scheduler-state reconcile to reduce stale running-task drift after restarts and orphaned task rows.
  - Periodically validates tracked `running` rows against real host processes.
  - Marks dead/mismatched rows as lost and collapses duplicate `task_key` rows.
  - Re-adopts valid running tasks into daemon memory without waiting for restart.
- Added stronger scheduler dedupe before spawn.
  - New launches now refuse to start if the same `task_key` is already tracked as running in the persistent task store.
  - Prevents duplicate `import`/segment/campaign launches caused by stale in-memory state boundaries.
- Added new lightweight runtime pressure signals:
  - `scheduler_state_drift`
  - `scheduler_duplicate_task_keys`
  - `php_console_stuck`
  - `swap_pressure_level`
- Added scheduler-aware host pressure pause.
  - When stuck PHP console count and/or swap pressure crosses runtime thresholds, daemon sets a temporary DB dispatch pause instead of continuing to feed heavy rings.
- Added new hot-applicable runtime controls:
  - `scheduler_reconcile_interval_sec`
  - `php_console_stuck_sec`
  - `host_pressure_pause_enabled`
  - `host_pressure_php_stuck_pause_threshold`
  - `host_pressure_swap_level_pause_threshold`
- Outcome:
  - stale scheduler inflation is cleaned automatically,
  - heavy hosts back off earlier under DB/memory pressure,
  - MCC can now surface drift/pressure separately from generic MySQL/PHP/Web semaphores.

## 0.8.93 - 2026-04-19

- Fixed plugin apply regression in the new MCC-driven plugin flow:
  - restored `plugins_dir` resolution inside `run_plugins_interactive()`,
  - non-interactive `plugins --bundle ...` calls from MCC now proceed past selection and conflict handling instead of failing with `NameError`.

## 0.8.92 - 2026-04-19

- Added plugin web-control support primitives for MCC:
  - `mcd-cli plugins --catalog-json` now emits clean machine-readable JSON for a selected instance,
  - `mcd-cli plugins --bundle <BundleName>` allows bundle-name based selection for non-interactive callers.
- Fixed interactive CLI regression:
  - the interactive hub now passes `bundles=None` explicitly when opening the plugin menu.
- Changed plugin catalog mode:
  - suppresses update notice/banner text so MCC can consume the catalog without stdout parsing failures.

## 0.8.91 - 2026-04-19

- Fixed tiny campaign scheduler:
  - removed rebuild-due -> trigger-lane fallback that could launch long useless trigger passes,
  - removed unconditional same-id rebuild->trigger chaining,
  - tiny profile now runs one campaign worker with actual trigger-due campaigns first, then rebuild-due campaigns.
- Fixed campaign trigger SQL time handling:
  - `campaign_lead_event_log.trigger_date` is now compared against instance local time (`now_local`) instead of UTC,
  - recent `campaign_leads.date_added` windows now use local 24h window for trigger detection and campaign weight recency.

## 0.8.90 - 2026-04-19
- Added: `mcd-cli apt-upgrade` command for host-side package update flow.
  - Runs `apt-get update` + `apt-get dist-upgrade -y`.
  - Preserves local config files via dpkg `--force-confdef` + `--force-confold`.
- Changed: `mcd-cli maintenance` supports host cron control.
  - New option: `--stop-cron` on enable.
  - `maintenance off` restores cron only if MCD stopped it earlier.
  - JSON output now includes maintenance + cron state.
- Added: immediate maintenance-state push to MCC after `maintenance` and `apt-upgrade` actions.
- Added: shared maintenance-state collector for daemon/CLI/MCC cache integration.
- Outcome: host operations from MCC are executed locally by MCD and reported back immediately.

## 0.8.89 - 2026-04-18
- Added: new manual command `mcd-cli permissions:fix` for explicit instance filesystem-permissions repair.
  - Uses the same MCD guard engine as daemon/pre-upgrade checks.
  - Pushes state immediately after successful run.
- Changed: `mcd-cli import` shorthand now supports `-i/--instance-id`.
- Changed: executor now passes instance id to `mautic:import` when provided.
- Outcome: MCC instance operations can run import by ID or full import without ID and can trigger explicit permissions fix via MCD-only orchestration path.

## 0.8.88 - 2026-04-18
- Fixed: config section upsert now preserves valid TOML section boundaries when updating existing sections.
  - Root cause: in edge cases, writing `[runtime]` keys could concatenate the last key line with the next section header (e.g. `... = 15[sql]`).
  - `upsert_section_values()` now ensures a newline boundary before the following section when needed.
- Outcome: stable runtime/profile sync updates no longer risk malformed `mcd.toml`.

## 0.8.87 - 2026-04-18
- Added: bidirectional backup-config sync between text config and state profile for stable backup settings.
  - MCC/runtime-applied stable backup runtime keys are now persisted into mutable config file `[runtime]`.
  - `backup profile-set` now mirrors profile sections into text config:
    - `[backup.storage]`
    - `[backup.mysql]`
    - `[backup.archive]`
  - Daemon now polls mutable config and syncs explicit `[backup.*]` edits back into backup profile state DB.
- Scope guard:
  - only stable backup settings are persisted to text config;
  - high-frequency dynamic scheduler/runtime noise remains out of text config.
- Outcome: operators can edit backup settings from either side (MCC or text config) without hidden drift between daemon-effective state and visible config.

## 0.8.86 - 2026-04-18
- Fixed: campaign scheduler fallback for trigger lane in dual-ring modes (`mini`/`midi`/`maxi`/`hiload`).
  - If `campaign_triggers_due` returns empty but `campaign_rebuilds_due` is non-empty, MCD now seeds trigger ring from rebuild-due IDs.
  - Prevents rebuild-only loops where published campaigns are repeatedly rebuilt but never triggered automatically.
  - Adds explicit warning log marker:
    - `campaign trigger fallback active: trigger_due=0, reuse rebuild_due=<N>`
- Outcome: newly published campaigns on rebuild-heavy hosts auto-start without requiring manual `mautic:campaigns:trigger` from console.

## 0.8.85 - 2026-04-17
- Changed: `mcd-cli mautic-upgrade apply` now runs a mandatory pre-upgrade permissions preflight.
  - Reuses MCD filesystem permission guard logic before any upgrade steps.
  - If permission repair fails, upgrade is stopped with explicit error.
- Added: post-upgrade sender dependency restore based on active sender config in `local.php`.
  - Detects `ses+api`/`mautic+ses+api` and ensures `symfony/amazon-mailer`.
  - Detects `sendgrid+api` and ensures `symfony/sendgrid-mailer:*`.
  - For zip installs, normalizes Node.js runtime to v20 before composer require (same preflight path used for existing SES dependency fix).
  - Runs `cache:clear` after dependency restore.
- Outcome: zip upgrades are resilient to missing API mailer packages after update, and upgrade failures from broken permissions are prevented earlier.

## 0.8.84 - 2026-04-15
- Added: new non-interactive cache command:
  - `mcd-cli cache:hard`
  - behavior: safe hard cleanup of `var/cache/prod` (delete + recreate via permission guard).
- Changed: interactive `Cache -> Hard Clear` now uses the same `cache:hard` implementation path.
- Outcome: MCC can run hard cache cleanup via standard MCD command API (no direct host-side shell logic outside MCD).

## 0.8.83 - 2026-04-14
- Fixed: `mcd-cli exec --command cache:clear|cache:warmup` now has built-in permission self-heal flow.
  - first run: executes cache command normally;
  - on `Permission denied`: runs MCD filesystem permissions repair (`www-data` ownership/writability + `bin/console` exec bit);
  - retries the same cache command automatically.
- Added: cache self-heal output marker `MCD_WARNING_CACHE_PERMISSIONS_REPAIRED` so MCC can show warning completion instead of plain success.
- Behavior: command exits `0` after successful retry, while preserving warning diagnostics in output.

## 0.8.82 - 2026-04-14
- Added: `mcd-cli exec` supports cache operations:
  - `cache:clear`
  - `cache:warmup`
- Added: shorthand commands:
  - `mcd-cli cache:clear`
  - `mcd-cli cache:warmup`
- Changed: MCC instance operations can now delegate cache actions through MCD only, aligning with orchestrator-only MCC model (no direct MCC execution of Mautic/PHP commands on hosts).

## 0.8.81 - 2026-04-12
- Added: APT service-profile support for `unattended-upgrades` policy management via dynamic MCC/MCD parameters.
  - New APT profile keys:
    - `unattended_upgrade_mode`: `off|security|all`
    - `unattended_upgrade_schedule_cron`: cron expression (host local time), e.g. `30 23 * * 0`
    - `unattended_upgrade_blacklist`: package patterns excluded from unattended updates
- Added: managed unattended-upgrades provisioning on hosts:
  - `/etc/apt/apt.conf.d/52mcd-unattended-upgrades`
  - `/etc/apt/apt.conf.d/20auto-upgrades`
  - optional scheduled runner `/etc/cron.d/mcd-unattended-upgrades`
- Behavior:
  - `mode=all` enables unattended updates for all origins, honoring blacklist.
  - `mode=security` keeps security-focused behavior and still applies blacklist.
  - `mode=off` disables unattended updates and removes managed cron/config override files.
- Added: `apt_state.unattended_upgrade` telemetry block for MCC visibility.

## 0.8.80 - 2026-04-12
- Added: modular one-time APT repo profiles with local marker persistence (`/opt/mcd/var/apt-repo-profiles.json`).
  - New independent profile toggles (MCC dynamic payload keys):
    - `db_repo_profile_enabled`, `db_repo_profile_apply_once`
    - `ondrej_php_profile_enabled`, `ondrej_php_profile_apply_once`
    - `ondrej_nginx_profile_enabled`, `ondrej_nginx_profile_apply_once`
- Added: DB stack auto-detection for repo profile decisions.
  - Detects and handles:
    - `mysql 8.0` -> no repo changes (one-time skip marker)
    - `mysql 8.4` -> ensure official MySQL repo (one-time)
    - `mariadb 11.4` -> ensure official MariaDB 11.4 repo (one-time)
    - `percona server 8.0` -> ensure official Percona PS repo (one-time)
    - `percona xtradb cluster 8.0` -> ensure official Percona PXC repo (one-time)
- Changed: APT repair flow now separates one-time profile enforcement from legacy “repair on apt error” behavior.
  - If modular profile is enabled for a repo family, repetitive per-cycle repair attempts are suppressed.
- Added: manual repo-profile rescan command:
  - `mcd-cli service-profile rescan --component apt`
  - Clears local repo profile marker and re-runs APT profile apply immediately.
- Added: `apt_state.repo_profiles` payload for MCC visibility of marker path and per-profile status.

## 0.8.79 - 2026-04-12
- Fixed: `tiny` campaign chain no longer drops rebuild-only campaigns.
  - Root cause: dispatch source was `trigger` ring only, so campaigns that were due in `rebuild` ring but absent in `trigger` ring were skipped indefinitely.
  - Now `tiny` planning merges rebuild-due IDs into trigger source set for chain scheduling.
  - Dispatch fallback order in tiny chain is now:
    - trigger priority
    - trigger regular
    - rebuild priority
    - rebuild regular
  - Result: published campaigns requiring rebuild are not missed and proceed through `rebuild -> trigger` chain without manual intervention.

## 0.8.78 - 2026-04-11
- Added: automatic `symfony/amazon-mailer` dependency preflight for SES SNS plugin pair.
  - Trigger bundles:
    - `AmazonSnsCallbackBundle`
    - `MauticAmazonSesBundle`
  - Applied in:
    - plugin install/update/reinstall flow (`mcd-cli plugins`)
    - Mautic version upgrade flow (`mcd-cli mautic-upgrade`)
- Behavior:
  - If `symfony/amazon-mailer` is already installed, no action is taken.
  - If missing:
    - For zip installs, MCD ensures Node.js 20 runtime and then runs `composer require symfony/amazon-mailer`.
    - For composer installs, MCD runs `composer require symfony/amazon-mailer` (without Node runtime migration step).
  - After dependency install, MCD runs `cache:clear`.

## 0.8.77 - 2026-04-11
- Added: new mutually exclusive SES webhook pair in plugin resolver.
  - `MauticAmazonSesBundle` <-> `AmazonSnsCallbackBundle`.
  - Installing either one now force-removes the competing implementation before apply.
  - Works together with manifest-level `replaces` for deterministic one-of behavior.

## 0.8.76 - 2026-04-10
- Fixed: campaign trigger and rebuild rings no longer use the same broad “all published campaigns” SQL.
  - `campaign_triggers_due` now selects campaigns with real due scheduled events or newly-added campaign contacts not yet initialized in event log.
  - `campaign_rebuilds_due` now selects campaigns whose source segment membership differs from `campaign_leads`.
  - Legacy `campaigns_due` all-published defaults are auto-migrated/ignored for the new split planner, while intentional custom SQL remains supported.
- Fixed: campaign planning failures no longer preserve stale campaign rings.
  - On planning failure, trigger/rebuild rings are cleared instead of repeatedly launching old entities.
- Added: DB dispatch circuit-breaker for scheduler safety.
  - MySQL overload/errors such as `Too many connections`, lost connection, lock wait timeout, deadlock, and metadata-lock overload pause new dispatch briefly per root.
  - DB watchdog observations can also pause dispatch when long-query or metadata-lock thresholds are exceeded.

## 0.8.75 - 2026-04-08
- Fixed: SQL segment technical ring is now persistent and restart-safe.
  - Added persistent state/lock per `root + segment_id` in state backend (`runtime_sync`) with owner, heartbeat and finish metadata.
  - Prevents the same SQL-managed segment from being started twice after daemon restart/self-update while previous run is still considered active.
  - New runtime keys:
    - `segment_sql_min_repeat_sec` (default `3600`)
    - `segment_sql_lock_heartbeat_sec` (default `15`)
    - `segment_sql_orphan_policy` (`manual` or `reclaim_stale`)
    - `segment_sql_orphan_after_sec` (default `900`)
- Fixed: SQL-managed segments are rate-limited and do not restart more often than once per hour by default.
- Fixed: direct DB rebuild for SQL-managed segments no longer keeps long `page_hits` scans inside one write transaction.
  - Heavy `SELECT DISTINCT ...` now runs before transaction start.
  - Transaction scope now contains only:
    - segment membership delete
    - membership insert
    - `lead_lists` metadata update
- Fixed: manual `segment` launch requests are skipped when the same segment is already running in SQL technical ring.
- Added: optional quiet-window protection for `page_hits`-based SQL segments.
  - New runtime keys:
    - `segment_sql_page_hits_quiet_only`
    - `segment_sql_page_hits_quiet_hour`
    - `segment_sql_page_hits_quiet_window_min`
- Added: automatic Mautic page-hit cascade patch guard.
  - If page-hit save fails, MCD patch now prevents dispatch of `PageHitNotification` for missing hit rows.
  - If handler receives invalid/missing hit payload, patched handler logs warning and exits cleanly instead of causing `TypeError` / `EntityManager is closed` noise.
  - New runtime key:
    - `pagehit_cascade_patch_policy`

## 0.8.74 - 2026-04-08
- Fixed: SQL segment technical ring now marks rebuilt segments as ready in Mautic UI/state.
  - Root cause: SQL rebuild wrote `date_modified = NOW()` together with `last_built_date = NOW()`, while Mautic treats a segment as still needing rebuild when `date_modified >= last_built_date`.
  - Now: SQL rebuild clears stale checkout markers and guarantees `last_built_date` is newer than the effective `date_modified`.
  - Result: SQL-ring segments no longer stay visually stuck in "needs rebuild" after successful direct DB rebuild.

## 0.8.73 - 2026-04-08
- Changed: increased agent MySQL socket timeouts for DB-backed operations (`read_timeout`/`write_timeout` -> `1800s`).
  - Purpose: prevent false `Lost connection to MySQL server during query (timed out)` on long SQL segment-ring rebuilds.
  - Impact: SQL technical ring can complete heavy `page_hits`-based rebuild queries instead of failing on client timeout.

## 0.8.72 - 2026-04-08
- Added: SQL segment technical ring for direct DB rebuild of selected segments.
  - New runtime keys:
    - `segment_sql_ring_enabled`
    - `segment_sql_ring_max_per_tick`
    - `segment_sql_ring_rules`
  - SQL-ring segments are excluded from standard Mautic segment rings (`priority`/`regular`).
  - Dependency order is respected (`depends_on`): prerequisites are rebuilt before dependent segment.
  - After SQL rebuild, segment metadata is updated in Mautic (`date_modified`, `last_built_date`, `last_built_time`) so UI shows current rebuild state.

## 0.8.71 - 2026-04-03
- Changed: interactive `Environment` menu now uses one dynamic IPv6 toggle item instead of three separate entries.
  - Before: `IPv6 Status`, `Disable IPv6`, `Enable IPv6`.
  - Now: one toggle item that switches label by current state (`Enable IPv6` when disabled, `Disable IPv6` when enabled).
  - `State Backend Status` and conditional `Bootstrap State DB` remain available in the same menu.

## 0.8.67 - 2026-04-03
- Fixed: `maintenance on` / scheduler pause flag now actually blocks all new daemon dispatch launches.
  - Root cause: daemon dispatch loop ignored `scheduler_pause_flag_path` and continued scheduling.
  - Now: when pause flag exists, daemon skips all new launches (auto rings + manual request dispatch), while running tasks continue until completion/explicit kill.
  - Effect: `mcd-cli maintenance on --kill-orphans` behaves as expected and no new segment/campaign tasks are started afterward.

## 0.8.66 - 2026-04-01
- Fixed: removed per-cycle heavy `campaign_weights` SQL call when campaign weights are already cached.
  - Before: daemon still executed heavy aggregate weight query every planning tick, even without recalculation need.
  - Now: heavy query runs only on real weight recalc (`ids set changed` or cache expired).
- Fixed: latest-priority campaign selection no longer depends on running heavy query every cycle.
  - If no fresh weight rows are fetched in current tick, daemon uses id-based latest fallback immediately.
- Optimized: default `sql.campaign_weights` now uses one aggregated subquery over `{prefix}campaign_leads` with conditional sums (pending + recent),
  instead of two separate grouped subqueries.
  - Effect: lower DB load and fewer SQL timeout spikes under high campaign pressure.

## 0.8.65 - 2026-04-01
- Added: self-update artifacts retention policy for `/opt/mcd` (automatic cleanup).
  - New runtime keys:
    - `mcd_update_cleanup_enabled` (default `true`)
    - `mcd_update_cleanup_interval_sec` (default `86400`)
    - `mcd_update_keep_archives` (default `3`)
    - `mcd_update_keep_preupdate_backups` (default `3`)
    - `mcd_update_artifacts_max_age_days` (default `30`)
  - Cleanup scope:
    - `/opt/mcd/var/updates/mcd-agent-*.tar.gz`
    - `/opt/mcd/var/backup/mcd-src-preupdate-*.tgz`
    - stale staging dirs `/opt/mcd/var/updates/src.next-*` and `src.prev-*`
  - Effect: old versions from earliest history are pruned automatically while keeping rollback window.

## 0.8.64 - 2026-04-01
- Fixed: campaign priority fallback when `campaign_weights` SQL times out on overloaded DB.
  - If weight query fails, latest priority campaigns are now selected by ID fallback (`newest first`) instead of empty latest-set.
  - Prevents newly published campaigns from being starved by old ring tails under DB timeout pressure.
- Changed: campaign ring ordering now prefers newer campaign IDs on equal weight.
  - Impact: fresh campaigns are rebuilt/triggered earlier during heavy load conditions.

## 0.8.63 - 2026-04-01
- Fixed: plugin install sanitization now removes macOS archive artifacts (`._*`, `__MACOSX`) before deploy.
  - Root cause: AppleDouble files in dev plugin archives caused PHP class redeclaration and `500` on plugin load.
- Fixed: dev/stable exclusive plugin apply now always force-removes counterpart before install (deterministic one-of behavior).
  - Applies to: `SalesSnapBundle` <-> `SalesSnapBundleDev`, `AmazonSesBundle` <-> `AmazonSesBundleDev`.
- Fixed: plugin status/list now respects canonical-path dev alias state metadata and does not show both variants as installed simultaneously.
- Fixed: legacy `/etc/php/*/(fpm|cli)/conf.d/98-mcd-php.ini` cleanup hardening in service profile apply.
  - Cleanup now runs across all installed PHP versions.
  - Legacy baseline file is no longer restored during rollback paths.

## 0.8.62 - 2026-04-01
- Fixed: dev plugin aliases now install into canonical stable plugin directories.
  - `SalesSnapBundleDev` installs to `plugins/SalesSnapBundle`.
  - `AmazonSesBundleDev` installs to `plugins/AmazonSesBundle`.
- Result: dev/stable remain separate choices in MCD plugin catalog, but runtime bundle paths stay unchanged and compatible with Mautic internals.
- Added: post-install alias cleanup for dev entries, so only canonical plugin directory remains on disk (no split path drift between dev and stable).

## 0.8.60 - 2026-04-01
- Fixed: plugin bundle name validation now accepts `*BundleDev` variants in addition to standard `*Bundle`.
  - Root cause: strict validator filtered out dev bundles from manifest view in `mcd-cli plugins`.
  - Impact: `AmazonSesBundleDev` / `SalesSnapBundleDev` now appear in interactive and CLI plugin lists.

## 0.8.59 - 2026-04-01
- Added: mutual exclusion guard for stable/dev plugin variants in MCD plugin manager.
  - Covered pairs: `AmazonSesBundle` <-> `AmazonSesBundleDev`, `SalesSnapBundle` <-> `SalesSnapBundleDev`.
  - In one apply action, selecting both sides now fails fast with clear conflict error.
  - When applying one side and opposite side is already installed, MCD auto-removes conflicting installed counterpart before apply.
- Result: prevents dual-install drift and removes operator error path in both interactive and CLI plugin flows.

## 0.8.58 - 2026-03-31
- Fixed: `mcd-cli interactive` now triggers immediate MCC state push after successful local changes, same as non-interactive commands.
  - Added immediate push after interactive plugin apply (`Plugins` menu).
  - Added immediate push after interactive Mautic upgrade.
  - Added immediate push after interactive instance inventory changes (rescan/add/remove).
  - Added immediate push after interactive `env ipv6` toggle.
  - Added immediate push after interactive backup run/prune actions.
- Result: MCC cache reflects MCD-initiated runtime changes without waiting for periodic sync.

## 0.8.57 - 2026-03-31
- Changed: removed managed global PHP baseline file (`98-mcd-php.ini`) from `php_fpm` service-profile apply.
  - Agent no longer writes `/etc/php/*/(fpm|cli)/conf.d/98-mcd-php.ini`.
  - On apply, existing legacy `98-mcd-php.ini` files are removed automatically (both FPM and CLI).
  - Purpose: prevent global CLI/FPM side effects from centrally enforced php.ini baseline.

## 0.8.56 - 2026-03-30
- Fixed: local MySQL connection fallback in agent DB layer (`MauticDB`) for plugin `pre_sql` and all DB-backed scheduler operations.
  - Added local connection variants: configured host, `localhost`, `127.0.0.1`, and common unix sockets.
  - Purpose: eliminate false auth failures like `Access denied for user ...@127.0.0.1` when DB grants are valid for `...@localhost`.
  - Impact: plugin apply flow (`pre_sql`) and other agent DB reads/writes now work on mixed local auth layouts without manual grant surgery.

## 0.8.55 - 2026-03-30
- Added: DB watchdog skeleton (observe-first) with runtime-dynamic policy.
  - New runtime key: `runtime.db_watchdog` (JSON map).
  - Supports global rules + host overrides with host precedence by rule `id`.
  - Designed for phased rollout: telemetry collection first, actions can be enabled later.
- Added: processlist telemetry collection per instance root (no kill actions while `observe_only=true`).
  - Metrics include metadata-lock waits, long queries, orphan candidates, and rule-hit counters.
  - Slow query samples are truncated and shipped for diagnostics.
- Added: MCC signal integration for DB watchdog telemetry.
  - Pushed via `signals.totals`:
    - `db_watchdog_samples`
    - `db_watchdog_errors`
    - `db_watchdog_metadata_lock_waits`
    - `db_watchdog_long_queries`
    - `db_watchdog_orphan_candidates`
    - `db_watchdog_rule_hits`
  - Event samples pushed via `signals.details.db_watchdog_recent`.
  - Signal history now stores compact `db_watchdog_events` snapshots for 3-day trend analysis.

## 0.8.54 - 2026-03-30
- Fixed: scheduler retry semantics now support explicit unlimited retries for failed tasks.
  - `runtime.task_retry_max <= 0` means unlimited retries.
  - `runtime.task_retry_max = 1` keeps no-retry behavior.
  - `runtime.task_retry_max > 1` keeps bounded retry cap behavior.
- Result: ring tasks no longer drop out after finite attempts when host policy requires continuous re-pick until success.

## 0.8.53 - 2026-03-27
- Fixed: template clone detection fallback now persists and reuses source host identity from marker file when MCC host key is not configured.
  - Added: `/opt/mcd/var/template_identity.json` source marker handling in host identity resolver.
  - Added: clone startup now always runs inventory autodiscovery refresh before scheduling loop.
  - Result: clones from template hosts no longer keep stale template instance cache and no longer require manual `mcd-cli instances rescan`.

## 0.8.52 - 2026-03-27
- Fixed: template-clone startup now forces inventory autodiscovery rescan before daemon scheduling loop.
  - Root cause: cloned hosts could inherit stale `instances` cache from template and keep old instance UID/name until manual `instances rescan`.
  - Result: on clone detection, MCD refreshes local instance inventory immediately and reports the cloned host as a new node without stealing template instance identity in MCC.
- Fixed: template clone detection now works even when `[mcc].host_name` is empty.
  - Added persistent template source marker (`/opt/mcd/var/template_identity.json`) used as fallback clone source identity.
  - Result: template-built nodes reliably auto-detect clone host rename and trigger autopromote + fresh discovery.

## 0.8.51 - 2026-03-21
- Fixed: successful `state-db init` now persists state backend settings into the correct config section (`[state]`) instead of runtime section.
  - Root cause: bootstrap wrote `state_*` keys via runtime upsert helper, which does not affect daemon state backend loading.
  - Result: after bootstrap/reload, daemon keeps `mysql_hybrid` mode and MCC status no longer falls back to yellow `legacy`.

## 0.8.50 - 2026-03-21
- Fixed: `state-db init` / interactive "Bootstrap State DB" now prefers unix socket in `auto` mode for local admin host, even when admin password is non-empty.
  - Root cause: auto socket resolution was gated by non-empty password and could fall back to TCP (`127.0.0.1`), causing `root@127.0.0.1` auth mismatch on hosts where root auth is socket/localhost-based.
  - Result: on local hosts, bootstrap uses `/var/run/mysqld/mysqld.sock` (or `/run/mysqld/mysqld.sock`) by default unless socket is explicitly overridden.

## 0.8.49 - 2026-03-21
- Fixed: State DB bootstrap now handles non-ASCII DB admin passwords in `Environment -> Bootstrap State DB`.
  - Added UTF-safe fallback path: when PyMySQL admin phase fails with `latin-1` encode error, agent executes the same bootstrap SQL through local `mariadb/mysql` CLI.
  - Scope: `create_state_database_with_admin` only; runtime user/schema validation remains unchanged.
  - Result: hosts with valid root/admin password containing non-latin characters can initialize `mysql_hybrid` state backend without false auth failures.

## 0.8.48 - 2026-03-21
- Fixed: Zabbix MySQL bootstrap now also tries MCD runtime state DB credentials as an additional DB-admin source.
  - Added `runtime_state` candidate in SQL execution path (`state_mysql_user/password/socket/host/port`).
  - Existing admin source order remains: root socket -> `debian.cnf` -> runtime state credentials.
  - Purpose: recover hosts where root socket and `debian.cnf` auth are unavailable but MCD has valid local DB credentials.

## 0.8.47 - 2026-03-21
- Fixed: Zabbix MySQL bootstrap fallback now scans all `/etc/mysql/debian.cnf` sections (not only `[client]`) for DB admin credentials.
  - Agent now tries all unique `(user,password,socket)` combinations from defaults and named sections.
  - Purpose: support hosts where valid maintenance/admin user is present outside `[client]` (for example `debian-sys-maint`).
  - Result: fewer manual interventions on hosts with non-standard `debian.cnf` layouts.

## 0.8.46 - 2026-03-21
- Fixed: Zabbix MySQL bootstrap now handles hosts with strict MySQL password policy (`validate_password.policy=MEDIUM`).
  - When bootstrap hits `ERROR 1819` for `zbx_monitor@127.0.0.1`, agent applies a safe temporary fallback:
    1. reads current `validate_password.policy`,
    2. temporarily switches policy to `LOW`,
    3. creates/grants monitor user,
    4. restores original policy value.
  - Fallback is only used on policy-related bootstrap failures and keeps one-shot marker semantics intact.

## 0.8.45 - 2026-03-21
- Fixed: Zabbix MySQL bootstrap now supports hosts where `root@localhost` requires password.
  - Added safe fallback DB admin credential source from `/etc/mysql/debian.cnf` (`[client]` section).
  - Bootstrap probe/apply now tries:
    1. socket auth as local root (`root_socket`),
    2. distro-maintained DB admin from `debian.cnf` (`debian_cnf`).
  - Result: one-shot `zbx_monitor@127.0.0.1` bootstrap can complete on mixed MariaDB/MySQL auth layouts without manual SQL.

## 0.8.44 - 2026-03-20
- Added: one-time Zabbix MySQL monitor bootstrap in agent APT workflow.
  - Agent can now create/grant `zbx_monitor@127.0.0.1` idempotently with marker tracking.
  - SQL is applied once by default (`zabbix_mysql_monitor_apply_once=true`) and does not loop every hour.
  - Result is persisted in marker file (`/opt/mcd/var/zabbix-mysql-bootstrap.json`, or state-dir-relative path).
- Added: automatic one-shot bootstrap attempt in periodic `apt_state` collection (for new hosts), with manual retry via CLI.
- Added: dedicated CLI helper:
  - `mcd-cli zabbix status`
  - `mcd-cli zabbix bootstrap-mysql-user [--force]`
- Changed: `apt_state` payload now includes `zabbix_mysql_monitor` state block for MCC visibility.
- Changed: APT profile apply accepts agent config context and includes zabbix bootstrap status in apply result.

## 0.8.43 - 2026-03-20
- Fixed: service-profile auto-apply is now idempotent for unchanged payloads.
  - `php_fpm` apply returns `noop` and does not run reload/restart when managed files are unchanged.
  - `mysql` apply returns `noop` and does not run reload/restart when managed drop-in is unchanged.
- Impact: prevents unnecessary hourly MariaDB restarts when `service_profiles_auto_apply=true` and profile content is unchanged.

## 0.8.42 - 2026-03-20
- Added: backup storage free-space snapshot on backup completion.
  - `backup.run` now captures storage usage (`total/used/free/used_pct`) at the end of a successful backup run.
  - Snapshot is persisted in backup state and backup marker metadata for MCC ingest.
- Added: backup push payload fields for storage snapshot telemetry:
  - `last_storage_checked_at`
  - `last_storage_total_bytes`
  - `last_storage_used_bytes`
  - `last_storage_free_bytes`
  - `last_storage_used_pct`
- Purpose: enable MCC dashboard backup free-space semaphore based on real backup-target usage, without continuous polling.

## 0.8.41 - 2026-03-19
- Added: new self-update policy/channel target `cluster` for controlled rollout streams.
  - Agent policy resolver now accepts `cluster` alongside `approved/test/lts/off`.
  - Legacy channel mapping now supports `mcd_update_channel=cluster`.
  - `mcd-cli self-update` help reflects `cluster` channel support.
- Purpose: enable dedicated rollout lane for cluster hosts without mixing them with generic approved/test/lts flows.

## 0.8.40 - 2026-03-19
- Fixed: added guarded MySQL state backoff for `mysql_hybrid` state backend.
  - On repeated MySQL failures, state operations now enter timed backoff (`mysql_backoff_active`) instead of retrying every loop.
  - Backoff is adaptive for critical classes (`1040 too many connections`, `1290 super-read-only`) to reduce pressure on cluster DB.
  - `state_backend` payload now includes retry metadata (`retry_after_sec`, `retry_after_utc`, error code) while backoff is active.
- Fixed: reduced state-push DB write/read noise in fast loops.
  - Added empty-queue cooldown for profile-event reads to avoid per-cycle DB polling when queue is empty.
  - Added snapshot upsert throttling: unchanged payload hash is no longer upserted every cycle.
  - Added cached `state_backend` probe payload between push cycles.
- Changed: MySQL state node identity resolution now prefers local host identity (`local_hostname`) before MCC alias.
  - Prevents cross-node row collisions in shared/clustered state DB when nodes share one MCC host alias.

## 0.8.39 - 2026-03-18
- Fixed: runtime-overrides startup sync is now mandatory after daemon start/restart (including post self-update restart), even when periodic runtime-overrides polling is disabled.
  - Added startup sync pending state with retry/backoff until first successful MCC fetch.
  - Prevents hosts from remaining on stale local-only runtime (rings/backup/runtime flags) after service restart.
- Result: after restart, agent reliably re-applies MCC runtime overrides without requiring manual `runtime-overrides trigger`.

## 0.8.38 - 2026-03-16
- Changed (default behavior): reduced MCC API noise to match aggregated push model.
  - `[mcc].push_on_change` default is now `false` (periodic push remains enabled).
  - New `[mcc].runtime_overrides_poll_enabled` default `false`:
    - periodic `/api/v1/agent/runtime-overrides` polling is disabled by default,
    - MCC-triggered immediate runtime sync (`runtime-overrides.poll`) still works.
  - New `[mcc].profile_guard_enabled` default `false`:
    - periodic `/api/v1/agent/config-desired` drift-check is disabled by default.
- Updated example configs:
  - `control-plane/agent/etc/mcd-agent.example.toml`
  - `control-plane/agent/etc/mcd-agent.system.example.toml`
  - Added new MCC flags and documented low-noise defaults.

## 0.8.37 - 2026-03-16
- Fixed: mysql-hybrid schema migration could fail on long composite indexes in utf8mb4 (`ERROR 1071 key too long`), leaving mixed legacy/new state tables.
  - Reworked TaskStore MySQL indexes/PK to use safe prefix lengths:
    - `tasks.idx_tasks_running`: `root(191)`
    - `tasks.idx_tasks_key`: `task_key(191)`
    - `weight_cache.PRIMARY`: `root(191)`
    - `weight_cache.idx_weight_cache_lookup`: `root(191)`
    - `manual_requests.idx_manual_requests_pending`: `root(191)`
- Added: explicit TaskStore startup schema ensure (`ensure_mysql_state_schema`) before sqlite->mysql migration.
  - Guarantees host-scoped table migration is applied before runtime writes in cluster/shared-DB mode.
  - Prevents partial migrations where only one table got `host_name` and others stayed legacy.

## 0.8.36 - 2026-03-16
- Fixed: mysql-hybrid scheduler state is now node-scoped in shared DB mode (cluster-safe writes/reads).
  - Added host/node scope (`host_name`) to TaskStore MySQL tables:
    - `mcd_tasks`
    - `mcd_weight_cache`
    - `mcd_runtime_sync`
    - `mcd_manual_requests`
  - Agent now always reads/writes only its own rows (`host_name=<this node>`), so one node cannot overwrite/delete another node's scheduler state.
- Fixed: `weight_cache` conflict class in Galera clusters.
  - Weight cache writes are now isolated per node key-space.
  - Legacy shared `weight_cache` rows are truncated once during schema migration (derived data is rebuilt automatically).
- Added: in-place schema migration for legacy mysql-hybrid tables on startup.
  - Adds host-scoped columns/indexes/PK where needed.
  - Converts `runtime_sync` primary key from global `key` to composite `(host_name, key)`.
  - Rebuilds host-aware indexes for tasks/manual requests.
- Changed: sqlite->mysql migration path now inserts task/manual rows without forcing legacy row IDs, avoiding cross-node ID contention in shared DB mode.

## 0.8.35 - 2026-03-16
- Fixed: legacy `sql.campaigns_due` migration now also upgrades DESC/no-deleted variants to the current safe default with campaign active-window filters:
  - `(publish_up IS NULL OR publish_up <= now_local)`
  - `(publish_down IS NULL OR publish_down >= now_local)`
- Added legacy patterns handled by migration:
  - `... AND (c.deleted IS NULL) ORDER BY c.id DESC`
  - `... WHERE c.is_published = 1 ORDER BY c.id`
  - `... WHERE c.is_published = 1 ORDER BY c.id DESC`
- Impact: prevents endless processing of expired campaigns and restores predictable execution for scheduled campaigns.

## 0.8.34 - 2026-03-16
- Changed: sender classification now uses active transport/DSN only (`mailer_dsn` + transport keys).
  - Removed plugin-driven classification dependency for SES labels.
  - If active DSN is `ses+api://...`, sender is now classified as `ses+api` even when SES plugins are installed.
  - Legacy fallback remains for transport-only cases (for example `mautic.transport.amazon_api` -> `ses+api`).

## 0.8.33 - 2026-03-15
- Fixed: sender-type detection for Mautic 4 legacy transport names.
  - `mautic.transport.amazon_api` and related `amazon*` transport values are now classified as SES sender flow.
  - With `AmazonSesBundle` installed, legacy Amazon API transport now maps to `mautic+ses+api` (instead of `unknown`).
- Result: sender column in MCC dashboard now resolves correctly for Mautic 4 installations using legacy transport format.

## 0.8.32 - 2026-03-15
- Added: sender telemetry in state push for each discovered Mautic instance:
  - `sender_type` (human label),
  - `sender_key` (stable key),
  - `sender_title` (detection details for tooltip/debug).
- Added: sender profile auto-detection from local Mautic config (`config/local.php` and composer/zip variants) with plugin hints.
- Supported sender labels currently include:
  - `mautic+ses+api`, `ses+api`, `ses+smtp`, `sendgrid+api`, `mailgun+api`, `smtp`, `sendmail`, `zender+api`, `unknown`.

## 0.8.31 - 2026-03-15
- Added: permissions-guard repair event details in agent telemetry payload (`signals.details.fs_permissions_fix_recent`).
  - Per-event fields: `ts`, `path`, `sample_path`, `reason`, `actor`, `actor_source`, `before_owner_group`, `before_mode`, `result`, `error`.
  - Actor detection is best-effort: prefers `auditd` (`ausearch`) and falls back to ownership-based guess.
- Changed: permissions-guard daemon logs now include detailed repair/error context (reason, actor/source, previous owner/mode, path).
- Changed: MCC state push keeps permissions-fix counters as delta and now also ships pending repair events for dashboard hover diagnostics.

## 0.8.30 - 2026-03-15
- Added: permissions-fix delta signal in MCC state payload (`signals.totals.fs_permissions_fix`).
  - Source: filesystem permissions watchdog repairs in daemon loop.
  - Counting model: per-push delta (repaired paths + optional console exec fix), reset after successful push.
  - Purpose: MCC dashboard can classify permission-fix activity by day (today vs previous days) without extra DB schema.

## 0.8.29 - 2026-03-15
- Added: filesystem permissions watchdog for Mautic instance roots.
  - New runtime keys (MCC dynamic overrides supported):
    - `fs_permissions_guard_enabled`
    - `fs_permissions_guard_interval_sec`
    - `fs_permissions_guard_paths`
    - `fs_permissions_guard_fix_console_exec`
    - `fs_permissions_guard_console_relpath`
  - Agent now periodically verifies/repairs owner+mode on critical paths (`var/cache`, `var/logs`, `var/spool`, `var/tmp`, media/config paths).
  - Agent also enforces executable bit on `bin/console` (`chmod ug+x`) and runtime owner when configured.
  - Watchdog runs in all profiles, including `passive`.

## 0.8.28 - 2026-03-14
- Fixed: APT phased update detection now also uses `apt-cache policy` phased marker parsing (e.g. `(phased 60%)`), not only `apt-get -s upgrade` text blocks.
  - Prevents phased-only updates from being misclassified as regular pending updates (`updates_pending`/red).
  - Result: phased-only cases are classified as `updates_deferred` (yellow) consistently, including cluster nodes.

## 0.8.27 - 2026-03-14
- Fixed: Gluster peer connectivity parsing in `cluster_db.gluster`.
  - `gluster peer status --xml` now evaluates `<connected>1</connected>` / state `3` correctly.
  - Prevents false `peers_connected=0` and false degraded Gluster semaphore when peers are healthy.

## 0.8.26 - 2026-03-14
- Added: cluster telemetry extensions in `cluster_db` payload for MCC dashboard:
  - `haproxy`: service state, backend server statuses, and effective DB route mode (`local|backup|remote|down|unknown`).
  - `gluster`: glusterd state, volume start status, peer connectivity summary, and detected gluster mounts.
- Purpose: enable cluster-level HAProxy/Gluster semaphores in MCC with detailed hover diagnostics.

## 0.8.25 - 2026-03-14
- Added: periodic cluster DB telemetry collection in agent state payload (`cluster_db`), pushed to MCC cache.
  - Source connections: state DB MySQL credentials first, backup MySQL credentials fallback.
  - Collected diagnostics:
    - Galera: readiness/connectivity/primary state, local sync state, cluster size, recv/send queue averages, flow control pause.
    - Replica: IO/SQL thread status and seconds-behind lag.
    - Read-only flags: `read_only`, `super_read_only`.
- Changed: telemetry is cached in-memory per push interval to avoid extra DB probing load.
- Purpose: cluster health semaphores in MCC dashboard with host-local measurements and no UI-side polling.

## 0.8.24 - 2026-03-14
- Added: `mcd-cli scheduler status` now supports diagnostic output:
  - `--verbose` prints tracked running tasks (`task_type`, `entity_id`, `pid`, `root`, `command_str`).
  - `--json` returns structured scheduler status payload.
- Purpose: faster factual ring/task troubleshooting on test hosts without killing/altering running jobs.

## 0.8.23 - 2026-03-14
- Fixed: segment due planning now accounts for both membership additions and removals/changes more consistently.
  - `sql.segments_due` default now includes additional due signals:
    - segment definition changes (`lead_lists.date_modified > last_built_date`),
    - active member lead changes (`leads.date_modified/date_added > last_built_date` for members in `lead_lists_leads`).
- Added: periodic segment full-scan fallback (`runtime.segment_full_scan_interval_sec`).
  - Agent periodically rebuild-plans from all published segments (ordered by oldest `last_built_date`) even when due SQL is quiet.
  - Default by profile:
    - `tiny=60s`, `mini=120s`, `passive/midi/maxi/hiload=300s`, generic runtime default `300s`.
- Added: import-aware full-scan boost for segments.
  - While imports are pending (and for a short hold window after), segment planning is forced to full-scan mode.
  - This prevents missed/late segment refresh after import-driven contact changes.
- Added: startup migration now upgrades legacy `sql.segments_due` defaults from both old forms:
  - pre-0.8.22 full published-order default,
  - 0.8.22 due-only default.

## 0.8.22 - 2026-03-14
- Fixed: scheduler `sql.segments_due` default no longer scans all published segments every cycle.
  - New due-only default includes segments that are:
    - never built,
    - stale (`last_built_date` older than 24h),
    - or have new members in `lead_lists_leads` since the last build.
  - Result: tiny/mini single-ring execution converges faster and avoids long queue latency before relevant segment IDs are reached.
- Fixed: scheduler `sql.campaigns_due` default now respects campaign active window (`publish_up/publish_down`) and excludes out-of-window campaigns from ring planning.
- Added: automatic config migration on agent start for legacy SQL defaults in `[sql]`:
  - `segments_due = "SELECT id FROM ... WHERE is_published = 1 ORDER BY id"`
  - `campaigns_due = "SELECT c.id FROM ... WHERE c.is_published = 1 ... ORDER BY c.id"`
  - Legacy defaults are upgraded in-place to new due-aware defaults (only when the exact legacy defaults are detected).

## 0.8.21 - 2026-03-14
- Changed: `apt_state` payload now classifies pending updates into:
  - `pending_regular` (actionable updates),
  - `pending_phasing` (deferred by phased rollout),
  - `pending_hold` (packages on hold).
- Added: `pending_total`, `upgradable_packages[].state`, `phasing_packages[]`, `held_packages[]` in agent APT state.
- Changed: backward-compatible `pending_updates` now maps to `pending_regular` so MCC can treat `hold/phasing` separately from real pending updates.
- Added: APT status `updates_deferred` with semaphore level `2` when only phased/held updates remain.

## 0.8.20 - 2026-03-14
- Changed: agent now refreshes `apt_state` not only by timer but also immediately when local APT/DPKG state fingerprint changes.
  - Fingerprint includes dpkg status and apt sources/list timestamps.
  - Result: dashboard APT metrics converge faster after local `apt` operations (no long stale wait).
- Added: new MCC push config key `mcc.push_apt_state_interval_sec` (default `120` sec).
- Changed: `mcd-cli service-profile apply` now triggers immediate MCC state push on successful non-dry-run apply.
- Changed: `mcd-cli env ipv6 enable|disable` now triggers immediate MCC state push.

## 0.8.19 - 2026-03-13
- Fixed: `mcd-cli plugins` now resolves `plugins_repo_fallback_ip` from MCC runtime-overrides when local TOML does not define it.
  - This makes interactive/CLI plugin operations honor centralized MCC fallback settings (not only daemon in-memory runtime).
  - Result: plugin manifest/package fetch fallback works in CLI mode on blocked FQDN source networks.

## 0.8.18 - 2026-03-13
- Added: plugin repo IP fallback as configuration parameter (`[plugins].repo_fallback_ip`).
  - Behavior: MCD first fetches manifest/packages via normal FQDN URL.
  - On HTTP/network failure, MCD retries once with DNS override (`host -> repo_fallback_ip`) while preserving original URL host.
  - No hardcoded origin IP in code; fallback is fully configurable per host.
- Added: runtime key support for centralized MCC control:
  - `plugins_repo_fallback_ip`
  - `plugins_repo_base_url`
- Added: example setting in `mcd-agent.system.example.toml` for operator-side tuning.

## 0.8.17 - 2026-03-12
- Changed: `php_fpm` service-profile apply now also enforces a managed global PHP ini baseline (`98-mcd-php.ini`) for both FPM and CLI.
  - Adds explicit tuning for `memory_limit`, timeouts, input vars, post/upload sizes, realpath cache, output buffering.
  - Keeps rollback safety: all written files are restored on failed validate/reload.

## 0.8.16 - 2026-03-12
- Changed: in `state_backend=mysql_hybrid`, scheduler task state is now primary in MySQL (`tasks`, `manual_requests`, `weight_cache`, `runtime_sync`).
  - SQLite remains local failover storage only (running/pending minimum), not primary history.
- Added: one-time bootstrap migration from local SQLite into MySQL state tables on first successful MySQL start.
  - Existing task/runtime rows are copied to MySQL with id-preserving upsert.
  - After successful migration, SQLite is pruned to failover-only footprint.
- Changed: CLI running-task views now read through `TaskStore` backend logic, so output matches effective backend (MySQL or SQLite fallback).

## 0.8.15 - 2026-03-12
- Fixed: campaign shared-cap round-robin no longer gets stuck in rebuild-only mode.
  - Root cause: round-robin counter update depended on runtime trigger limits *after* cap/prefer filtering.
    In `campaign_total_parallel=1` mode, rebuild-preferred tick zeroed trigger limits and stopped counter advancement.
  - Impact: scheduler could continuously spawn `campaign_rebuild` and almost never run `campaign_trigger`
    until daemon restart.
  - Result: trigger/rebuild alternation remains stable for shared-cap profiles (`tiny`/`mini` customizations).

## 0.8.14 - 2026-03-12
- Fixed: `state-db status` now evaluates effective runtime state (local config + MCC desired runtime overrides), not only raw local file values.
- Result: CLI `state-db` status is aligned with real daemon mode after MCC-driven `state_backend/state_mysql_*` runtime sync.

## 0.8.13 - 2026-03-12
- Fixed: state DB bootstrap now pushes runtime keys to MCC desired runtime map (not observed snapshot), so host converges to `mysql_hybrid` after successful init.
- Added: `mcd-cli runtime-overrides push --target observed|desired` for explicit destination control (default: `observed`).

## 0.8.12 - 2026-03-12
- Fixed: interactive `Environment -> Bootstrap State DB` now handles wrong DB admin password explicitly.
  - On auth failure, CLI shows immediate clear error (`DB auth failed...`) and offers password retry loop.
  - Operator can retry password without leaving menu or re-entering host/port/user fields.

## 0.8.11 - 2026-03-12
- Fixed: state DB bootstrap now also syncs `state_backend/state_mysql_*` runtime keys to MCC as host runtime overrides (`merge=true`) and triggers immediate runtime poll.
  - Prevents MCC from reverting host back to legacy `root`/no-password state values after successful local bootstrap.
  - Result: after root-password bootstrap, host reliably converges to MySQL mode in dashboard.

## 0.8.10 - 2026-03-11
- Fixed: `Environment -> Bootstrap State DB` is now available for legacy hosts when state DB is missing or inaccessible (including access-denied cases), not only `unknown database`.
- Changed: bootstrap flow now uses temporary admin credentials only for initialization.
  - Root/admin password is never persisted.
  - Agent creates dedicated runtime DB user (`mcd_state`) with minimal privileges on state DB.
  - Runtime config is switched to `mysql_hybrid` and stores only state DB runtime credentials.
- Added: `mcd-cli state-db init` now supports `--admin-unix-socket` and uses the same bootstrap flow as interactive menu.

## 0.8.9 - 2026-03-11
- Fixed: MySQL state backend now supports local unix-socket auth flow for passwordless local DB users.
  - Agent auto-detects common local socket paths when `state_mysql_host` is local and password is empty.
  - Explicit `state.mysql_unix_socket` (or runtime `state_mysql_unix_socket`) is supported.
- Changed: state backend runtime keys are no longer blocked from MCC runtime hot-apply.
  - `state_backend` and `state_mysql_*` keys can now be pushed via MCC runtime overrides and applied immediately.
- Result: hosts can switch from `legacy` to `mysql` state mode without manual TOML edits when local DB auth is available.

## 0.8.8 - 2026-03-11
- Added: explicit state DB lifecycle controls in CLI (`mcd-cli state-db status|init`).
  - `init` is allowed only when `state.backend=mysql_hybrid` and the target state database is missing.
  - Supports admin credentials input (interactive prompt or `--admin-password-stdin`).
- Added: interactive hub Environment menu now shows state backend status and conditional action:
  - `State Backend Status`
  - `Create State DB (admin credentials)` only for missing DB case.
- Changed: agent state push now always includes `state_backend` probe result so MCC can display effective mode (`mysql` vs `legacy`) and init errors.

## 0.8.7 - 2026-03-11
- Added: general DB-backed state mode for all installations (`[state].backend = "mysql_hybrid"`).
  - Outbound profile events are written to MySQL/MariaDB first (host-scoped rows).
  - Agent uses a dedicated state DB (`state.mysql_database`, default `mcd_state`) and auto-creates it if missing.
  - Local SQLite outbound queue remains as fallback when shared DB is unavailable.
- Added: latest MCC payload snapshot upsert to shared state DB (optional).
  - Sensitive fields are masked (`password/token/secret`), raw `config_state.toml` is omitted.
  - Snapshot push result is tracked (`sent/failed`) in shared state table.
- Added: state backend runtime keys to config model and runtime map.
  - These keys are blocked from remote hot-apply for process safety (require restart).
- Docs: state backend is documented as general mode (not cluster-only), with cluster replication as optional bonus.

## 0.8.6 - 2026-03-10
- Fixed: orphan `segment` tasks no longer stick to wrong PID after PID reuse.
  - Root cause: task signature check matched only generic tokens (`bin/console` + `mautic:*`) and ignored per-entity identity.
  - Impact: after daemon restart, an old running row (for example `-i 142`) could be falsely considered alive when OS reused PID for another `segments:update` process, keeping priority slots blocked for hours.
  - New behavior: PID command signature now includes entity selector tokens (`-i/--id/--list-id/--campaign-id/--segment-id` and value), so mismatch is detected and stale rows are marked `lost`.
  - Result: stuck priority slots are released correctly and stale segments can continue cycling through rings.
- Fixed: dual-ring segment scheduler now prevents stale starvation when priority slots are occupied by very long-running tasks.
  - New behavior: if priority ring has backlog and at least one priority worker is running longer than `2h`, scheduler temporarily borrows regular launch slot for priority backlog (`3+1 -> 4+0` for new launches only).
  - Result: stale/priority segments continue to enter rings even while heavy segments are still executing.

## 0.8.5 - 2026-03-09
- Added: bounded retention for delivered outbound profile events.
  - New runtime key `outbound_events_sent_keep_days` (default `14`).
  - Daemon prunes old `sent` profile events in quiet window together with task compaction cycle.
- Added: periodic cleanup for local custom-scripts cache.
  - New runtime/system keys:
    - `custom_cache_cleanup_enabled`
    - `custom_cache_cleanup_interval_sec`
    - `custom_cache_cleanup_quiet_hour`
    - `custom_cache_cleanup_quiet_window_min`
    - `custom_logs_keep_days`
    - `custom_logs_max_files`
    - `custom_downloads_keep_days`
    - `custom_downloads_max_entries`
  - Cleanup policy removes stale logs/downloads by age and hard-cap limits, and prunes downloads for keys missing from current custom manifest.

## 0.8.4 - 2026-03-09
- Added: runtime keys for template workflow:
  - `runtime.host_template` (mark host as template source),
  - `runtime.template_autopromote_on_clone` (auto-promote clone to normal host identity).
- Added: agent identity resolver for clone-safe MCC calls.
  - On detected clone, agent switches `hostname` to local OS hostname and clears `mcc_host_name` in API payload.
  - Payload now includes `template_state`, `agent_hostname`, and `configured_host_name`.
- Changed: runtime-overrides, service-profile fetch, self-update check/release and state push now use unified identity resolution.

## 0.8.3 - 2026-03-09
- Changed: custom scripts now support manifest flag `interactive` and run in foreground by default for such entries.
  - Foreground execution uses live stdout/stderr stream and supports interactive stdin prompts.
  - Detached launch (`tmux`/`screen`) remains available explicitly.
- Changed: `mcd-cli custom` detach control switched to `--detach|--no-detach` (auto by default).
  - Auto mode now respects manifest intent: interactive scripts default to foreground.
- Changed: interactive menu `Custom Scripts` detach prompt is now profile-aware.
  - For interactive scripts, default answer is foreground (`[y/N]` for detach).
- Changed: custom scripts list output now shows `interactive=yes|no`.

## 0.8.2 - 2026-03-09
- Added: service-profile component `apt` (MCC-driven, hardware-aware).
  - New fetch/apply support in `mcd-cli service-profile --component apt`.
  - Daemon auto-apply loop accepts `apt` in `service_profiles_components`.
- Added: APT profile executor with built-in repair primitives:
  - optional cleanup for `third-party.sources`,
  - optional dedupe for `.list` source entries,
  - optional `mariadb_repo_setup` rebootstrap,
  - optional Ondrej PPA ensure hooks (`php`, `nginx`, `apache2`),
  - optional package presence/absence and upgrade mode controls.
- Added: APT state push in standard `/api/v1/agent/state` payload (`apt_state`) with:
  - `pending_updates`,
  - `error_count`,
  - duplicate source detection summary.

## 0.8.1 - 2026-03-08
- Fixed: `Custom Scripts` menu no longer fails hard on MCC `404` for manifest.
  - Behavior now treats missing manifest as an empty catalog (`No custom scripts`) and caches empty manifest locally.
  - Result: fresh MCC setup (before first custom publish) keeps interactive menu usable.
- Fixed: custom repo base URL resolution now prefers plugin/static repo URL before MCC API URL.
  - Root cause: when `mcc.url` pointed to API backend (e.g. `:18080`), custom manifest path resolved against API origin and returned `404`.
  - Result: custom manifest fetch uses static repo origin consistently with plugin repo behavior.

## 0.8.0 - 2026-03-08
- Added: centralized custom script execution from MCC repository.
  - New command: `mcd-cli custom [--list] [--json] [--no-detach] [<script_key_or_name>] [-- <args...>]`.
  - Interactive hub now includes `Custom Scripts` menu with manifest-driven list and grouped labels.
  - Scripts are resolved by manifest key (with display-name fallback) and never by local filename.
- Added: detached custom script runtime with operator-safe behavior.
  - MCD prefers `tmux` for long scripts, falls back to `screen`, then direct foreground execution.
  - Downloaded scripts are cached locally and verified by `sha256` from manifest.
- Added: custom manifest prefetch on daemon startup.
  - MCD fetches manifest from MCC and uses local cache fallback when MCC is temporarily unreachable.
- Added: custom script runtime config section (`[custom]`) with defaults:
  - repo URL/path, cache directory, default run mode, tmux/screen preferences, and session prefix.

## 0.7.21 - 2026-03-08
- Fixed: segment scheduler now reuses regular slot(s) for priority ring when regular ring is empty.
  - Root cause: strict split guard blocked priority spill, so `midi` could stay at `3` running segments instead of target `4` (`3+1`) when all segments were classified into priority.
  - New behavior: if regular ring has no launch candidates, MCD borrows missing slot(s) for priority to keep total segment parallelism at configured limit.

## 0.7.20 - 2026-03-07
- Changed: profile-event delivery queue moved from pending file to SQLite (`state_db`) outbound events table.
  - Added durable queue table `outbound_events` with event status (`pending|failed|sent`), retries and timestamps.
  - Added one-time automatic migration of legacy `profile-event.pending.json` into SQLite queue.
- Changed: profile-event delivery now records send result.
  - Failed send attempts increment retry counters and keep event queued.
  - Successful delivery marks event as `sent` instead of deleting it immediately.
- Changed: SQLite access for task/state paths now uses `WAL` journal mode and `busy_timeout=5000ms` to reduce lock contention between daemon and CLI.

## 0.7.19 - 2026-03-07
- Added: profile drift guard against MCC desired state.
  - Daemon periodically compares local active profile vs MCC `desired_profile`.
  - On unexpected drift, agent backs up local config and restores canonical config from MCC (`config-desired`), then reloads runtime without manual intervention.
- Added: profile change event queue on agent side.
  - `mcd-cli profile ...` now writes a pending profile event and pushes it to MCC.
  - Event delivery is retried automatically until successful push.
- Changed: state push now includes optional `profile_event`, and pending profile events force immediate push (even when normal change-push is disabled).

## 0.7.18 - 2026-03-07
- Added: self-update pre-switch smoke gate.
  - Agent now runs staged `compileall` + import smoke (`mcd_agent.backup`, `mcd_agent.self_update`, `mcd_agent.daemon`, `mcd_agent.cli`) before switching `/opt/mcd/src`.
  - Guard includes mandatory symbol check for `backup_lock_active`.
- Result: broken/partial release trees are rejected before source switch, preventing post-update crash loops.

## 0.7.17 - 2026-03-07
- Added: robust config load recovery path for broken/legacy host config during upgrades.
  - `load_config()` now performs pre-parse cleanup of known legacy runtime keys.
  - If config parse fails, agent can pull canonical desired config from MCC and recover automatically.
  - Broken local config is preserved as `mcd.toml.broken-<timestamp>` before rewrite.
- Added: CLI command `mcd-cli config-check` with optional `--repair-from-mcc` for explicit validation/recovery.
- Changed: runtime override reads now strip legacy runtime keys to prevent stale parallel settings re-activation after update.

## 0.7.16 - 2026-03-07
- Added: service-profile component `mysql` (MCC-driven, hardware-aware).
  - New fetch/apply support in `mcd-cli service-profile --component mysql`.
  - Daemon auto-apply loop now supports `mysql` in `service_profiles_components`.
- Added: safe MySQL profile apply with managed drop-in config file and rollback on restart failure.
  - Writes only managed drop-in (`99-mcd-hw.cnf` by default) instead of replacing base package config.
  - Supports both MySQL/Percona and MariaDB service names.
- Changed: default service-profile components for new configs are now `["php_fpm","mysql"]`.

## 0.7.15 - 2026-03-07
- Fixed: ring planner is now resilient to transient DB query failures.
  - Root cause: when `segments_due`/`campaigns_due` query failed, planner rebuilt rings from empty lists and effectively dropped pending entities from current cycle.
  - New behavior: on query failure, scheduler preserves previously planned segment/campaign rings and continues dispatch from existing circles.
  - Result: one blocked/limited DB query no longer clears rings or stalls cycle progression.
- Fixed: planner DB connections now use bounded network timeouts.
  - Added `connect_timeout=5s`, `read_timeout=120s`, `write_timeout=30s` for MCD planning DB calls.
  - Result: a stuck DB socket/query can no longer freeze the daemon loop indefinitely.
- Changed: backup dump timeout is normalized to a safe bounded value.
  - `dump_timeout_sec <= 0` now resolves to `10800` seconds in effective config.
  - Added runtime override key `backup_dump_timeout_sec` (MCC dynamic runtime table).
  - Result: hung backup subprocess no longer keeps backup guard active forever.

## 0.7.13 - 2026-03-06
- Fixed: stale/ghost running tasks after daemon restart or PID reuse no longer block scheduler slots.
  - Root cause: orphan adoption and monitor path treated `pid alive` as sufficient, so a reused PID from another process could keep an old task in `running` state.
  - Added command-signature verification against `/proc/<pid>/cmdline` for adopted tasks and monitored orphan tasks.
  - On mismatch, task is marked `lost` with `pid_cmd_mismatch` and removed from running map.
  - Result: segment/campaign slot accounting recovers automatically from PID reuse artifacts and ring dispatch resumes.

## 0.7.11 - 2026-03-05
- Fixed: mydumper long-query-guard semantics in backup runner.
  - `backup_mydumper_long_query_guard <= 0` is now treated as "guard disabled".
  - Because mydumper interprets `--long-query-guard 0` as abort on any query `>0s`, MCD now maps disabled mode to a large safe value.
  - Applied in both primary and fallback mydumper command builders.
  - Result: default host config with `long_query_guard = 0` no longer fails immediately on busy databases.

## 0.7.10 - 2026-03-05
- Fixed: backup mydumper command now always passes `--long-query-guard` including explicit `0`.
  - Root cause: argument was emitted only for values `> 0`, so configured `0` was omitted and mydumper default guard (`60s`) was used.
  - Impact before fix: backup could fail on busy hosts with `There are queries in PROCESSLIST running longer than 60s, aborting dump` even when runtime intended to disable the guard.
  - Result: runtime/config value is applied deterministically in both primary and fallback mydumper command builders.

## 0.7.9 - 2026-03-04
- Added: manual one-shot commands are now scheduler-aware in active profiles (`exec` and shorthand forms).
  - Commands are enqueued to local `manual_requests` queue in agent SQLite state DB.
  - Daemon launches queued manual tasks immediately on next dispatch cycle.
  - Manual launches ignore ring slot limits for the launch moment (temporary extra slot).
  - After manual launch, scheduler waits until active counts return to profile formula before spawning new ring tasks.
  - Manual entity id is marked as executed for current cycle (ring position moved to tail), so it is not re-run immediately in same circle.
- Added: direct fallback for manual command if daemon does not pick queued request quickly (request is cancelled and command is executed directly).
- Added: task/request linkage (`manual_request_id`) for robust status propagation from running task monitor.
- Added: `exec` command now supports `--config` (same default behavior as other commands).

## 0.7.8 - 2026-03-04
- Added: native shorthand Mautic execution commands in `mcd-cli` (no `exec` wrapper required):
  - `mcd-cli segments:update [-i <id>] [--root <root|uid>]`
  - `mcd-cli campaign:trigger [-i <id>] [--root <root|uid>]`
  - `mcd-cli campaign:rebuild [-i <id>] [--root <root|uid>]`
  - `mcd-cli campaigns:update [-i <id>] [--root <root|uid>]`
  - `mcd-cli campaigns:trigger [-i <id>] [--root <root|uid>]`
  - `mcd-cli import [--root <root|uid>]`
- Added: shorthand commands support `--config` and automatic local instance root resolution when `--root` is omitted and host has a single known instance.
- Result: operators can run short commands directly on host and from MCC `host-run` passthrough with same syntax.

## 0.7.7 - 2026-03-04
- Fixed: backup scheduler helper regression in daemon loop.
  - Restored missing `_backup_done_for_local_date()` guard used by scheduled backup slot checks.
  - Root cause: helper call remained after refactor while function definition was removed.
  - Impact before fix: daemon crashed on startup (`NameError`) and entered systemd restart loop, blocking normal scheduler operation.

## 0.7.6 - 2026-03-04
- Fixed: dual-ring dispatch now rebinds freshly reconciled rings/sets in the same planning tick.
  - Root cause: scheduler used previous-cycle ring/set objects until next plan refresh.
  - Impact: `midi` could underfill segment regular slot and behave like `3` visible workers instead of stable `3+1`.

## 0.7.5 - 2026-03-04
- Changed: strict segment ring split for idle scheduler.
  - Removed priority spillover when regular ring has no candidates.
  - For `midi` this keeps fixed `3+1` behavior (no automatic `4+0` expansion).

## 0.7.4 - 2026-03-04
- Added: backup controls are now runtime-dynamic and can be changed live from MCC runtime table (no direct `mcd.toml` edits required):
  - `backup_enabled`
  - `backup_schedule_*`
  - `backup_mydumper_threads`
  - `backup_mydumper_long_query_guard`
  - `backup_mydumper_kill_long_queries`
  - `backup_mydumper_extra_args`
  - `backup_mydumper_use_nice`
  - `backup_mydumper_nice_level`
  - `backup_mydumper_use_ionice`
  - `backup_mydumper_ionice_class`
  - `backup_mydumper_ionice_level`

## 0.7.3 - 2026-03-03
- Changed: safer backup defaults for production (applies to new/clean configs):
  - `backup.mydumper.threads = 6`
  - `backup.mydumper.kill_long_queries = false`
  - `backup.mydumper.long_query_guard = 0`
- Added: backup dump process priority controls:
  - `backup.mydumper.use_nice = true`, `nice_level = 15`
  - `backup.mydumper.use_ionice = true`, `ionice_class = 2`, `ionice_level = 7`
  - mydumper execution is now wrapped with `ionice`/`nice` automatically when tools are available.
- Changed: transaction/lock defaults for mydumper are now capability-aware:
  - auto-add `--sync-thread-lock-mode=AUTO` when local mydumper supports it,
  - prefer `--trx-tables` on supported versions,
  - fallback to `--trx-consistency-only` on older versions.
  - explicit operator-provided lock/transaction flags in `backup.mydumper.extra_args` still take precedence.

## 0.7.2 - 2026-03-03
- Added: runtime-overrides control channel in `mcd-cli`:
  - `mcd-cli runtime-overrides show|fetch|push|trigger|status`
  - `trigger` creates a local poll flag for daemon (`/opt/mcd/var/runtime-overrides.poll`).
- Added: daemon-side runtime trigger handling:
  - daemon now consumes runtime trigger flag and performs immediate MCC runtime-overrides pull/apply (no restart).
- Added: runtime sync snapshots in local SQLite state DB (`runtime_sync` table):
  - `local_runtime` (parsed from config runtime section),
  - `mcc_runtime` (latest desired payload from MCC),
  - `active_runtime` (last applied runtime sync result metadata).
- Changed: state push payload now includes `runtime_overrides` (local observed runtime map), enabling MCC cache visibility without host polling.
- Changed: daemon now detects local mutable-runtime config changes and pushes them immediately to MCC (`replace` mode), so MCD-side edits become visible in MCC without waiting for periodic state push.

## 0.7.1 - 2026-03-03
- Changed: segment priority-ring order is now deterministic by business priority, not by id.
  - Order inside priority ring: whitelist first, then stale (>24h / never built), then by computed weight, then by id tie-break.
  - Regular ring order is now by computed weight (desc), then id.
- Changed: passive -> active profile switch now sanitizes legacy local config overrides.
  - Clears stale legacy runtime keys (`max_parallel_*`, `segment_non_whitelist_policy`) from mutable config.
  - Clears legacy SQL override keys (`segments_due`, `segment_weights`, `campaigns_due`, `import_pending_count`, `mail_queue_count`) so active profile uses current default scheduler SQL.
  - Result: hosts leaving passive mode converge faster to current profile defaults without keeping old config artifacts.
- Added: MCD-managed runtime-dynamic maintenance tasks:
  - `contacts_cleanup_mode` (`email_and_mobile` or `email_only`) for lead cleanup policy.
  - `enable_cache_clear` + schedule window (`cache_clear_*`) and command template.
  - `enable_cache_warm` + schedule window (`cache_warm_*`) and command template.
  - These knobs are runtime keys, so MCC runtime overrides can change behavior on-the-fly.
- Changed: cron manager now also marks these as managed when profile is active:
  - `doctrine:query:sql`
  - `cache:clear`
  - `cache:warm` / `cache:warmup`
  - `mautic:emails:send`

## 0.7.0 - 2026-03-03
- Added: MCC runtime-overrides pull/apply loop in daemon (dynamic host table, no local file rewrite).
  - MCD periodically fetches `POST /api/v1/agent/runtime-overrides` and applies supported runtime keys in-memory.
  - Effective behavior changes immediately (without service restart) for hot-safe runtime knobs.
  - Removing keys in MCC reverts host back to local base config values on next poll.
- Added: conditional Mautic 6 core patch triggers by version range:
  - `runtime.mautic6_core_patch_version_min`
  - `runtime.mautic6_core_patch_version_max`
  - `runtime.mautic6_core_patch_apply_if_version_unknown`
  - patch can now be auto-stopped by updating runtime trigger values from MCC (no code change).
- Added: remote runtime override normalization:
  - supports flat keys (`segment_regular_parallel_idle`),
  - `runtime.*` keys (`runtime.mautic6_core_patch_policy`),
  - nested payload form (`{"runtime": {...}}`).
- Added: safety guard for dynamic runtime table:
  - static/bootstrap-only keys are blocked from hot apply (`state_db_path`, `scheduler_pause_flag_path`, `php_bin`, `mautic_run_as_user`),
  - unsupported keys are ignored with explicit log entry.

## 0.6.18 - 2026-03-03
- Added: global Mautic 6 core hotfix watcher (independent from plugin flow).
  - Detects and patches `ReloadHelper.php` bug where `PluginUpdateEvent` receives `null` metadata instead of `array`.
  - Patch is idempotent and works for both layouts:
    - zip: `<root>/app/bundles/.../ReloadHelper.php`
    - composer: `<root>/docroot/app/bundles/.../ReloadHelper.php` (and `public/` variant)
  - Daemon now checks/apply this hotfix on every planning cycle, so if file is overwritten by Mautic update, patch is re-applied automatically.
- Added: manual Mautic 6 core patch controls in CLI and interactive menu.
  - New command: `mcd-cli mautic6-patch status|apply|revert|policy`.
  - Interactive menu item: `Mautic6 Core Patch`.
  - Policy supports fixed daemon behavior:
    - `required` (auto-patch always),
    - `off` (never auto-patch).
- Changed: plugin post-step fallback for `metadata=null` error remains as a safety net, but primary fix is now global Mautic 6 core patching.

## 0.6.17 - 2026-03-03
- Added: immediate MCC state push after successful mutating `mcd-cli` operations.
  - Triggers on: `plugins`, `mautic-upgrade` (apply/interactive), `instances` add/remove/rescan, `reload-config`, `profile set`, backup ops (`run/prune/restore/profile-set`).
  - Push path uses the same `/api/v1/agent/state` payload model as daemon, but does **not** force a fresh signals poll.
- Changed: on-demand push helper now supports payload build without `signals`, so semaphore telemetry remains on periodic routine push only.

## 0.6.16 - 2026-03-03
- Added: MCD state push now includes per-instance `mautic_version` in payload.
  - Version is resolved locally from instance root with composer/docroot-aware candidate roots.
  - Detection path: `bin/console --version` / `about` fallback, then `composer.lock` fallback.
- Result: after local Mautic upgrade (including interactive `mcd-cli` flow), version change is propagated to MCC via normal push path without manual rescan.

## 0.6.15 - 2026-03-03
- Changed: self-update is now backup-lock aware and never starts while host backup lock is active.
  - Daemon checks backup lock before every auto-update cycle and defers update check to the next scheduler tick.
  - Added explicit defer/resume logs:
    - `auto-update deferred: backup lock active; retry on next cycle`
    - `auto-update defer cleared: backup lock released`
- Changed: `maybe_auto_update()` now also has an internal backup-lock guard, so direct calls are deferred safely as well.

## 0.6.14 - 2026-03-03
- Changed: backup scheduler now applies a global pre-backup dispatch guard for all new Mautic tasks.
  - New config key: `[backup.schedule].pre_pause_sec` (default `3600`).
  - During pre-backup window and while backup lock is active, daemon does not start new tasks:
    - segments
    - campaigns (trigger/rebuild)
    - imports
    - scheduled jobs
  - Already running tasks are not killed by backup guard.
  - Dispatch resumes automatically when backup run finishes (success or failure).
- Changed: backup profile payload pushed to MCC now includes schedule key `pre_pause_sec`.

## 0.6.13 - 2026-03-02
- Changed: `mautic-upgrade` now performs patch-only upgrades inside current branch (`X.Y.x`) by default.
  - Major/minor jumps are blocked in current flow.
  - Upgrade target is resolved from MCC release cache (`/api/v1/agent/mautic/releases`) with safe local fallback.
- Changed: interactive upgrade target menu is simplified to branch patch update only.
- Changed: composer upgrade flow is hardened:
  - resolves composer project root (`root`/`root/..`) before running composer,
  - performs bounded version-token replacement (`current -> target`) before `composer update --with-dependencies`.
- Changed: upgrade flow keeps environment/system upgrade recipe optional; default patch upgrade path no longer mixes distribution upgrade with PHP/system migration.

## 0.6.12 - 2026-03-02
- Changed: service-profile PHP target resolution is now runtime-aware:
  - prefers currently running `php-fpm` version when multiple PHP versions are installed,
  - falls back to latest installed version that has both `fpm` and `cli` trees.
- Changed: `php-fpm` service name is now resolved dynamically (`phpX.Y-fpm`/fallback), reducing reload mismatches after PHP upgrades.
- Changed: signal collector no longer scans hardcoded PHP-FPM unit list (`php8.0..8.4`) on every cycle.
  - now detects active/installed `php*-fpm.service` units from systemd and reads only those journals.
  - reduces unnecessary `journalctl` calls and log noise on hosts with a single installed PHP version.
- Added: service-profile now manages Redis session overrides for both FPM and CLI:
  - `/etc/php/<ver>/fpm/conf.d/90-redis-sessions.ini`
  - `/etc/php/<ver>/cli/conf.d/90-redis-sessions.ini`
- Added: Redis session settings are profile-driven with safe defaults (`127.0.0.1:6379`, DB 10, locking enabled) and participate in rollback logic on apply failures.
- Result: after Mautic/PHP version upgrades, MCD applies the same tuning set (pool/opcache/redis/sysctl) to the active PHP version automatically.

## 0.6.11 - 2026-03-02
- Fixed: self-update now installs staged runtime dependencies before source switch:
  - runs `/opt/mcd/venv/bin/python -m pip install -r <staged>/requirements.txt`,
  - executes from stable working directory (`cwd=/`) to avoid deleted-CWD failures,
  - aborts update before switch on dependency install error.
- Added: startup dependency bootstrap in `mcd_agent.__main__`:
  - on `ModuleNotFoundError` during CLI import after update, agent auto-runs `pip install -r requirements.txt` once and retries import.
- Result: host updates no longer require manual dependency fixes after self-update; dependency handling is global and automatic.

## 0.6.10 - 2026-03-02
- Changed: service-profile fetch payload now sends both host identities:
  - `hostname` (OS hostname),
  - `mcc_host_name` (explicit MCC inventory host name from config, if set).
- Result: MCC can resolve host identity more reliably when inventory name differs from system hostname.

## 0.6.9 - 2026-03-02
- Fixed: release packaging alignment for dynamic service profiles build.
- Result: published test package version and internal agent `__version__` are consistent for MCC self-update flow.

## 0.6.8 - 2026-03-02
- Added: MCC-driven dynamic service profile apply path for host tuning (first component: `php-fpm`).
  - New agent command: `mcd-cli service-profile status|fetch|apply --component php_fpm`.
  - New daemon auto-apply loop controlled by runtime keys:
    - `service_profiles_enabled`
    - `service_profiles_auto_apply`
    - `service_profiles_poll_interval_sec`
    - `service_profiles_components`
- Added: safe `php-fpm` apply mechanics with rollback:
  - writes drop-in files under `/etc/php/<ver>/...` (`zz-mcd-hw.conf`, `99-mcd-hw.ini`),
  - validates config with `php-fpm -tt`,
  - reloads/restarts `php<ver>-fpm`,
  - rolls back changed files on failure.
- Added: host-side sysctl alignment for profile payload (`net.core.somaxconn`) with rollback safety.
- Result: service tuning can now be changed dynamically on MCC without rebuilding MCD package.

## 0.6.7 - 2026-03-02
- Added: split-config safe deployment layout in MCC install playbook (enabled by default for new/explicit config uploads):
  - `/opt/mcd/etc/mcd.toml` is now entrypoint include file (`MCD_CONFIG_ENTRYPOINT v1`),
  - package defaults are loaded from `/opt/mcd/src/etc/mcd-agent.system.example.toml` and `/opt/mcd/src/etc/mcd-agent.operator.example.toml`,
  - host-local overrides are stored in `/opt/mcd/etc/mcd.local.toml`.
- Changed: profile/mode operations are split-layout aware:
  - `mcd-cli profile ...` now updates effective mutable config file (local override) when entrypoint mode is detected.
  - runtime override cleanup for profile baselines is applied to local override file in split mode.
- Result: package/code updates can safely overwrite stock defaults while host custom settings remain in external override file.

## 0.6.6 - 2026-03-02
- Changed: backup `mydumper` command now auto-adds `--trx-consistency-only` by default.
  - Applies to primary and fallback dump execution paths.
  - Auto-injection is skipped when operator explicitly sets `--sync-thread-lock-mode=...` in `backup.mydumper.extra_args`.
- Result: avoids unnecessary global-lock attempts and reduces lock-related warnings for standard Mautic (InnoDB) backups.

## 0.6.5 - 2026-03-02
- Fixed: stale backup directory cleanup (`.incomplete-*`) is now strict and verified.
  - Cleanup now uses Python `shutil.rmtree` with explicit failure detection.
  - If any stale incomplete backup directory cannot be removed, backup run fails immediately with a clear error message.
- Result: no silent leftover `.incomplete-*` directories between retries.

## 0.6.4 - 2026-03-02
- Fixed: backup `mydumper` execution flow no longer re-runs fallback dump when primary run produced valid dump files but exited non-zero (global-lock warning path).
- Fixed: backup now accepts a completed/verified dump directory after non-zero `mydumper` exit and proceeds to finalize (`.incomplete-*` -> `YYYY-MM-DD`) instead of failing.
- Result: if DB dump is actually complete, host backup is finalized as `ok` and folder naming is correct.

## 0.6.3 - 2026-03-01
- Fixed: backup scheduler now prevents duplicate daily runs inside the same quiet-window slot.
  - If a successful backup for current local date already exists, scheduler skips launch.
- Fixed: `backup_run` is now idempotent for the current date.
  - If target date directory already exists with successful marker, run returns `ok` with `ok_skip_existing` history instead of starting a new dump.
- Result: no immediate re-run in the same `YYYY-MM-DD` backup path after successful completion.

## 0.6.2 - 2026-03-01
- Changed: backup run now performs automatic cleanup of stale `/.incomplete-*` directories in remote host backup path before creating a new backup.
- Changed: failed backup run now removes current temporary `/.incomplete-*` directory instead of leaving partial artifacts.
- Result: retry backup starts from clean state by default; failed temporary backup payload does not accumulate.

## 0.6.1 - 2026-03-01
- Added: backup lock aware segment scheduler behavior.
  - While host backup/restore lock is active, MCD pauses only **new** segment launches.
  - Already running segment tasks are not interrupted and finish normally.
  - Campaign/import/other task dispatch stays independent.
- Added: daemon transition logs for backup-driven segment pause/resume.

## 0.6.0 - 2026-03-01
- Added: host-level full restore flow in backup module:
  - `mcd-cli backup restore [--date YYYY-MM-DD|--path ...]`
  - restores archived files to `/` and restores DB dumps via `myloader`.
- Added: encrypted backup profile vault in local MCD SQLite (`backup_profile` table), including migration-safe profile merge/set flow.
- Added: backup profile CLI operations:
  - `backup profile-show`
  - `backup profile-set --profile-json-file ...`
  - `backup profile-set --profile-json-stdin` (safe for shell history).
- Added: daemon backup scheduler (`[backup.schedule]`) with quiet-window + interval controls, independent of Mautic task dispatch.
- Added: agent state push now includes:
  - `backup_state` (last run/success/error/path/restore markers),
  - `backup_profile` payload for secure storage on MCC side.
- Changed: backup archive default scope now includes operational host paths required for full-state restore (`/etc/nginx`, `/etc/apache2`, `/etc/php`, `/etc/mysql`, `/etc/cron.d`, `/etc/systemd/system`, `/opt/mcd/etc`, `/var/www`, cron spool).

## 0.5.6 - 2026-03-01
- Changed: segment ring planner now forces stale segments into priority ring:
  - segment is considered stale when `last_built_date` is older than 24 hours, or missing.
  - stale rule is independent from normal weight threshold/top-N logic.
- Behavior: when regular ring becomes empty (for example on first runs after long inactivity), its slot is automatically reused by priority ring (`3+1` effectively becomes `4+0` for that cycle).
- Behavior: after stale segments are rebuilt, they stop matching stale rule and return to normal weight-based ring placement.

## 0.5.5 - 2026-02-28
- Changed: host self-update no longer runs `pip install` during apply.
- Changed: self-update now stages release source to `var/updates/src.next-*` and performs atomic source switch to `/opt/mcd/src` (no in-place delete of current working directory).
- Fixed: eliminated mass auto-update failures caused by `pip` startup in removed CWD (`FileNotFoundError: os.getcwd()` / `OSError: No such file or directory`).
- Rollback: on failure after switch, source is restored from pre-switch snapshot without dependency reinstall step.

## 0.5.4 - 2026-02-28
- Fixed: self-update archive extraction now ignores developer/runtime artifacts (`.venv*`, `__pycache__`, `.DS_Store`, cache dirs) before replacing `/opt/mcd/src`.
- Fixed: self-update source copy now preserves symlinks (`symlinks=True`) and no longer fails on broken dev symlinks from packaged virtual environments.
- Result: host auto-update applies cleanly from MCC packages without manual archive cleanup.

## 0.5.3 - 2026-02-28
- Added: bounded task-history retention for local SQLite state DB:
  - `runtime.tasks_history_keep_days`
  - `runtime.tasks_history_max_rows`
- Added: quiet-window state DB compaction controls:
  - `runtime.tasks_compact_enabled`
  - `runtime.tasks_compact_interval_sec`
  - `runtime.tasks_compact_quiet_hour`
  - `runtime.tasks_compact_quiet_window_min`
  - `runtime.tasks_compact_vacuum`
- Changed: daemon now runs periodic non-running task history prune and optional `VACUUM` (in quiet window), keeping only operationally required depth.
- Docs: updated system config example and README with state DB structure and retention/compaction behavior.

## 0.5.2 - 2026-02-28
- Fixed: self-update state persistence no longer loses `last_status`/`last_result` after apply attempt (post-apply state is re-read before writing next schedule timestamp).
- Changed: `config_customized` is now computed as effective deviation from selected profile baseline, not just presence of `[runtime]` keys.
- Result: operators see real profile state (not false `custom` due static runtime block), and self-update diagnostics reflect actual apply outcome.

## 0.5.1 - 2026-02-28
- Added: `mcd-cli --version` global flag to print installed agent version.
- Added: interactive hub header now shows running version (`MCD Interactive (vX.Y.Z)`).
- Changed: minimal patch release over 0.5.0 for operator visibility and verification.

## 0.5.0 - 2026-02-27
- Changed: designated as first production major update baseline for MCC-driven MCD self-update rollout.
- Changed: update workflow is now expected to be managed through MCC release catalog (`approved/test/lts`) with host-side MCD self-apply.
- Result: next MCD code updates can be published on MCC only, while hosts upgrade themselves by policy.

## 0.4.9 - 2026-02-27
- Added: MCD self-update flow through MCC API (`mcd-cli self-update check|apply|status`).
- Added: update policy model in runtime config:
  - `mcd_update_policy = off|lts|approved|test`
  - `mcd_update_allow_test_build`
  - `mcd_update_wait_retry_sec`
- Changed: default `mcd_auto_update_enabled` is now `true` (unless explicitly disabled).
- Added: daemon periodic self-update check/apply loop (`maybe_auto_update`) integrated into scheduler cycle.
- Added: local MCD config history snapshot file with retention (`mcd_config_history_limit`, default 10).
- Result: MCC can trigger update by command, while MCD performs upgrade locally and reports final state.

## 0.4.8 - 2026-02-27
- Added: `mcd-cli maintenance on|off|status` command for temporary maintenance mode without profile switching.
- Behavior:
  - `maintenance on` sets scheduler pause flag and (by default) stops running Mautic console tasks.
  - `maintenance on --kill-orphans` also stops orphan `bin/console mautic:*` processes not tracked in MCD task DB.
  - `maintenance off` removes pause flag only.
  - `maintenance status` reports paused state, tracked running tasks, and console process counters.
- Result: maintenance windows for DB operations can be started/stopped quickly without changing active profile.

## 0.4.7 - 2026-02-27
- Fixed: campaign SQL compatibility for Mautic 4 in scheduler loops.
- Changed: for Mautic 4 instances, agent now automatically strips `AND (c.deleted IS NULL)` from `sql.campaigns_due` and `sql.campaign_weights` at runtime.
- Result: campaign rings are built correctly on Mautic 4 schemas where `{prefix}campaigns.deleted` column does not exist.

## 0.4.6 - 2026-02-27
- Changed: CLI now auto-resolves default config path in this order:
  - `MCD_CONFIG` env var (if set)
  - `/opt/mcd/etc/mcd.toml`
  - `/etc/mcd/mcd.toml`
  - local repo example config (dev fallback)
- Changed: commands with `--config` now use the same auto-resolved default, including `profile`.
- Result: local profile switch no longer requires explicit config argument in standard installs (`mcd-cli profile passive --yes`).

## 0.4.5 - 2026-02-25
- Fixed: `tiny` profile now keeps import polling active; `mautic:import` is no longer skipped by campaign-chain branch.
- Fixed: default `sql.import_pending_count` now supports numeric import statuses (`1,2`) in addition to string statuses (`pending`, `in_progress`) for mixed Mautic schemas.
- Result: pending imports are detected and executed on tiny-profile hosts such as Alex.

## 0.4.4 - 2026-02-25
- Added: MCD push payload now includes `config_state` snapshot (`schema_version`, `customized`, `sha256`, full TOML text).
- Added: agent config metadata fields in runtime (`config_file_path`, `config_schema_version`, `config_customized`, `config_sha256`) for deterministic state export.
- Result: MCC can persist exact host config state and preserve behavior across frequent daemon code upgrades.

## 0.4.3 - 2026-02-25
- Added: environment policy task `web.cloudflare_real_ip` (plan-only) with Cloudflare CIDR template and `CF-Connecting-IP` header settings.
- Added: policy plan component selector `web_cf_real_ip` in `mcd-cli env policy plan`.
- Result: MCD now exposes Cloudflare real-IP nginx environment task in policy list without applying changes automatically.

## 0.4.2 - 2026-02-25
- Changed: default campaign selection logic is now strictly `is_published=1` (+ not deleted), without `publish_up/publish_down` window filtering.
- Changed: default `sql.campaigns_due` and `sql.campaign_weights` templates were updated to remove publish window constraints.
- Result: MCD campaign loops treat published campaigns as active by DB publish flag only.

## 0.4.1 - 2026-02-25
- Fixed: profile switching now removes profile-managed runtime override keys from `[runtime]` in `mcd.toml`.
- Result: named profiles (`passive|tiny|mini|midi|maxi|hiload`) are now deterministic and no longer inherit stale parallel/ring/throttle values from old manual runtime overrides.
- Changed: `mcd-cli profile passive` and `mcd-cli profile <named>` both enforce profile baseline cleanup before service restart.

## 0.4.0 - 2026-02-24
- Added: centralized environment policy scaffold (`version=1`) for host-level domains:
  - `apt`
  - `iptables`
  - `database` (MariaDB/MySQL)
  - `php` (php-fpm pool knobs)
  - `web` (nginx/apache high-level knobs)
- Added: `mcd-cli env policy show` to print default policy template.
- Added: `mcd-cli env policy plan` to render host-local execution plan from policy payload (`--policy-file|--policy-json|--policy-b64`, `--component`).
- Safety: policy workflow is plan-only in this release; no host configuration is applied by policy commands.

## 0.3.36 - 2026-02-23
- Fixed: web signal collection now includes nginx file logs (`/var/log/nginx/access.log*`, `/var/log/nginx/error.log*`) in addition to systemd journal.
- Added: `web_critical` signal counter (upstream/PHP web error patterns from nginx error log within selected window).
- Changed: web component level now accounts for both HTTP 5xx and critical nginx/web upstream errors.
- Result: MCC dashboard semaphore now reflects real 500/web incidents on hosts where nginx does not write to journald.

## 0.3.35 - 2026-02-22
- Fixed: plugin inventory now ignores invalid/non-bundle directory names (including nested `plugins` directory marker).
- Changed: plugin list and state push accept only valid bundle naming pattern (`*Bundle`) and skip service directories.
- Result: prevents phantom `plugins` bundle from appearing in MCC cache/dashboard.

## 0.3.34 - 2026-02-22
- Fixed: install type detection now supports composer layout where instance root contains `config/local.php` + `bin/console` and web root is `docroot`.
- Fixed: plugin operations now resolve plugin directory with layout-aware search order:
  - `<root>/plugins`
  - `<root>/docroot/plugins`
  - `<root>/public/plugins`
- Fixed: state push plugin inventory uses the same layout-aware plugin directory resolver.
- Result: composer instances now correctly report install type and installed plugins.

## 0.3.33 - 2026-02-22
- Fixed: autodiscovery domain/name selection now prefers `site_url` host from Mautic `local.php`, with web vhost `server_name` as fallback.
- Result: instance name/uid are aligned with actual Mautic canonical URL even when nginx/apache has extra aliases.

## 0.3.32 - 2026-02-22
- Fixed: autodiscovery candidate resolver now also checks two levels up from vhost root (covers additional composer/docroot layouts and symlinked webroots).
- Changed: autodiscovery instance `name` now prefers detected primary domain (when available), with root-name fallback.
- Result: better instance naming and more stable root resolution for composer installs.

## 0.3.31 - 2026-02-22
- Fixed: autodiscovery now resolves composer-style vhost roots (`docroot/public`) to effective Mautic project root automatically.
- Changed: discovery now checks candidate paths (`vhost root`, resolved path, parent) and accepts the first path where both `local.php` and console are found.
- Result: composer installs are discovered without manual instance config.

## 0.3.30 - 2026-02-22
- Fixed: Mautic 4 plugin workflow now applies transaction-safety patch regardless of selected bundle (not only when Hostnet is selected in current action).
- Fixed: added HostnetAuthBundle M4 compatibility patch for `plugins/HostnetAuthBundle/HostnetAuthBundle.php`:
  - removes fragile explicit transaction wrapper in install/update hook and executes idempotent schema query directly.
  - prevents `mautic:plugins:reload` failure `There is no active transaction` from Hostnet plugin hook.
- Changed: existing M4 Engine transaction guard patch remains and is applied opportunistically when needed.

## 0.3.29 - 2026-02-21
- Added: automatic HostnetAuthBundle compatibility patch for Mautic 4 in plugin workflow.
- Behavior: when HostnetAuthBundle is installed/updated, MCD patches `app/bundles/IntegrationsBundle/Migration/Engine.php` to guard `commit()/rollback()` by active transaction checks before running post-steps.
- Result: prevents `mautic:plugins:reload` failure `There is no active transaction` on affected Mautic 4 stacks.

## 0.3.28 - 2026-02-21
- Fixed: install type detection no longer relies on `composer.lock` (it exists in many zip/package installs and produced false `composer`).
- Added: shared install-type detector (`mcd_agent.install_type`) with conservative rules:
  - `composer` for documented `mautic/recommended-project` layout (`docroot/public` under composer project root).
  - default fallback is `zip`.
- Changed: `mcd state push` now sends install type from the new detector.
- Changed: `mautic-upgrade --mode auto` now uses the same detector (aligns upgrade mode with cached install type).

## 0.3.27 - 2026-02-21
- Changed: profile `maxi` campaign rebuild parallel changed to `2+1` (`campaign_rebuild_priority_parallel=2`, `campaign_rebuild_regular_parallel=1`).
- Changed: profile `hiload` campaign rebuild parallel changed to `3+1` (`campaign_rebuild_priority_parallel=3`, `campaign_rebuild_regular_parallel=1`).
- Docs: profile defaults in README/spec/operator examples synced to new rebuild values.

## 0.3.26 - 2026-02-21
- Changed: `mini` profile now uses shared campaign cap `campaign_total_parallel=1`.
- Result: `campaigns:trigger` and `campaigns:rebuild` in `mini` cannot exceed one concurrent campaign task in total.

## 0.3.25 - 2026-02-21
- Fixed: campaign publish-window SQL context now provides instance-local time (`{now_local}`) based on Mautic timezone from `local.php`.
- Changed: default `campaigns_due`/`campaign_weights` SQL templates now use `{now_local}` for publish window checks.
- Changed: operator/system config examples synced with current `tiny` profile (segments `1`, single campaign chain worker).

## 0.3.24 - 2026-02-21
- Fixed: `mcd-cli plugins` no longer loops forever in non-interactive mode when selection input is empty/EOF (`empty=back` now exits cleanly for non-TTY).
- Result: prevents runaway CPU from orphaned non-interactive plugin sessions.

## 0.3.23 - 2026-02-21
- Fixed: MCD state push now includes installed plugin inventory per instance (`plugins` with version from `plugins/*/Config/config.php`).
- Result: MCC cache/dashboard receives plugin changes from host side without manual MCC instance sync.

## 0.3.22 - 2026-02-21
- Added: `install_type` in pushed instance snapshot (`composer|zip`) for MCC cache dashboard.

## 0.3.21 - 2026-02-21
- Fixed: state push module import to inventory type (`MauticInstall`) for runtime compatibility.
- Changed: main deploy config (`mcd-agent.example.toml`) now includes `[mcc]` push settings to work even when split include files are not copied to target host.

## 0.3.20 - 2026-02-21
- Added: MCC push state loop in daemon.
- Added: periodic push every 5 minutes by default (`mcc.push_interval_sec = 300`).
- Added: out-of-band push on state snapshot change (`mcc.push_on_change = true`).
- Added: alert-driven checks from critical log signals (`mcc.push_alert_poll_interval_sec = 60`, window `5` min).
- Added: new MCC push options in `[mcc]` config (`push_enabled`, `push_*`, `host_name`).

## 0.3.19 - 2026-02-21
- Fixed: `signals` module now uses `timezone.utc` (Python 3.10 compatible) instead of `datetime.UTC`.

## 0.3.18 - 2026-02-21
- Added: lightweight critical host signal collector command: `mcd-cli signals [--window-min N] [--json]`.
- Added: default critical signal set (last window only): `oom_kill`, `mysql_critical`, `php_fpm_max_children`, `http_5xx`.
- Added: bounded journal scan with short timeouts to keep host overhead low by default.

## 0.3.17 - 2026-02-20
- Changed: `tiny` profile now uses exactly `1` segment worker (`segment_regular_parallel_idle=1`).
- Changed: `tiny` campaign scheduler is now single-worker chain mode: `campaigns:rebuild -i <id>` then `campaigns:trigger -i <id>` for the same ID.
- Changed: `tiny` campaign ring uses published campaigns in newest-first order and iterates in a plain cycle (single ring, no whitelist, no priority rings).

## 0.3.16 - 2026-02-20
- Changed: removed `mcd-cli mode ...` command from public CLI.
- Changed: only `mcd-cli profile ...` remains for passive/active-profile switching and status.

## 0.3.15 - 2026-02-20
- Changed: operator-facing state switch is now `profile` only (`mcd-cli profile ...`).
- Changed: `profile status` output no longer prints duplicate `mode=...`; only `profile=...` is shown.

## 0.3.14 - 2026-02-20
- Changed: default deployment profile in example configs is now `passive` for new hosts.
- Added: explicit `[profile].name = "passive"` override in `mcd-agent.example.toml` to keep passive-by-default behavior stable.

## 0.3.13 - 2026-02-19
- Changed: backup module is host-level by design (one run backs up all discovered instance databases with DB creds + optional system archive).
- Changed: backup state file path is now host-scoped (`/opt/mcd/var/state/backup/host-<host>.json`).
- Added: backup config option `[backup].host_name` for explicit remote host folder naming.

## 0.3.12 - 2026-02-19
- Added: new backup module (`mcd-cli backup run|status|history|prune`) for direct remote host-level backups via `sshfs + mydumper`.
- Added: backup semaphores/state in local JSON (`last_status`, `last_success_at`, `last_error`, `last_backup_path`, `history`) per host.
- Added: backup completion marker `.mcd-backup.json` in remote backup folder.
- Added: backup config sections in system config example: `[backup]`, `[backup.storage]`, `[backup.archive]`, `[backup.mydumper]`.
- Added: interactive menu section `Backup` with run/status/prune actions for active instance.

## 0.3.11 - 2026-02-19
- Fixed: campaign time-window SQL now uses daemon UTC placeholder (`{now_utc}`) to avoid local DB clock skew in publish window checks.
- Fixed: `campaign_trigger` and `campaign_rebuild` now keep independent ring cursors, so one loop does not advance the other.
- Fixed: ring dispatch no longer rotates queue on failed spawn attempt (busy slot), preventing repeated lock on the same campaign ID.
- Fixed: single-ring mode preserves source queue order (no forced `sorted(...)`), so scheduler respects SQL/ring ordering.

## 0.3.10 - 2026-02-19
- Fixed: campaign scheduler fairness with `campaign_total_parallel` cap.
- In shared-cap mode (e.g. `tiny` with total=1), trigger/rebuild now alternate dispatch priority to prevent rebuild starvation.
- Result: new active campaigns are not blocked by long trigger-only loop when rebuild pass is required first.

## 0.3.9 - 2026-02-19
- Fixed: `mcd-cli exec --command segments:update --instance-id N` now applies `-i N` correctly.
- Changed: `exec` id-based routing now consistently supports entity id for segment/campaign command family.

## 0.3.8 - 2026-02-19
- Added: new `passive` profile (planning/statistics mode, no Mautic task dispatch).
- Changed: `mode active|passive` is now profile-based:
  - `mode passive`: sets profile to `passive`, restores cron, restarts `mcd`.
  - `mode active`: restores previous non-passive profile (or `tiny` fallback), comments managed cron, restarts `mcd`.
- Changed: scheduler no longer depends on `scheduler.pause` as primary control; profile `passive` controls planning-only behavior.
- Added: `campaign_total_parallel` runtime cap; in `tiny` profile campaign workers (`trigger` + `rebuild`) share one total slot.

## 0.3.7 - 2026-02-19
- Fixed profile matrix defaults for one-ring hosts:
  - `tiny`: single ring, `segments=2`, `campaign_trigger=1`, `campaign_rebuild=1`, `campaign_update=0`.
  - `mini`: single ring, `segments=4`, `campaign_trigger=2`, `campaign_rebuild=1`, `campaign_update=0`.
- Changed: all named profiles now keep `campaign_update_* = 0` because `campaigns:update` is alias to `campaigns:rebuild` and is not scheduled separately.

## 0.3.6 - 2026-02-19
- Changed: scheduler no longer runs a separate `campaigns:update` loop; `campaigns:update` is treated as synonym of `campaigns:rebuild`.
- Fixed: removed duplicate campaign pre-processing passes (`update` + `rebuild`) that could overrun campaign worker slots.
- Changed: `mcd-cli exec --command campaigns:update` now executes rebuild-equivalent command for backward compatibility.

## 0.3.5 - 2026-02-18
- Changed: plugin list modes (`--list-available`, `--list-installed`) now output clean list views without interactive status table noise.

## 0.3.4 - 2026-02-18
- Fixed: `plugins --list-available` and `--list-installed` now work without `--root` on multi-instance hosts (iterates all instances).
- Clarified install behavior: `plugins --action install` force-installs/replaces selected bundles regardless of current state.

## 0.3.3 - 2026-02-18
- Fixed: `mcd-cli` help output now uses proper program name (`mcd-cli`) instead of `__main__.py`.
- Added: `plugin` alias for `plugins` command (`mcd-cli plugin ...`).
- Added: plugin list modes `--list-available` and `--list-installed`.
- Changed: `plugins --action install` now force-installs/replaces selected plugins regardless of prior state.
- Added: `--action /?` convenience handling (shows plugin command help).

## 0.3.2 - 2026-02-18
- Added: `mcd-cli` help aliases `/?` and `-?` at root and subcommand levels.
- Example: `mcd-cli /?`, `mcd-cli instances /?`, `mcd-cli plugins /?`.

## 0.3.1 - 2026-02-18
- Fixed: dataclass field order regression in `MauticInstall` after domain uid changes.

## 0.3.0 - 2026-02-18
- Added: instance uid strategy based on active web server domain (`nginx/apache sites-enabled`) with deterministic short collision suffix.
- Changed: Mautic autodiscovery now uses active web roots first and validates Mautic by specific files (`app/config/local.php` or `config/local.php`) plus console path.
- Added: environment operations for IPv6 (`mcd-cli env ipv6 status|disable|enable`) with persistent `/etc/sysctl.d/99-disable-ipv6.conf`.
- Added: interactive menu section `Environment` with IPv6 status/disable/enable.

## 0.8.115 - 2026-04-25
- Fixed: `mcd-cli mautic-locks:cleanup --json` now serializes stale lock rows safely instead of crashing on datetime values.

## 0.8.114 - 2026-04-25
- Fixed: mysql-hybrid scheduler shadow now resyncs SQLite failover rows from the authoritative MySQL task table so stale phantom running tasks stop accumulating in host signals.
- Added: automatic stale Mautic `checked_out` lock cleanup for segments/campaigns with conservative quiet-window, age, and backup-guard controls.
- Added: `mcd-cli mautic-locks:cleanup` for safe one-shot cleanup of stale Mautic locks on one or all local instances.

## 0.2.7 - 2026-02-18
- Added: stable short `instance_uid` for each Mautic instance (derived from install root).
- Changed: `mcd-cli instances list` now prints `uid=...` for every instance.
- Changed: instance remove/select now accepts `uid` in addition to name/root.
- Changed: MCC `host-run --instance` now accepts instance uid from MCD inventory.

## 0.2.6 - 2026-02-18
- Added: plugin manifest `pre_sql` hooks execution before `mautic:plugin:install`.
- Added: DB SQL template execution method with `{prefix}` rendering for plugin pre-fixes.
- Fixed: plugins error hint is now shown only for repo/manifest configuration errors.

## 0.2.5 - 2026-02-18
- Fixed: plugins menu no longer appears to hang on manifest load; added visible progress line and stricter network timeout/error message.
- Fixed: plugin integrity check no longer uses potentially heavy recursive glob; replaced with bounded non-following symlink scan to avoid long stalls.
- Ops note (team): part of the observed "manifest hang" was host networking (IPv6 path/connectivity), not only MCD logic.

## 0.2.4 - 2026-02-18
- Fixed: plugins menu now works out-of-the-box even if `plugins.repo_base_url` and `mcc.url` are not set explicitly.
- Changed: default plugin repository base URL in config loader is `https://servercontrol.sales-snap.com`.

## 0.2.3 - 2026-02-18
- Fixed: interactive mode with multiple instances now requires a valid active instance selection and no longer gets stuck in non-working state.
- Fixed: active instance selection flow is strict when multiple installs exist (cannot silently continue with empty selection).

## 0.2.2 - 2026-02-18
- Fixed: interactive menu no longer crashes with traceback when plugin repo is not configured; shows user-friendly error and returns to menu.
- Fixed: protected interactive operations (`Plugins`, `Mautic Upgrade`, `Cache`, `Instances` actions) with error handling.
- Changed: plugin HTTP user-agent now uses current MCD version dynamically.
- Process rule: patch version is incremented on each delivered change batch.

## 0.2.1 - 2026-02-18
- Added: active instance selection model in interactive menu; operations run against one selected instance.
- Added: upgrade targets selection (`next step`, `latest in current major`, `latest known major`).
- Added: support for local upgrade packages from `/opt/mcd/cache/updates/*-update.zip`.
- Added: passive/active mode switching with cron backup/comment/restore workflow.
- Added: uninstall command with cron rollback support.
- Fixed: instance inventory collision when multiple installs had same folder name (`public_html`) by switching uniqueness to `root`.
- Added: version update check settings (`mcd_update_notify`, `mcd_auto_update_enabled`, `mcd_update_check_interval_sec`, `mcd_update_channel`, `mcc.mcd_manifest_url`) with notify-only default behavior.
