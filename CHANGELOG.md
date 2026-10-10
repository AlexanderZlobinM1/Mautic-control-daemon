## 1.5.71 - 2026-10-10

- Preserve the full fresh MCC instance desired-state snapshot during a local
  push; apply only field-level local deltas, so an instance key absent from a
  persisted local baseline is never cleared accidentally. Explicit local
  removal clears only the key that was present in that baseline, while direct
  local additions are sent with unrelated remote instance fields intact.

## 1.5.70 - 2026-10-10

- Accept both stable local instance UIDs and MCC host-qualified aliases on
  the wire; collapse legacy/base and qualified rows to the exact current
  desired revision before normalizing rings and acknowledging it.

## 1.5.69 - 2026-10-10

- Keep bounded segment retries profile-driven: the approved default is two
  retries after the initial start (three total starts), and a terminal Mautic
  non-zero exit immediately releases the cycle to the next eligible item.
- Persist the local runtime baseline across daemon restart and, after a
  desired-state revision conflict, fetch the latest MCC state, rebase only
  the local delta, preserve unrelated host/instance keys, and retry once.
- Keep custom profiles autonomous without MCC while using the normal observed
  state push when MCC becomes available.

## 1.5.68 - 2026-10-10

- Translate MCC's canonical per-instance `segment_special_rings_v1` envelope
  into the supported priority and super-priority instance settings before
  applying it; preserve validated `slots_override` capacity and reject malformed
  envelopes instead of acknowledging dropped work.
- Acknowledge the exact desired-state revision only after the normalized
  runtime payload is accepted, with the effective translated payload recorded
  in the acknowledgement receipt.
- Preserve the canonical selected workload profile across every runtime sync
  and trigger, including a stale passive fallback on disk; an observed passive
  snapshot cannot demote an active selected profile, while an explicit desired
  passive selection remains valid.

## 1.5.67 - 2026-10-10

- Preserve the authoritative workload and hardware profile selection across
  self-update deploy/rollback, including the manual hardware-selection marker;
  source/package replacement never reselects a host profile.
- Replace the importable venv `mcd_agent` tree from the exact rollout source
  after pip metadata/script installation, and verify imported code, metadata,
  and source version agree on both deploy and rollback.
- Implement the missing `tasks-lifecycle` and `purge-failed` TaskStore
  operations with dry-run JSON, active-lock protection, and compressed
  terminal receipts before deletion.
- Add host-profile `segment_retry_count` (default `3` retries after initial,
  four maximum starts) and document that bounded Mautic non-zero exits are
  terminal item outcomes while the dispatcher continues the cycle.

## 1.5.66 - 2026-10-10

- Serialize all `TaskStore` SQLite/MySQL operations across scheduler,
  terminal-callback, and state-push threads, preventing shared-connection
  `sqlite3.InterfaceError` crashes and false `pid_lost` recovery after a
  concurrent state push.
- Add a long-cycle persistence regression across several minute ticks and a
  deterministic shared-connection concurrency regression.

## 1.5.65 - 2026-10-10

- Add persisted ordinary segment-cycle admission with deterministic never-built/changed/stale ordering, retry retention, and same-state manual admissions.
- Add canonical super-priority and priority segment lanes with dependency-aware claims, shared host capacity, terminal wake-up, and release-line compatibility guards.

## 1.5.64 - 2026-10-09

- Finalize the image-install bootstrap and cache-clear correction for the
  versioned Publish MCD candidate; preserve MCC-owned image admission and
  selected-PHP execution paths.

## 1.5.63 - 2026-10-09

- Keep the MCC Mautic compatibility matrix as the sole image-admission
  authority. The installer consumes the selected archive and performs only
  deployment lifecycle work; it no longer requires a serialized artifact or
  per-image compatibility payload, rewrites Composer guards, or blocks the
  install on a redundant post-extract bootstrap probe.

## 1.5.62 - 2026-10-09

- Collapse image admission to one MCC-owned Mautic compatibility matrix receipt:
  image identity/version/install type plus fresh host PHP/database facts. Remove
  the superseded MCD image-requirements v1-v3 evaluators and capability adverts.
- Keep artifact SHA-256 only as download integrity evidence. When an admitted
  image contains a disagreeing Composer-generated `platform_check.php`, replace
  only that derived guard and require a real Mautic bootstrap with the selected
  PHP before nginx, certificate, mail, or durable instance state can succeed.
- Use the selected per-install PHP executable throughout mail profile package,
  cache and test-message steps instead of the host-global PHP default.

## 1.5.61 - 2026-10-09

- Refine image compatibility into immutable v3 universal manifest semantics:
  the requirements envelope must declare at least one real constraint, while
  PHP, database, disk and resource categories are independently optional.
- Evaluate only explicitly declared image constraints. An unavailable fact for
  a declared constraint fails closed; an undeclared category adds no inferred
  requirement and never causes fabricated CPU, memory or disk policy.

## 1.5.60 - 2026-10-09

- Replace the image-compatibility v1 minima-only evaluator with an immutable
  v2 manifest-driven contract supporting PHP and MySQL/MariaDB lower/upper
  ranges, required PHP extensions, and disk/memory/CPU resource minima.
- Fail closed for missing, empty, invalid or unobservable image requirements;
  arbitrary future image names and versions are evaluated only from their
  declared requirements and fresh host facts, without Mautic-version policy.

## 1.5.59 - 2026-10-09

- Advertise the exact image-compatibility capability in both the top-level MCD
  host state and the preserved install-readiness envelope, so normal MCC state
  ingestion can retain the command, schemas, contract digest and probe policy.

## 1.5.58 - 2026-10-09

- Accept both canonical catalog digest representations in image compatibility
  requests: lowercase SHA-256 hex and `sha256:`-prefixed lowercase SHA-256.
  Responses retain the exact immutable digest references supplied by MCC.

## 1.5.57 - 2026-10-09

- Add the production host-side `image-compatibility` endpoint for MCC image
  candidates, with strict request validation, exact digest-bound responses and
  an advertised read-only capability in normal host state.
- Probe active PHP-FPM, authenticated MySQL/MariaDB server version, target free
  space and required host tools freshly for each request. Unknown or failed
  observations never produce `compatible=true`.

## 1.5.56 - 2026-10-09

- Add an explicit `tasks-lifecycle purge-failed` operator action. Without
  `--yes` it is an exact-ID dry-run; with `--yes` it transactionally deletes
  only unreferenced `state=failed` rows without creating an archive or receipt.
- Preserve timeout, lost/dead-letter, running and every non-failed state, and
  fail closed for live processes, runtime/active-plan or admission-lock
  references, an unreadable lock directory, a changed candidate set, or a
  changed deletion count.

## 1.5.55 - 2026-10-09

- Add a supported `tasks-lifecycle` dry-run/apply workflow that reports exact
  candidate IDs, writes a compact content-addressed terminal receipt archive
  and manifest, and deletes only the archived failed/timeout generation.
- Retain lost rows as dead-letter evidence and protect running, live-process,
  and runtime-referenced rows from lifecycle pruning.
- Reconcile stale admission metadata only after task/reference checks, with
  atomic identity validation and protected active PIDs; automatic compaction
  no longer removes lost, failed, or timeout evidence through its legacy path.

## 1.5.54 - 2026-10-08

- Treat the canonical special-ring loop as unready until its first
  authoritative startup snapshot, so an empty snapshot cannot consume the
  first aligned deadline.
- Publish the startup snapshot immediately after runtime authority sync and
  lazily load dependency metadata only for configured instances, independently
  of the slower all-instance planning pass.
- If authority becomes ready just after a boundary, perform one bounded
  catch-up and then resume the aligned minute schedule without backlog.

## 1.5.53 - 2026-10-08

- Fixed a process-level startup/replanning crash where ordinary segment lane
  locals could be read before binding, or leak from the previous instance,
  when a segment scan was skipped while restoring a persisted cycle.
- Isolated ordinary ring bindings per instance before all planning branches;
  this is host-, instance-, and segment-ID agnostic.
- Made the canonical special-ring minute loop survive and report unexpected
  tick exceptions instead of silently losing all future aligned ticks.

## 1.5.52 - 2026-10-08

- Prevent frequent scheduler snapshot notifications from starving the
  canonical special-ring minute loop: a wake at or after the aligned boundary
  now runs the due tick before scheduling the next boundary.
- Add a boundary regression proving pre-tick wakes only refresh state while a
  wake exactly at, or after, the minute boundary cannot skip dispatch.

## 1.5.51 - 2026-10-08

- Supersede the non-acceptable 1.5.50 release identity after two concurrent
  publication workflows produced different package bytes at the same URL.
  Runtime source is unchanged from the tested hot special-ring reconciliation
  fix; 1.5.51 restores one-version/one-artifact immutable provenance.

## 1.5.50 - 2026-10-08

- Reconcile a newly configured canonical special-ring closure out of every
  persisted and in-memory ordinary, SQL, follow-up and frozen-cycle structure
  before dispatch, preserving the remaining ordinary cursor and order.
- Prevent durable full-scan pending state from reintroducing special roots or
  covered dependencies after the planner has excluded them.
- Add the hot configuration-revision regression: special ownership arrives
  after an ordinary cycle was frozen, removes only that closure, and leaves
  independent ordinary members running without duplicate special launches.

## 1.5.49 - 2026-10-08

- Consume MCC's canonical per-instance `segment_special_rings_v1` contract as
  independent minute-tick Priority and Super-priority lanes, including typed
  ID/slot validation and legacy fallback without duplicate dispatch.
- Exclude configured roots and their complete dependency closures from
  ordinary and SQL rings. Priority remains profile/throttle bounded;
  Super-priority uses its fixed operator slot override.
- Deduplicate selected dependency roots into execution units. Mautic 7 uses a
  single native entry only when it covers every requested root and fails
  closed for unsafe overlapping siblings; Mautic 6 and older execute one
  deduplicated topological sequence per connected unit.
- Publish durable start/terminal/error observations for canonical special
  rings. Live Adonis metadata proves the current units are `{58}` and
  `{57,59}`; with one Super-priority slot they execute serially and segment 57
  is rebuilt only inside the native segment 59 invocation.

## 1.5.48 - 2026-10-08

- Serialize all operations on TaskStore's shared SQLite connection so task
  completion callbacks cannot race scheduler runtime-state writes and crash
  the daemon with `sqlite3.InterfaceError`.

## 1.5.47 - 2026-10-08

- Consume the durable cross-root fairness turn when a persisted ordinary
  segment is launched through the throttled whitelist branch, preventing one
  throttled root from retaining the reservation for its entire cycle.

## 1.5.46 - 2026-10-08

- Make one segment root admission own its complete dependency closure: Mautic 7
  uses one native cascade command, while Mautic 6 and older run one sequential
  MCD-built topological chain in a single worker and slot.
- Suppress separately scheduled dependency members, serialize intersecting
  ordinary and recurring-priority closures, and retain parallel capacity for
  independent closures.
- Apply successful completion state to every member covered by the root
  closure so durable cycles and admission generations reflect native work.

## 1.5.45 - 2026-10-08

- Preserve already-running segment jobs when campaign pressure or queue throttle
  becomes active. Throttle still limits new segment admissions according to the
  selected profile, but no longer crosses lane boundaries to kill work.
- Remove the forbidden `segment_throttle_kill_non_whitelist` runtime/profile
  setting so custom configuration cannot restore the old behavior.

## 1.5.44 - 2026-10-08

- Keep each segment failure history row bound to the attempt that actually failed while exposing the next retry attempt separately.

## 1.5.43 - 2026-10-08

- Scope segment dead-letter admission to its failed generation and profile cooldown instead of permanently blacklisting the entity.
- Persist bounded, redacted per-attempt segment failure evidence with timing, retry policy, output hash, and truncation state.

## 1.5.42 - 2026-10-08

- Align the canonical runtime-admission policy with Mautic 7 native initial admission, related running-closure serialization, and downstream-versus-MCD retry ownership.

## 1.5.41 - 2026-10-08

- Serialize runtime execution of related segment dependency chains on Mautic 7 while retaining native-cascade initial admission, preventing concurrent commands from writing the same membership rows.

## 1.5.40 - 2026-10-08

- Remove explicit special IDs from legacy ordinary cycles while preserving cursor state, and eliminate all remaining cycle members from the priority lane.
- Log per-plan lane membership with an explicit overlap receipt.

## 1.5.39 - 2026-10-08

- Replace the regular dispatch ring with the exact persisted ordinary-cycle projection while a cycle is incomplete, preventing fresh scan IDs from preceding its cursor.

## 1.5.38 - 2026-10-08

- Persist a bounded `attempted_no_launch` ordinary fairness turn when a reserved regular ring has no verified cycle-member launch, allowing rotation on the next tick.

## 1.5.37 - 2026-10-08

- Persist and restore ordinary fairness rotation state, consuming a turn only for a verified persisted regular-cycle launch.

## 1.5.36 - 2026-10-08

- Rotate promoted ordinary segment reservations after each actual persisted-cycle launch and prevent priority work from bypassing another root's turn.

## 1.5.35 - 2026-10-08

- Restore durable incomplete ordinary-cycle fairness age after draining generic lane claims so priority, SQL, and import progress cannot reset it.

## 1.5.34 - 2026-10-08

- Emit persisted ordinary-head receipts before scheduler pause, backup guard, database dispatch pause, and disabled cluster-route early exits.

## 1.5.33 - 2026-10-08

- Expose profile authority containment diagnostics in CLI profile status and state push while preserving the existing profile runtime readback transport.

## 1.5.32 - 2026-10-08

- Reserve aged ordinary segment capacity ahead of import, SQL, immediate never-built, and static-priority work while allowing other lanes when the ordinary head is ineligible.
- Emit bounded concrete pending-head denial receipts and contain incomplete profile authority keys without overriding custom profiles.

## 1.5.31 - 2026-10-08

- Reconcile the immediate never-built queue against authoritative candidates, preserving FIFO age while removing built or duplicate entries.
- Document the canonical twelve-invariant segment lifecycle state machine.

## 1.5.30 - 2026-10-08

- Defer import targets and changed existing segments to the next terminal-boundary ordinary snapshot.
- Keep immediate dispatch only for positively identified never-built segments created during an incomplete cycle, with aged ordinary work retaining precedence.

## 1.5.29 - 2026-10-08

- Migrate and persist missing legacy ordinary-cycle age in the pre-fairness path, including preserve-only scheduler ticks.

## 1.5.28 - 2026-10-08

- Migrate incomplete legacy segment cycles without `pending_since` into durable fairness aging exactly once.
- Persist the migrated timestamp until terminal completion or assignment of a new ordinary cycle.

## 1.5.27 - 2026-10-08

- Trigger a standard segment rescan only at a terminal-cycle transition or for one unconsumed dirty generation.
- Prevent completed empty cycles from rescanning on every scheduler tick while preserving dirty IDs 58/59 rebuild admission.

## 1.5.26 - 2026-10-08

- Seed scheduler lane aging from the durable incomplete ordinary-cycle timestamp before running the fairness watchdog.
- Expose durable ordinary-cycle ages in fairness state so follow-up, throttle, and unrelated launches cannot reset reservation eligibility.

## 1.5.25 - 2026-10-08

- Persist the ordinary-cycle fairness age in `SegmentLaneCycle` snapshots and restore it across daemon restart and ring reconciliation.
- Preserve the age through follow-up, throttle, and terminal progress, resetting it only when a new ordinary cycle is assigned.

## 1.5.24 - 2026-10-08

- Reserve the shared segment slot for pending ordinary work only after its profile-configured bounded-aging wait threshold is reached; zero disables the reservation.
- Keep normal import-follow-up priority before the threshold and expose the active reservation wait reason without changing host caps.

## 1.5.23 - 2026-10-08

- Persist bounded scheduler mail-throttle observability with queue metric source, threshold, window, transition state, and timestamp.
- Emit structured transition and ordinary-dispatch branch logs that distinguish mail throttle from campaign pressure without changing scheduling policy.

## 1.5.22 - 2026-10-08

- Merge pending members of an incomplete persisted ordinary cycle back into the regular ring after scan reconciliation.
- Prevent a scan with an empty regular result from erasing cursor-ordered work while throttled, without resetting the frozen cycle.

## 1.5.21 - 2026-10-08

- Restore an incomplete persisted ordinary segment cycle into empty in-memory rings after restart without rescanning or resetting its snapshot.
- Reconstruct pending work in persisted cursor order while excluding active and terminal members.

## 1.5.20 - 2026-10-08

- Restore terminal-boundary segment planning: a new ordinary scan requires `segment_scan_pending` and no incomplete ordinary cycle.
- Keep scheduler ticks observation/dispatch-only during an active frozen cycle, and limit follow-up enqueue to the incomplete-cycle path.

## 1.5.19 - 2026-10-08

- Allow a segment that is terminal in an older frozen ordinary snapshot to enter the FIFO follow-up lane when a later input generation marks it dirty again.
- Retain active-task exclusion and follow-up deduplication to prevent concurrent duplicate dispatch.

## 1.5.18 - 2026-10-08

- Enqueue newly dirty segment IDs into the deduplicated FIFO follow-up lane independently of whether the restored ordinary cycle was complete or incomplete.
- Preserve the frozen ordinary snapshot while ensuring dirty input is not skipped after a complete-cycle assignment.

## 1.5.17 - 2026-10-08

- Re-enqueue dirty segment IDs immediately after restoring or assigning an incomplete frozen cycle during the first post-restart scan.
- Avoid waiting for a second input-state interval while preserving the ordinary assignment, cursor, and terminal state.

## 1.5.16 - 2026-10-08

- Run segment input-state scans on the configured external interval even when `segment_scan_pending` was cleared by an earlier ordinary-cycle scan.
- Preserve the frozen ordinary assignment while allowing newly dirty input to enter the existing FIFO follow-up lane.

## 1.5.15 - 2026-10-07

- Run the periodic segment input-state scan while an ordinary frozen cycle remains active, so newly dirty input is observed without rebuilding that cycle.
- Queue dirty IDs discovered during an active cycle into the existing FIFO follow-up lane, preserving the ordinary assigned order and cursor.

## 1.5.14 - 2026-10-07

- Replace mutable ordinary segment pending state with a frozen, persisted `SegmentLaneCycle` ordered as never-built, dirty/changed, then stale work.
- Preserve assigned-cycle order across scheduler ticks, retries, terminal handling, and restart recovery; only create the next snapshot after cycle exhaustion.
- Keep static priority and super-priority chains outside the ordinary cycle, and dispatch import follow-ups through a separate FIFO lane using free segment capacity.

## 1.5.13 - 2026-10-07

- Reconcile priority-ring promotion from durable admission state on every plan, so dirty IDs remain ahead of older queued work across recurring non-empty ring updates.
- Add the live-shaped fairness regression for a large existing priority ring with dirty IDs behind older entries.

## 1.5.12 - 2026-10-07

- Preserve dirty-ID promotion when a segment ring is recreated, preventing changed segments from returning behind older queued IDs.
- Keep a scan blocked until every ID assigned to the current ring has launched or been explicitly removed; one terminal worker cannot close the cycle.
- Add regression coverage for dirty promotion from an empty ring and queued-ring lifecycle.

## 1.5.11 - 2026-10-07

- Allow the minute-aligned post-terminal segment scan to reconsider durable queued IDs instead of treating them as active ring work forever.
- Preserve active native/SQL/recurring overlap guards and add regression coverage for queued fairness after ring terminal.

## 1.5.5 - 2026-10-07

- Add durable per-segment observed/applied input generations without retaining history.
- Promote never-built, changed-since-last-success, and stale segments through separate priority tiers.
- Scan input state at a configurable interval with a minimum of 60 seconds and preserve dirty state across restart/re-adoption.
- Keep a segment dirty when its input generation changes during a long build; prune state for unpublished segments.

## 1.5.4 - 2026-10-06

- Preserve raw MCC `profile_runtime` source and values in active runtime status instead of replacing it with an empty manifest-derived snapshot.
- Make `mcd-cli profile status` read the live MCC profile payload when local manifest/status data is absent.
- Keep canonical stale-age consumption unchanged at `segment_priority_stale_age_sec`.

## 1.5.3 - 2026-10-06

- Consume canonical MCC `profile_runtime.values` during runtime sync instead of silently falling back when the value is absent from legacy runtime overrides.
- Persist profile readback for local status diagnostics and retain source metadata such as `mcc_host_profile`.
- Add regression coverage for canonical profile-runtime value extraction.

## 1.5.2 - 2026-10-06

- Correct scheduler telemetry and follow-up wait reason to describe any eligible priority segment tier, including stale-over-threshold work, not only never-built work.
- Preserve the two-level priority behavior and return-to-regular behavior after a successful build.

## 1.5.1 - 2026-10-06

- Accept the canonical MCC profile key `segment_priority_stale_age_sec` while retaining the MCD runtime alias.
- Expose both names through effective runtime mapping to keep profile synchronization and local compatibility intact.

## 1.5.0 - 2026-10-06

- Add configurable segment stale-priority policy with ordered tiers: never-built, built beyond the configured age, then fresh/ordinary work.
- Default the policy threshold to 86400 seconds through runtime/MCC settings, without host-specific constants.
- Add regressions for tier ordering and policy-driven stale threshold under priority admission.

## 1.4.4 - 2026-10-06

- Classify an adopted segment worker as completed when its Mautic `last_built_date` confirms completion after restart.
- Keep real PID loss and command mismatch as `lost`, preventing false loss reports for workers that survived daemon restart.
- Add regression coverage for DB-confirmed completion of an adopted segment task.

## 1.4.3 - 2026-10-06

- Hold known import-followup segment dispatch while an admissible never-built priority segment is pending.
- Preserve import execution, active-target blocking, capacity limits, and Mautic 6 runtime dependency guards.
- Add the causal ordering fix after natural Farm-02 acceptance showed follow-up work starting before new segments.

## 1.4.2 - 2026-10-06

- Make the segment priority lane exclusive to published segments with no recorded `last_built_date`.
- Keep already processed, stale, weighted, whitelisted, and follow-up segments in the regular lane unless independently admitted by another explicit priority mechanism.
- Add regression coverage for never-built versus already-known segments under saturation.

## 1.4.1 - 2026-10-06

- Prevent fairness reservations from consuming the emergency host slot assigned to an actual priority segment dispatch.
- Stop applying MCD dependency runtime serialization to Mautic 7 native segment execution; retain active import-target blocking and older-Mautic guards.
- Correct priority-lane propagation so only priority segment rings, not regular spill, can use the reserved slot.

## 1.4.0 - 2026-10-06

- Let the actual priority segment dispatch claim the emergency host slot without depending on a stale pre-ring pending-root snapshot.
- Preserve pending-root reservation for ordinary per-instance background work and keep host maxima unchanged.
- Add regression coverage for a priority lane arriving after the pending-root snapshot.

## 1.3.9 - 2026-10-06

- Pass priority-lane admission through the actual priority segment dispatch paths, not resume or mixed baseline paths.
- Keep the emergency host and per-instance reserve inaccessible to import follow-ups and ordinary segment dispatch.
- Add the release correction after Farm-02 acceptance showed priority segments still receiving `host_capacity` under saturation.

## 1.3.8 - 2026-10-06

- Allow an explicitly scheduled priority segment lane to claim the emergency host and per-instance slot reserved for priority work.
- Keep import follow-ups and regular background tasks out of that reserved capacity while preserving configured host and instance maxima.
- Register pending priority segment rings in shared scheduler admission state and add saturated-host starvation regression coverage.

## 1.3.7 - 2026-10-06

- Stop applying legacy MCD dependency admission to Mautic 7 terminal segment plans; native Mautic 7 cascade must be allowed to start the first priority task.
- Preserve dependency admission for Mautic 6 and older, active import-target blocking, runtime dependency conflicts, and host/instance/task capacity gates.
- Record the concrete per-ID blocked reason for import targets, dependency, invalid-filter, failure-cooldown, logical-issue, and recurring-priority cases.
- Add regressions for Mautic 7 priority admission and independent priority dispatch beside a blocked import target.

## 1.3.6 - 2026-10-06

- Allow the versioned MCD self-update to proceed while independent Mautic console workers are running; retain their observation and rely on TaskStore re-adoption after the agent restart.
- Keep backup, source-operation, update, and task locks as the update safety boundary instead of blocking on any live Mautic console PID.
- Add regression coverage for self-update with an active segment console process.

## 1.3.5 - 2026-10-06

- Reserve the configured priority segment lane before import-followup dispatch so pending priority full scans cannot be indefinitely starved by import work.
- Emit deduplicated per-entity scheduler admission and rejection reasons for lock, dependency, repeat-guard, capacity, and baseline decisions.
- Add regression coverage for seven pending priority segments competing with import-followup work.

## 1.3.4 - 2026-10-06

- Expose read-only MCC profile readback through the existing profile status path: selected profile, revision/version, full and short SHA-256, source, effective runtime values, sync verdict, and atomic replacement receipt.
- Recover status readback from the existing MCC runtime-sync state when the active cache file is unavailable, without changing settings or execution sources.

## 1.3.3 - 2026-10-06

- Treat a fully verified MCC host-profile document with the same version and a new SHA-256 as authoritative and atomically replace the local document.
- Reserve rejection for unavailable/invalid fetches and preserve the last-good document on verification failure or local-newer revisions.

## 1.3.2 - 2026-10-06

- Keep MCC profile synchronization available without an Ed25519/KMS/HSM dependency in the current contract.
- Treat manifest signature fields as optional future authentication metadata; when a configured key is present, supplied signatures are still verified.
- Preserve authenticated transport, schema/version/hash validation, atomic cache replacement, and last-good same-profile recovery.

## 1.3.1 - 2026-10-06

- Align MCC profile manifest verification with the signed v1 contract: Ed25519 envelope signature, explicit selection mode, and complete mandatory MCD runtime key set.
- Apply the selected host/custom document as one source, expose exact effective readback, and reject missing, unsigned, stale, incomplete, or hash-invalid manifests without synthesizing values.
- Add `ring_count` and manifest public-key configuration for signed MCC profile delivery.

## 1.3.0 - 2026-10-06

- Make MCC profile manifests the authoritative source for provider-scoped host profiles and MCD runtime capacity.
- Verify manifest schema, identity, canonical document hash, and monotonic version before atomic cache replacement.
- Preserve last-good profile state, expose manifest apply/readback status, and keep operator custom state from being overwritten.
- Keep the packaged profile catalog only as a standalone migration fallback when MCC profile synchronization is unavailable.

## 1.2.92 - 2026-09-29

- Enforce the shared per-instance campaign capacity for priority/liveness
  trigger and rebuild lanes instead of allowing separate lanes to bypass
  `campaign_total_parallel`.
- Keep excess campaign work queued without killing running processes or
  changing host web/PHP-FPM configuration.

## 1.2.91 - 2026-09-28

- Accept MCC's minimal blocked patch-resolution response without requiring a
  request context echo when catalog admission stops before plan selection.
- Preserve strict context, catalog, and immutable-plan validation for selected
  and fully formed non-selected responses.

## 1.2.90 - 2026-09-28

- Surface an MCC catalog-resolve HTTP 403 as `mcc_patch_http_403:<detail>` only when MCC supplies a bounded allowlisted detail token.
- Preserve the generic 403 reason for invalid, oversized or unstructured response bodies; never expose response text, credentials or tokens.

## 1.2.89 - 2026-09-28

- Stop sending host-qualified `instance_uid@host` aliases in runtime-override polling; the canonical instance UID is now the only outbound key.
- Retain read-only matching of one unambiguous legacy host-qualified setting so existing desired state remains consumable during normalization.

## 1.2.88 - 2026-09-28

- Keep an explicit MCC host name unchanged for catalog transport, but when it is blank send the already resolved effective hostname instead of rejecting the request locally.
- Preserve failure on an unavailable effective hostname and cover the blank-name transport payload without changing host/plugin execution.

## 1.2.87 - 2026-09-28

- Permit the existing guarded PHP 8.4 system stage for an MCC-preflighted, operator-acknowledged strictly forward manual Mautic 7.x upgrade only when it has an immutable patch plan/run ID and local backup.
- Advertise the narrow manual runtime capability in read-only upgrade preflight; preserve the prohibition on a free per-instance PHP selector.
- Keep the PHP stage in the same maintenance operation but execute it only after Mautic reaches the selected target, with bound execution evidence for backup, host compatibility, observed PHP/FPM/nginx state and rollback status.

## 1.2.86 - 2026-09-28

- Emit bounded target-stage failure evidence with the allowlisted relative path and staged/live SHA-256 values when a prepared target differs from the installed target.
- Preserve the strict target-source rejection and patch rollback behavior; do not expose file contents or weaken source binding.
- Cover exact failure evidence and unchanged success behavior in the target-stage regression.
- Update the v1 patch-resolution package identity stamp to 1.2.86 as required by the existing equality check; this does not add a target-policy gate.

## 1.2.85 - 2026-09-28

- Authorize immutable patch-plan backups after transient storage unmount using the exact durable marker path and SHA from the instance-backup receipt.
- Keep remote backup identity and legacy mounted-marker evidence intact; reject altered, misplaced, missing or unsafe durable evidence before issuing or consuming a signed attestation.
- Cover post-unmount issue/validate, path and identity drift, marker tampering and legacy receipt compatibility.

## 1.2.84 - 2026-09-28

- Satisfy a cross-line backup prerequisite with an existing signed JSON repair authorization only after revalidating its instance, versions, plan, signature, expiry and actual backup marker.
- Keep standalone and unproven transitions on the local backup requirement; do not create a duplicate backup for an externally verified MCC backup.
- Preserve honest rollback availability from verified external backup evidence and cover missing, altered and expired proofs.

## 1.2.83 - 2026-09-28

- Preserve the remote instance-backup marker path and publish a separate durable, root-only exact-byte authorization marker before releasing transient backup storage.
- Bind the authorization marker to the backup receipt digest so separate repair authorization and apply commands can verify it after unmount; fail honestly on missing or altered evidence.
- Cover post-unmount authorization, marker integrity and legacy backup receipt behavior.

## 1.2.82 - 2026-09-27

- Remove the introduced major-wide PHP readiness policy and restore native Composer readiness/bootstrap behavior without a replacement policy resolver.
- Preserve authoritative static-version checks, Composer errors, repair/backup requirements and raw PHP observations without claiming target dependency compatibility.
- Cover existing upgrade preparation, native failure propagation and cache-only bootstrap rejection across target versions.

## 1.2.81 - 2026-09-27

- Retain the existing source-update lock in shared mode throughout Mautic upgrade execution, including staging, Composer gaps, verification and failure cleanup.
- Defer self-update while an operation owns that lease; preserve deferred session status and normal retry without terminating workers.
- Cover source-package repair and restart-only paths with the updater's exclusive lock and test real cross-process races plus ZIP/Composer entrypoint boundaries.

## 1.2.80 - 2026-09-27

- Execute and verify assets when baseline policy permits backup-off; keep actual rollback availability separate from execution success.
- Preserve genuine asset command and HTTP failures without fabricated backup or rollback evidence.
- Bind whole-plan patch rollback receipts to the original immutable apply plan and include the v3 evidence schema.

## 1.2.79 - 2026-09-27

- Remove ordinary-upgrade conditional exclusion proof transport and global pre-Composer database admission.
- Evaluate selected conditional patches during runtime execution while preserving source bindings and typed drift guards.
- Prepare Composer-managed directory ownership and owner write permissions during execution, including nested package assets, without changing unrelated file contents.

## 1.2.78 - 2026-09-27

- Execute campaign/import consumer patch phases immediately after source installation under upgrade maintenance, before scripts or managed consumer resumption.
- Preserve catalog phase names, target binding, source gates, idempotency, rollback and final postimage verification in ZIP and Composer workflows.
- Cover ordinary six-record upgrade plans, asset-only patches, repeated phases, drift and early rejection through the upgrade entrypoint.

## 1.2.77 - 2026-09-27

- Scope migration token ambiguity to the requested target; unrelated raw-distinct logical collisions no longer invalidate unique exact target presence.
- Preserve raw duplicate, target alias, unsafe history and absent-target literal rejection without decoding, deduplication or receipt changes.
- Add complete 120-row collision regression with unchanged raw history hashes.

## 1.2.76 - 2026-09-27

- Scope migration execution facts to positive raw canonical target presence without decoding unsupported namespace-looking literal history.
- Preserve ambiguity, duplicate, malformed and absent-target guards; retain complete raw hashes and receipt bindings.
- Add bounded target-relevance diagnostics from existing readonly capture without new SQL.

## 1.2.75 - 2026-09-27

- Add bounded sanitized migration-storage error diagnostics from existing readonly capture.
- Preserve stable error prefixes, fail-closed behavior, storage grammar and receipt fields; expose no raw values or connection data.

## 1.2.74 - 2026-09-27

- Recognize documented unrelated legacy migration timestamps without requiring all history to be FQCN-only.
- Preserve exact target identity, raw storage hashes and typed receipt bindings; reject target aliases, duplicates and unknown formats.
- Version collector storage semantics without changing catalog predicates or receipt wire fields.

## 1.2.73 - 2026-09-27

- Add fully bound read-only conditional exclusion proofs for ordinary upgrade plans.
- Reobserve complete accepted facts before target preparation, maintenance admission; reject true, unknown, partial coverage or drift.
- Advertise the conditional exclusion guard capability without enabling scenario execution.

## 1.2.72 - 2026-09-27

- Advertise the exact read-only file-only target verification capability through a standalone nonroot JSON query.
- Return actual agent version, pinned contract and explicit online-admission/database-fact limits without reading configuration or an instance.
- Do not grant scenario dispatch, apply, rollback or lease-renewal authority.

## 1.2.71 - 2026-09-27

- Add root-only check-only verification of excluded target patches under dedicated online purpose admission.
- Bind effective catalog bytes, original apply plan, prepared identity, source layout and complete fixed-source signatures without apply or rollback permission.
- Read prepared registry metadata without writes or sidecars; recheck actual archives and staged bytes only after finite admission.
- Publish bounded direct JSON results with independent gate validation and explicit private-file rejection.
- Keep scenario dispatch disabled and preserve legacy context commands.

## 1.2.70 - 2026-09-27

- Add root-only read-only local-context v2 CLI with independently discovered identity and Composer layout.
- Separate project root, application root and console location without changing legacy context v1.
- Require real static version metadata and reject ambiguous consoles, symlinked anchors and unknown bindings.
- Publish the bounded v2 context/root-mapping contract and a data-only split-layout fixture without enabling scenario dispatch.

## 1.2.69 - 2026-09-27

- Resolve split Composer layouts with application files in docroot/public and console in the project root.
- Preserve strict layout ambiguity, containment and symlink checks for project-root and application-root selection.
- Bind read-only context preflight to the independently resolved application root without weakening instance UID checks.

## 1.2.68 - 2026-09-26

- Accept the MCC authorization context's strict JSON integer policy revision without coercion.
- Reject boolean, string, floating-point and null policy revisions before sending authorization requests.

## 1.2.67 - 2026-09-26

- Consume private MCC release-authorization context-v2 and live authorization-v3 with exact immutable-plan and instance bindings.
- Require explicit release-transition permission for every cross-line apply path; never fall back to target-only approval.
- Select automatic patch targets only within the installed release line, and enforce signed repair, backup, database and installation prerequisites independently.
- Advertise the versioned host execution capability and packaged wire contract without exposing authorization material.

## 1.2.66 - 2026-09-26

- Add read-only selected-instance context preflight for MCC immutable upgrade-plan issuance and redispatch validation.
- Read discovery configuration without migration, recovery, history writes, database queries or Mautic bootstrap.
- Publish per-instance context selection and enqueue/retry/bulk/rollback binding sequence.

## 1.2.65 - 2026-09-26

- Supply independently discovered explicit instance context through authenticated patch resolution.
- Reject typed plans whose context differs from the fresh request; preserve file-only compatibility when context is unknown.
- Publish versioned context capability and precise typed receipt digest/binding verification contract.

## 1.2.64 - 2026-09-26

- Add typed read-only integer-domain, filtered row-count and byte-exact migration-state observations.
- Bind DB facts to independently discovered instance/root/prefix and local connection identity; reject unknown schema, encoding and domain states.
- Recheck admission observations before file apply and preflight all rollback phase receipts/hash chains before restoration.
- Preserve verified conditional skips as explicit outcomes, without advertising method or dependency predicates.

## 1.2.63 - 2026-09-26

- Add the versioned MCC-resolved Mautic patch contract for generic runtime remediation.
- Add authenticated resolved-plan/evidence transport with host, version, plan, and payload hash checks.
- Enforce plan/record field allowlists, trigger/phase selection, and safe payload references before execution.
- Preserve upgrade atomic preflight with one inline, revision-pinned v3 plan and later declared execution phases.
- Add resolver regression coverage for auth, canonical hashes, payload integrity, and fail-closed schema guards.
- Add generic v3 phase execution, read-only atomic simulation and plan-bound file rollback, including verified legacy backup import.
- Bind major-upgrade backup evidence to immutable plans with a signed host-local attestation; same-major JSON preflight does not require external backup.
- Publish exact gate field schemas and a data-only accepted record fixture for catalog owners.
- Resolve recommended-project Composer ownership above docroot using its manifest and lock, independently of the application console location.

# MCD Changelog

## 1.2.62 - 2026-09-26

- Support bounded binary Git patch payloads and safe declared-file creation/replacement with exact before/after SHA-256 evidence.
- Add generic absent/present path-state and exact SHA-256 gates; reject symlinked paths and ambiguous or partial source states before mutation.
- Make verify run the same non-mutating patch/path checks as apply, and make rollback restore replaced files or remove created files only when recorded after hashes match.

## 1.2.61 - 2026-09-26

- Advertise the Mautic patch-plan v2 and preflight evidence capabilities only when the complete executor is available on supported host runtimes.
- Include v2 evidence schema in the CLI contract and raise its minimum supported agent version to 1.2.61.
- Preserve and restore every file in multi-file patches and return safe rollback evidence when atomic preflight fails before any phase is applied.

## 1.2.60 - 2026-09-25

- Exclude mutable runtime logs, cache, spool, queue, temporary and session contents while retaining runtime directory metadata.
- Record runtime exclusions and SHA-256 asset digests in backup manifests; verify recorded digests before restore.
- Add `backup --no-prune run` to preserve pre-existing backup generations for a one-shot run.

## 1.2.59 - 2026-09-25

- Keep full-scan segment work pending across ordinary due-query refreshes until dispatch or unpublication, including children waiting for parent segment completion.
- Decode serialized segment filter values without treating array indexes or adjacent clauses as dependency IDs.
- Log full-scan pending IDs and native ring membership for scheduler diagnosis.

## 1.2.58 - 2026-09-25

- Treat a config restored from MCC profile drift as the new local runtime baseline so recovery cannot push the stale pre-recovery runtime back as a local edit.

## 1.2.57 - 2026-09-25

- Retry runtime desired-state ACK after transient failure or restart, tracking each scope by applied revision and content SHA256 independently from the desired snapshot.

## 1.2.56 - 2026-09-25

- Cover authoritative instance override removal from a saved legacy config through agent reload and restart.

## 1.2.55 - 2026-09-25

- Include agent test fixtures in published source archives so the final package passes the complete regression suite.

## 1.2.54 - 2026-09-25

- Rebuild effective runtime state after authoritative MCC clears and remove stale bare/qualified instance aliases during desired-state reconciliation.

## 1.2.53 - 2026-09-25
- Reconcile authoritative MCC runtime removals from the local/profile baseline,
  deleting stale stable instance overrides instead of retaining them after an
  unset or daemon restart.
- Keep named profile capacity presets authoritative through runtime reload.

## 1.2.52 - 2026-09-25
- Run periodic full segment discovery on active named profiles so newly
  created or changed segments enter the normal planner without instance
  overrides.
- Reconcile removed instance-scoped MCC overrides from authoritative desired
  state rows and preserve other instances' settings.
- Preserve the selected profile's configured scheduler capacity independently
  of hardware recommendations.
- Define literal host and per-instance scheduler capacities for every named
  profile; hardware recommendations no longer supply missing profile limits.
- Remove absent authoritative MCC stable runtime keys from local config and
  reapply remote state from the local/profile baseline, preventing stale
  per-instance segment overrides after an unset or daemon restart.

## 1.2.51 - 2026-09-25
- Add registry-bound Mautic patch-plan v2 with semantic version applicability,
  literal fail-closed signature gates, immutable payload checksums, phase-scoped
  execution, and rollback evidence while preserving v1 compatibility.

## 1.2.50 - 2026-09-24
- Honor selected-profile per-instance segment fan-out when no explicit
  operator host safety cap is configured; retain configured caps when present.
- Add bounded segment scheduler telemetry for import-follow-up queue wait,
  dispatch blocking reasons, and segment process runtime/outcome.

## Unreleased

## 1.2.49 - 2026-09-24
- Add bounded segment scheduler telemetry for import-follow-up queue wait,
  dispatch blocking reasons, and segment process runtime/outcome.

## 1.2.48 - 2026-09-24
- Allow explicitly acknowledged manual forward upgrades within the installed Mautic major without requiring an unrelated patch plan; retain strict validation for selected patch adapters and release authorization for automatic upgrades.

## 1.2.47 - 2026-09-23

- Rotate web-availability probes across discovered instances so each instance
  is sampled over successive MCC alert-poll cycles.

## 1.2.46 - 2026-09-22

- Add contract v1 HTTPS web-availability observations with DNS/TLS/HTTP
  diagnosis, persistent failure threshold, cooldown and bounded service restart.
- Emit AssetMapper verification v2 evidence with explicit webroot source,
  manifest HTTP/content-type checks, referenced CSS/JS evidence and rollback.

## 1.2.45 - 2026-09-22

- Keep Mautic worker processes alive across MCD self-update and supervisor
  restart, persist their task rows, and re-adopt live processes after startup.
- Defer every self-update path while an active Mautic console process is
  running, including `mautic:segments:update` and restart-only repairs.

## 1.2.44 - 2026-09-22

- Return a nonzero result from `mautic-upgrade check` when version evidence is
  non-authoritative, preventing legacy apply routing from treating
  `current=unknown` as an admissible upgrade source.

## 1.2.43 - 2026-09-22

- Make `mautic-upgrade check` fail closed for cache-only version evidence:
  cache fallback is no longer printed as `current`, branch targets are not
  selected from it, and rollback checks continue to report authoritative
  on-disk metadata from the selected install root.

## 1.2.42 - 2026-09-22

- Add post-upgrade AssetMapper verification for Mautic 7 Composer and ZIP
  layouts: discover the served webroot, generate assets and clear cache as the
  runtime user, then require manifest-referenced CSS/JS HTTP 200 responses with
  matching content types before reporting upgrade success.

## 1.2.41 - 2026-09-22

- Require authoritative static Mautic version evidence before Composer
  preparation or Mautic 6 to 7 repair authorization; stale cache-only evidence
  cannot trigger bootstrap or issue an authorization context.

## 1.2.40 - 2026-09-22

- Fix `mautic-upgrade check` to consume structured read-only version evidence
  directly, so static metadata is marked authoritative and cache fallback is
  visibly non-authoritative.

## 1.2.39 - 2026-09-22

- Distinguish authoritative static Mautic version evidence from cache fallback
  in inventory and `mautic-upgrade check`; manual refresh evidence is no longer
  marked authoritative when only the MCD cache exists.
- Fail closed on conflicting `release_metadata.json` and `composer.lock`
  versions.

## 1.2.38 - 2026-09-22

- Make inventory and `mautic-upgrade check` prefer authoritative on-disk
  Mautic metadata, refresh the MCD cache downward after rollback, and emit
  structured read-only version evidence for MCC refresh/rescan.

## 1.2.37 - 2026-09-22

- Fix read-only Mautic version detection to prefer on-disk release metadata and
  Composer lock evidence over a stale cache, and fail closed when those sources
  disagree.

## 1.2.36 - 2026-09-22

- Make Mautic upgrade preflight, Composer prepare and repair authorization
  version probes bootstrap-free: they use only MCD cache and static release or
  Composer metadata, and fail closed when no read-only version evidence exists.

## 1.2.35 - 2026-09-22

- Add the signed, allowlisted `mcd-mautic-json-schema-repair-execution-v1`
  executor for the validated Mautic 6 to 7 JSON/collation repair plan, with
  idempotent nullable/non-nullable handling, before/after evidence, JSON
  validity counts, backup authorization and fail-closed outcomes.
- Add one-line `MCD_UPGRADE_PREFLIGHT_EVIDENCE` markers, the guarded
  `mautic-upgrade composer-prepare` operation, and automatic repair admission
  before Composer/Doctrine work in the major upgrade path.
- Gate Composer/package work on the existing Mautic major-to-PHP policy and
  expose explicit PHP compatibility, remediation and system-upgrade decision
  fields in preflight evidence.

## 1.2.34 - 2026-09-22

- Add the read-only `mcd-mautic-upgrade-preflight-v1` contract with sanitized
  PHP/Composer readiness and fail-closed Composer bootstrap states.
- Add the read-only `mcd-mautic-json-schema-repair-v1` diagnosis and strict
  `mcd-mautic-json-schema-repair-plan-v1` allowlist for the observed Mautic 6
  to 7 SQLSTATE 1253 JSON/collation condition; no arbitrary SQL or migration
  history mutation is accepted.

## 1.2.33 - 2026-09-21

- Add the revision-pinned Mautic 7.2.0 GrapesJS asset-path remediation to the
  standard patch-plan contract, including exact vulnerable/fixed gates,
  enabled-plugin and compiled-asset preflight, evidence, backup and rollback.

## 1.2.32 - 2026-09-17

- Keep every published due Mautic 7 segment in the scheduler plan even when a
  terminal segment references it through a lead-list filter. Recursive Mautic
  dependency traversal no longer suppresses customer-visible unsubscribe,
  bounce or other lead lists without a dedicated rebuild.

## 1.2.31 - 2026-09-16

- Use the shared Mautic 6/7 `lead_fields` schema names for native/custom,
  group, ordering and declared character length metadata. The failed 1.2.30
  test build was not promoted.

## 1.2.30 - 2026-09-16

- Fix the contact field metadata report to read Mautic's canonical
  `lead_fields.label` column. The failed 1.2.29 test build was not promoted.

## 1.2.29 - 2026-09-16

- Publish the read-only `mcd-contact-field-metadata-v1` report contract for
  instance-local Mautic field definitions and safe database storage metadata.
  The report excludes contact rows, stored values, defaults and serialized
  field properties, and returns structured per-instance errors for MCC jobs.
- Advertise the report in DB-capable instance runtime capabilities and
  operations, and include explicit schema, MCD and Mautic versions in results.

## 1.2.28 - 2026-09-15

- Guarantee one per-instance segment/import slot and one per-instance campaign
  slot outside the dynamic shared host pool while preserving exact-task locks
  and bounded additional concurrency.
- Keep one non-whitelist segment baseline alive during queue throttle and fix
  multi-root fairness promotion so it reserves one owned slot instead of
  reducing every promoted root's host limit to zero.

## 1.2.27 - 2026-09-15

- Reserve newly available host capacity for fairness-watchdog promoted instances
  and reset their wait age only after a successful scheduler launch, preventing
  continuous work on other instances from starving published segment rebuilds.

## 1.2.26 - 2026-09-14

- Move recurring segment admission into an independent one-second daemon loop
  so long multi-tenant DB planning cycles cannot violate the configured maximum
  start interval.

## 1.2.25 - 2026-09-14

- Preserve canonical recurring segment observations in the frequent
  monitor-only scheduler signal payload so MCC does not replace them with an
  otherwise valid partial scheduler snapshot.

## 1.2.24 - 2026-09-14

- Publish recurring segment observations with the matched canonical instance
  scope key so MCC can correlate delivery and telemetry when local inventory
  still contains a legacy unqualified UID.

## 1.2.23 - 2026-09-14

- Include canonical host-qualified instance UIDs in runtime override polls when
  legacy local inventory still stores an unqualified UID.
- Materialize and acknowledge canonical instance desired state against the
  matching local instance without adding a host-global fallback.

## 1.2.22 - 2026-09-14

- Add the opt-in `segment_recurring_priority_v1` instance/segment contract for
  a dedicated MCD-managed single-flight lane with bounded start cadence.
- Exclude configured recurring IDs from ordinary segment rings and share their
  process lock so unrelated tenants and host-wide parallelism remain unchanged.
- Publish active process, last/next run and terminal failure state through the
  `mcd-segment-recurring-priority-v1` scheduler signal contract.

## 1.2.21 - 2026-09-14

- Reject an already-installed target in the MCC-preflighted single-instance
  path before maintenance, backup, permissions or source mutation, using the
  stable `target_already_installed` reason.
- Emit one terminal `mcd-mautic-patch-preflight-v1` evidence record for every
  manual admission rejection so MCC can classify a fail-closed no-mutation
  result instead of reporting missing patch-stage evidence.
- Keep the managed patch plan pinned to Mautic 7.1.3 to 7.2.0 and raise its
  minimum compatible agent version to 1.2.21.

## 1.2.20 - 2026-09-14

- Scan campaign rebuild eligibility in a dedicated liveness thread so long
  segment planning, plugin work and message-queue jobs cannot prevent campaign
  membership from being initialized.
- Dispatch exact rebuild-due campaign IDs through isolated priority capacity
  while preserving cluster cron routing, campaign locks and active-window SQL;
  expired campaigns are not recovered or triggered late.
- Publish per-instance rebuild scan status, errors, pending IDs and dispatch
  evidence in `campaign_scheduler_liveness_runtime` signals.

## 1.2.19 - 2026-09-14

- Reconcile a cached Mautic version when local Composer or release metadata
  proves that the installation has advanced, then refresh from the authoritative
  runtime before the next inventory payload. Never downgrade a newer cache from
  older package metadata.
- Define patch-plan evidence SHA-256 over compact, key-sorted canonical JSON so
  MCC and MCD verify the same plan bytes after a successful atomic patch stage.
- Keep stale-source admission fail-closed; a preflighted 7.1.3 to 7.2.0 job is
  rejected when the authoritative runtime has already advanced.

## 1.2.18 - 2026-09-13

- Advertise `mcd-mautic-patch-preflight-v1` in outbound runtime capabilities
  and derived operations for supported host ZIP/Composer profiles that
  explicitly declare filesystem and console, without requiring the optional
  `host-managed-upgrade` policy token.
- Keep Docker/image-managed profiles fail-closed and retain `core-upgrade` as a
  separately declared host-managed operation.

## 1.2.17 - 2026-09-13

- Advertise the versioned `mcd-mautic-patch-preflight-v1` capability and its
  minimum agent version through the patch-plan contract.
- Validate the complete catalog plan against the actual source, target and
  install type before upgrade mutation, and emit a stable terminal evidence
  shape for every success and fail-closed rejection.
- Run the atomic patch hook whenever a strict plan is explicitly supplied while
  preserving the mandatory managed Mautic 7.1.3 to 7.2.0 plan requirement.

## 1.2.16 - 2026-09-13

- Restore instance backup files into the exact managed instance root and load
  the marker-declared tenant database instead of applying host-level restore
  heuristics.
- Fail instance restore when its managed target, files archive or DB dump is
  missing, and never use host/system MariaDB root authentication for this path.

## 1.2.15 - 2026-09-13

- Add the opt-in `backup.storage.kind=local` transport for direct Mydumper and
  persistent-file instance backups into a caller-mounted filesystem target.
- Preserve the SFTP profile and backup format while adding root-owned path,
  mountpoint, traversal, symlink, overlap and atomic-publication guards plus
  local restore through the existing parallel Myloader path.
- Accept profile-level Mydumper/Myloader thread counts for local and SFTP
  transports without changing existing defaults.

## 1.2.14 - 2026-09-11

- Retain the complete terminal `mcd-plugin-apply-result-v1` record in recurring
  plugin-operation output even when its inventory exceeds the bounded stdout
  tail, so MCC receives post-step and exact inventory evidence atomically.
- Keep non-plugin output bounded and leave all command timeout policies
  unchanged.

## 1.2.13 - 2026-09-11

- Make cluster plugin operations own the complete structured apply-result
  lifecycle, including delegated and wait-success paths.
- Remove the cluster no-change early success: synchronize nodes, run post-steps
  and require exact per-selected-bundle inventory before terminal `rc=0`.

## 1.2.12 - 2026-09-11

- Require exact selected target-version confirmation before a plugin apply can
  emit terminal success; catalog status alone is no longer sufficient.
- Run post-steps and inventory confirmation for no-op file phases, preventing
  stale installed plugin versions from returning `rc=0` with false success.

## 1.2.11 - 2026-09-11

- Publish durable recurring plugin-operation runtime state with lifecycle
  timestamps, heartbeat, resource ownership, result output and next run.
- Isolate recurring plugin operations from unrelated campaign/import capacity;
  only an identical resource key can block a run, with an explicit skip reason.
- Exempt recurring plugin operations from generic wall-clock termination and
  derive stale state from heartbeat and process ownership instead.

## 1.2.10 - 2026-09-11

- Make plugin production-cache reset race-safe: atomically detach the active
  disposable cache generation, immediately recreate `prod` with runtime
  ownership and mode `0775`, and keep best-effort cleanup from failing the job.
- Generate the stable plugin UID from the bundle and supported Mautic majors
  when a manifest omits `plugin_uid`, using the same canonical value for
  catalog output and `--plugin-uid` selection.

## 1.2.9 - 2026-09-10

- Add the fail-closed `mcd-plugin-apply-result-v1` callback/result contract.
  Emit `files_applied` immediately before post-steps, require cache clear,
  native plugin install/reload, cache warmup and final inventory confirmation,
  and preserve any nonzero post-step result as terminal failure even when the
  new plugin version is already visible in filesystem inventory.

## 1.2.8 - 2026-09-10

- Delete and recreate the active `var/cache/prod` or legacy `app/cache/prod`
  generation with runtime ownership and mode `0775` before the first Symfony
  console boot after plugin filesystem mutation. Plugin reload then rebuilds a
  clean cache and subsequent queued plugin jobs cannot inherit compiled
  references to a removed bundle. Emit `mcd-plugin-cache-reset-v1` evidence;
  ordinary `remove` remains non-purge and retains plugin settings.

## 1.2.7 - 2026-09-09

- Add `mcd-mautic-patch-preflight-v1` evidence and rollback-safe execution for
  the complete Mautic 7.1.3 to 7.2.0 patch sequence. Snapshot all patch files
  before the first patch, verify the final state and restore exact pre-patch
  bytes on phase, apply or verification failure; rollback failure is a hard
  incident and failures report `upgrade_started=false`.

## 1.2.6 - 2026-09-09

- Add an explicit root-only, MCC-preflighted manual single-instance override
  for the exact Mautic 7.1.3 to 7.2.0 upgrade. Validate canonical root,
  installed version/layout, strict pinned patch plan and run ID before
  maintenance and again before source mutation. Ordinary upgrade paths retain
  global release authorization; no shared release exception is introduced.
- Require prior MCC plugin preflight and explicit operator risk acknowledgement
  as the manual dispatch contract. This path adds no token, secret argument or
  network authorization protocol and cannot perform a major/system upgrade.

## 1.2.5 - 2026-09-09

- Match the actual 7.2.0 role migration's 8/12-space executable block, using
  the independently captured vulnerable file SHA and the corrected file SHA
  together with exact occurrence counts. Unknown, mixed and changed files fail
  closed; the original 7.2.0 file is never treated as already fixed.
- Preserve per-phase gate evidence and original backups across repeat phase
  calls; emit failed gate evidence before aborting an upgrade. Validate real
  ZIP/Composer layout and version metadata, including root containment.
- Add complete upstream source fixtures matching the disposable acceptance
  hashes, PHP hydrated-row execution coverage and interrupted-write recovery.

## 1.2.4 - 2026-09-09

- Fixed the Mautic 7.2 role-migration gate to match the canonical executable
  loop signature rather than an optional docblock, while retaining exact-count
  fail-closed behavior for duplicate, mixed or unknown source states.

## 1.2.3 - 2026-09-09

- Added internal Mautic 7.1.3 to 7.2.0 patch-plan orchestration to
  `mautic-upgrade apply`: role remediation runs after source install and before
  migrations; AssetMapper runs before deferred Composer scripts/asset generation.

## 1.2.2 - 2026-09-09

- Added a fail-closed, revision-pinned Mautic 7.2 patch-plan adapter for the
  role migration and AssetMapper webroot remediations. It accepts no commands,
  file paths or patch payloads from the caller.

## 1.2.1 - 2026-09-08

- Fixed campaign audit liveness: forced rebuild/trigger candidates now spill
  ahead of a continuously populated regular ring when the priority lane has no
  dedicated workers, preventing detected campaigns from waiting indefinitely.

## 1.2.0 - 2026-09-06

- Preserve integration settings during ordinary plugin removal and mutually exclusive replacement. Remove registration references transactionally and restore them after native reinstallation without reading or changing setting values.
- Add explicit `purge` as a separate plugin action to delete retained integration settings, including after earlier removal. Fail closed on registration database errors.
- Keep registration-only ownership metadata in the instance database so shared-database installations use the same lifecycle state.

## 1.1.0 - 2026-09-06

- Add generic declarative plugin-operation listing, validation, command execution,
  and completion driven only by the installed instance catalog.

## 1.0.35 - 2026-09-05

- Require live MCC authorization for Mautic upgrade targets before maintenance
  and again before package replacement. Explicit targets and cached ZIP files
  cannot bypass operator approval; unavailable MCC authorization fails closed.
- Resolve managed upgrade targets only from MCC, without merging stale local
  fallback versions or unapproved ZIPs into the available target list.

## 1.0.34 - 2026-09-05

- Defer Composer post-autoload and post-update scripts until dependencies have
  been replaced and the old Mautic production container has been removed.
  This prevents 7.1-to-7.2 asset generation from booting stale service wiring.
- Preserve normal Composer plugin and pre-update events, replay declared
  deferred scripts, and require Composer 2.8.6 or newer before changing files.
- Keep dry runs script-free and cover hook ordering, absent hooks, unsupported
  Composer versions and failure propagation with upgrade regression tests.

## 1.0.33 - 2026-09-05

- Automatically reconcile the existing import-tag detached-entity workaround
  on host-native Mautic 7.0, 7.1 and 7.2 installations. Upstream PR #17209 is
  still unmerged and the affected assignment remains in 7.2.0.
- Check the shipped release metadata and unique source context, skip unknown
  versions, Docker and maintenance pauses, and leave upstream fixes untouched.
- Serialize patch changes and refresh the verified rollback source after a core
  update. Restore managed patches before upgrades and reapply when still needed.
- Add regression coverage for default reconciliation, version/layout gates,
  maintenance, upstream-fixed code, write errors and repeated upgrade cycles.

## 1.0.32 - 2026-09-05

- Aligned Mautic upgrade diagnostics and CLI help with the unified Mautic 7
  release family. Checks now report `branch=7`, and `--allow-minor` accurately
  describes guarded forward minor upgrades rather than a one-step limit.

## 1.0.31 - 2026-09-05

- Unified Mautic 7 release discovery across minor versions. Upgrade checks on
  7.0 and 7.1 now resolve the latest stable Mautic 7 target from MCC, with
  7.2.0 as the offline fallback.
- Allowed an explicitly authorized forward minor update to cross more than one
  Mautic minor release, enabling the unified 7.x path while retaining the
  existing `--allow-minor` guard.

## 1.0.30 - 2026-09-04

- Fixed: the cluster offsite scheduler now honors the configured
  `offsite_not_before_hour` and `offsite_not_before_minute` instead of starting
  a missed backup immediately after an earlier daemon restart.
- Fixed: SSHFS backup mounts no longer use unbounded reconnect behavior after a
  dead connection. Failed normal unmounts fall back to bounded lazy FUSE
  detach, allowing backup locks and the daemon to recover for the next run.
- Added regression coverage for the offsite start gate and stale SSHFS recovery
  behavior.

## 1.0.29 - 2026-09-03

- Changed: orphan page-hit cleanup now keeps a rolling 24-hour attribution
  window by default while preserving explicit operator overrides.

## 1.0.28 - 2026-09-02

- Added: own-host image deployment validates the host's shared forward-confirmed
  mail identity before downloading artifacts or creating database, filesystem,
  nginx, and certificate resources.
- Fixed: local-mail state stores one explicit MTA identity for the entire host
  instead of selecting it from the alphabetically first instance domain.
- Fixed: image deployment retries bounded Certbot operations when another
  Certbot process temporarily owns the global lock, allowing rollback to remove
  certificates reliably.

## 1.0.27 - 2026-09-02

- Fixed: the configured MCC endpoint is now an invariant local Fail2ban
  allowlist entry, independent of the centrally returned blocklist profile.
- Added: every security synchronization repairs and unbans the control-plane
  address before contacting MCC, preventing an existing all-ports ticket from
  deadlocking heartbeat and policy recovery.
- Added: regression coverage for empty central allowlists and self-recovery
  when the control-plane address was already blocked.

## 1.0.26 - 2026-09-02

- Fixed: plugin reload passes the discovered Mautic database table prefix via
  Mautic's native `MAUTIC_TABLE_PREFIX` environment variable. Native plugin
  migrations now inspect and update prefixed tables instead of silently
  treating them as absent.
- Added: host and container command rendering keeps that environment scoped to
  plugin reload; unrelated Mautic commands receive no new environment values.
- Added: regression coverage for prefix delivery and command scoping.

## 1.0.25 - 2026-09-02

- Fixed: Mautic 6 plugin updates preserve the database version for selected
  metadata-less plugins that ship native migrations, so the normal plugin
  reload executes those migrations instead of skipping them after version
  prealignment.
- Added: plugin updates try the stock Mautic reload first. The Mautic 6
  null-metadata compatibility adjustment is used only after that known native
  failure, is scoped to one retry, and restores the core helper immediately;
  unrelated metadata-less plugins retain the existing prealignment guard.
- Added: regression coverage for migration-version preservation and exact
  temporary core restoration.

## 1.0.24 - 2026-09-01

- Prevent MCD from rebuilding a segment while a queued or running import explicitly writes contacts to that segment through `defaults.list`.
- Queue the exact target segment for a priority native rebuild after a successful import and bypass only that segment's ordinary repeat interval; task locks, capacity limits, throttling, and failure guards remain active.

## 1.0.23 - 2026-09-01

- Kept the MCC web-scanner blocklist independent from SSH Fail2ban state. A
  host with an absent or externally managed SSH jail now still applies its
  verified web-security blocks; active managed SSH jails retain their firewall
  verification.

## 1.0.22 - 2026-09-01

- Preserve and restore only confirmed Mautic 4 queued-email sender workers,
  including legacy `mautic:emails:send` and documented Messenger consumers.
  MCD continues to retire the removed legacy command on Mautic 5/6/7, while
  `mautic:messages:send` remains separate because it only processes
  frequency-rule messages.

## 1.0.21 - 2026-09-01

- Serialize security-blocklist synchronization and add desired bans before
  removing stale ones. A stalled or interrupted reconciliation can no longer
  create an unprotected window or race a concurrent daemon cycle.

## 1.0.20 - 2026-09-01

- Install and maintain the Mautic nginx login Fail2ban filter with the managed
  jail. Hosts without a legacy local filter now receive the same all-vhost
  login-brute-force protection rather than rejecting the security profile.

## 1.0.19 - 2026-09-01

- Use the same verified all-ports netfilter action for a detected Mautic login
  attacker as for SSH and MCC Wazuh blocks. The login filter remains limited to
  authentication POSTs, while the resulting source-IP ban cannot be bypassed
  by switching ports. Do not require an on-demand web action chain before the
  first login attack creates it.

## 1.0.18 - 2026-08-31

- Manage the Mautic nginx login jail alongside SSH. Every Mautic vhost on a
  protected host now blocks three failed login POSTs in ten minutes, with the
  same office/VPN allowlist and progressive seven-to-thirty-day bans. The
  action is limited to HTTP/HTTPS and does not ban generic web or API errors.
- Verify the persistent SSH firewall chain after a managed Fail2ban restart;
  the web-login action is activated on its first matching login attack.

## 1.0.17 - 2026-08-31

- Wait for Fail2ban to restore its persistent SSH firewall chain after a
  managed restart. This avoids rolling back a valid strict jail merely because
  systemd reports the service active before its actions are installed.

## 1.0.16 - 2026-08-31

- Hardened the managed SSH Fail2ban jail to ban after three failed attempts in
  ten minutes, with office/MCC-VPN allowlisting and progressive bans from seven
  to thirty days.
- Replaced the stale legacy nftables action with a unique, verifiable
  netfilter action. Fail2ban can no longer report SSH tickets as banned when
  its firewall table was never created.

## 1.0.15 - 2026-08-31

- Added a narrowly scoped, reversible Mautic `7.1.3` import-tag remediation.
  It repairs the detached existing-tag reference only on the affected version,
  records verified original and patched checksums, and never rewrites an
  already-fixed file.
- Mautic upgrades from `7.1.3` now restore that exact core file before changing
  versions, preventing the temporary remediation from carrying into a later
  release.

## 1.0.14 - 2026-08-31

- Enforced the dual priority queue whenever campaign or segment whitelists are
  configured. A stale single-ring runtime override can no longer make the
  whitelist ordering ineffective.

## 1.0.13 - 2026-08-31

- Fixed optional missing campaign/segment whitelist files being logged as a
  warning every scheduler cycle. Empty optional files now leave the rings
  unchanged without obscuring operational errors.

## 1.0.12 - 2026-08-31

- Changed segment and campaign whitelists into queue-priority only. A
  whitelisted entity enters its priority ring when Mautic has work to process,
  but no longer gains a periodic execution schedule merely by being listed.
  Explicit `realtime` settings remain the opt-in mechanism for timed runs.

## 1.0.11 - 2026-08-31

- Added revisioned MCC canonical desired-state synchronization for live runtime
  settings. MCD now uses optimistic revision matching, so a stale disconnected
  or reinstalled local configuration cannot overwrite a newer confirmed MCC
  change after reconnecting.
- Added immutable `instance_uid` desired-state records for every supported
  instance-scoped runtime map. Their settings follow an instance to a new host,
  apply without restarting the daemon, and are acknowledged back to MCC with
  source, revision, agent version, applied host, time, status, and error.
- Promoted existing root/domain-keyed MCC instance maps into UID records on
  agent contact while retaining host JSON compatibility for older agents.

## 1.0.10 - 2026-08-31

- Added a marker-only scheduled bootstrap mode. A successful one-time repair
  can now be retained without treating pre-existing rows as proof that the
  repair already ran.
- Viber statistics now run `viber:stats:update --backfill` after every
  `Enabled` transition, then return to routine `viber:stats:update` runs.
  Explicitly disabling Viber Stats still prevents both commands.

## 1.0.9 - 2026-08-31

- Fixed scheduled plugin operations to require both the installed bundle path
  and a non-missing Mautic plugin registry record. A stale Viber directory can
  no longer make `viber:stats:update` executable.
- Added regression coverage for the registry gate and packaged generic plugin
  scheduler path.

## 1.0.7 - 2026-08-31

- Promote local runtime override edits to MCC `desired` state so the canonical operator change is preserved and propagated back to the active MCD instance. Keep `observed` state telemetry-only and retain live application without requiring a daemon restart.

- Removed the host-local AnanasRS campaign worker test from the public agent
  suite. The script is not MCD functionality and remains owned by its single
  runtime host; agent runtime behavior and version are unchanged.

## 1.0.6 - 2026-08-31

- Split bulk Fail2ban updates by IP family. A shared blocklist containing both
  IPv4 and IPv6 addresses now creates separate commands and nftables sets
  instead of failing on a mixed-family comparison.

## 1.0.5 - 2026-08-31

- Compared blocklist and allowlist networks only within the same IP family, so
  mixed IPv4/IPv6 MCC allowlists cannot interrupt Fail2ban reconciliation.

## 1.0.4 - 2026-08-31

- Reconciled large MCC security blocklists through bounded bulk Fail2ban
  commands instead of one process per address. Lists containing several
  thousand entries now converge quickly without blocking the daemon loop.

## 1.0.3 - 2026-08-31

- Added authenticated MCC security blocklist polling and exact reconciliation
  into a dedicated permanent `mcc-global` Fail2ban jail. Central additions,
  removals and allowlist changes propagate without giving agents Cloudflare or
  Wazuh credentials; stale/unavailable snapshots never trigger mass-unban.
- Added an independently operating aggressive SSH jail with progressive bans
  and centrally synchronized ignore addresses. Fail2ban is installed only on
  explicitly targeted hosts, and the all-ports nftables action also protects
  direct SMTP/SSH services while Cloudflare remains the web enforcement plane.

## 1.0.2 - 2026-08-31

- Constrained Symfony Amazon SES and SendGrid mailer bridges to the installed
  `symfony/mailer` minor branch, preventing unconstrained Composer resolution
  from selecting incompatible Symfony generations on Mautic 6.
- Retried Mautic 6 plugin reload exactly once when MySQL DDL was applied but
  Mautic reported `There is no active transaction`; other versions and reload
  failures remain fail-fast.

## 1.0.1 - 2026-08-30

- Blocked Composer Mautic 6 to 7 upgrades unless the observed active database
  is MySQL 8.4+ or MariaDB 10.11+. Detection is fail-closed and the agent
  repeats it immediately before maintenance; MCD never upgrades the database.
- Migrated active cron paths and referenced wrapper scripts from the ZIP source
  root to the new Composer root, retired the removed `mautic:emails:send`
  command for Mautic 5/6/7, and added rollback plus post-write stale-root
  guards.
- Gated global `mautic:emails:send` reconciliation on independently confirmed
  runtime, Composer lock, discovered major and layout evidence. Mautic 4 keeps
  its command; Mautic 5/6/7 disable it; unknown or conflicting evidence makes
  no cron change.
- Required the same independent evidence before applying the Mautic 6
  ReloadHelper core patch, preventing stale inventory from modifying Mautic 7.

## 1.0.0 - 2026-08-30

- Introduced the two-axis Mautic runtime matrix: host or Docker runtime,
  independently combined with ZIP or Composer installation layout.
- Added root-owned Docker descriptors with explicit capabilities, plugin and
  Composer metadata paths, exact image references and a trusted migration
  adapter contract. Console jobs, plugin operations, dependency checks,
  backups and migrations now route or fail closed from that contract instead
  of guessing host paths.
- Added Docker-to-compatible-Docker migration relay support. Target selection
  requires a running Docker daemon, the exact trusted adapter and a locally
  synchronized source image; adapter validation happens before any requested
  target cleanup.
- Added host readiness reporting for Docker and an explicit, opt-in Docker
  service profile. Docker remains optional for ordinary host-native Mautic
  hosts.

## 0.11.47 - 2026-08-29

- Extended the startup service migration to `mcd.service.d/*.conf` drop-ins,
  preventing a legacy `KillMode=process` override from defeating the corrected
  main unit. Unit and drop-in changes share rollback and one daemon reload.

## 0.11.46 - 2026-08-29

- Run the existing-unit `KillMode=control-group` migration at daemon startup,
  after the updated source is active. This fixes the updater bootstrap case in
  which the previous running version cannot execute newly downloaded code.

## 0.11.45 - 2026-08-29

- Migrated existing `mcd.service` units from `KillMode=process` to
  `KillMode=control-group` during self-update, so a daemon restart also stops
  its managed flock, sudo, and PHP child processes.
- Aligned the MCC installation template with the same process-group shutdown
  contract and added regression coverage for idempotent unit migration.

## 0.11.44 - 2026-08-29

- Reconciled production fixes that had remained on divergent release branches
  instead of the canonical agent tree.
- Prevented per-instance Mautic version probes from walking into a shared
  parent installation and reject cached versions whose major differs from the
  discovered instance major.
- Disabled the unsafe path-agnostic `nginx-4xx-scan` fail2ban jail while
  preserving bans independently held by other jails.
- Restored managed web-ingress firewall ordering, persistent embedded-form
  status, and filtering of incomplete plugin directories left after removal.
- Added a one-hour default ceiling for isolated priority segment updates,
  avoided segment-link foreign-key races, and made agent restarts terminate
  the complete managed process group.
- Kept all new behavior covered by focused regression tests while preserving
  the 0.11.43 cron ownership contract for `mautic:emails:send`.

## 0.11.43 - 2026-08-29

- Restored the permanent cron ownership contract for `mautic:emails:send`
  after the public 0.11.42 package regressed to an older cron-reconciliation
  implementation. MCD never migrates or schedules Mautic file-spool delivery.
- Active hosts automatically restore only MCD-commented
  `mautic:emails:send` workers. Other managed and manually disabled cron jobs
  remain unchanged.
- Added release regression coverage for direct workers, wrapper scripts,
  marker recovery and mixed managed cron content.
- Reconciled the previously divergent public package before release, retaining
  fresh-install mail parameter creation and MCC queue/Postmaster telemetry.

## 0.11.42 - 2026-08-28

- Restricted direct external SMTP from unprivileged host processes and blocked
  forwarded/container SMTP while own-host mail is enabled. Mautic continues to
  submit through the local managed MTA, and the MTA retains outbound delivery.

## 0.11.41 - 2026-08-28

- Aligned the managed public Sendmail SMTP greeting/HELO identity with the
  configured own-host mail hostname and PTR without changing the OS hostname.

## 0.11.40 - 2026-08-28

- Fixed own-host mail FCrDNS validation to query public DNS resolvers before
  the host resolver, preventing `/etc/hosts` or local resolver aliases from
  producing a false PTR mismatch.

## 0.11.39 - 2026-08-28

- Hardened own-host mail identity: activation now requires forward-confirmed
  IPv4 reverse DNS for the host MTA name, and runtime status reports the
  resolved A/PTR identity.
- Restricted managed outbound Sendmail and Postfix delivery to IPv4 until a
  separately validated IPv6 mail identity is configured.
- Disabled SMTP AUTH advertisement on the public Sendmail listener used only
  for managed bounce and feedback-loop reception.
- Fixed activation on valid Mautic installations whose `local.php` parameters
  array does not yet contain mailer keys.

## 0.11.38 - 2026-08-27

- Ensure Mautic 6 to 7 upgrades install or update `MauticLocaleFixBundle`,
  publish its integration, and enable Gmail image-proxy counting.
- Preserve all other Locale Fix integration settings during the migration.

## 0.11.37 - 2026-08-27

- Fixed Nginx vhost parsing when a location regex contains `#`, which is valid
  Nginx syntax. MCD now reaches the PHP FastCGI handler and can safely add the
  managed `/form/` route instead of reporting a false missing-upstream block.

## 0.11.24 - 2026-08-20

- Added safe adoption of compatible existing Nginx form locations. When MCC
  supplies an allowed HTTPS origin, MCD replaces only the known CSP and CORS
  directives inside that form location with a marked managed header block;
  the form handler and unrelated directives stay unchanged.
- Unsupported form locations, custom origin logic, multiline security
  directives, missing FastCGI handlers and ambiguous routes are reported as
  blocked without touching the vhost. Each instance is planned fully before
  any of its vhosts is written.

## 0.11.23 - 2026-08-20

- Added a standard managed Nginx `/form/` route to new Mautic image installs
  and completed instance migrations. The secure default permits only
  `frame-ancestors 'self'` and does not enable browser CORS.
- Added an opt-in per-instance form embedding policy. MCC can set exact HTTPS
  domain origins separately for CSP `frame-ancestors` and CORS; the agent
  validates, tests and reloads Nginx atomically before applying it.
- Protected custom form routes: an existing unmanaged `location /form/` is
  reported as blocked and remains untouched. Failed Nginx validation or reload
  restores every modified vhost from a timestamped backup.

## 0.11.22 - 2026-08-20

- Added opt-in per-instance realtime subsets inside segment and campaign
  whitelist settings. Realtime segment, campaign-rebuild and campaign-trigger
  work uses separately bounded executor capacity outside the normal scheduler
  budget, so regular priority work cannot occupy the dedicated slots.
- Added independent realtime intervals for segment rebuild, campaign rebuild
  and campaign trigger. The executor now applies the selected lane interval to
  the shared launch guard, allowing a realtime subset to run faster than the
  host's normal full-scan and campaign-audit cadence without changing other
  entities.
- Kept exact-task and campaign-root file locking across realtime and normal
  dispatch, preventing duplicate execution when both schedulers observe the
  same whitelisted ID.

## 0.11.20 - 2026-08-18

- Fixed targeted Composer preflight to keep the newly required runtime package
  in `composer.json` alongside its updated lock entry while restoring the
  original private VCS repository list. A successful dependency repair can no
  longer leave `composer.json` and `composer.lock` inconsistent.
- Added failure-path coverage proving both Composer files are restored when the
  targeted update fails.

## 0.11.19 - 2026-08-18

- Fixed Composer plugin preflight when a Mautic instance contains private
  `git.sales-snap.com` repositories that are unavailable to the runtime
  `www-data` Composer process. Each missing runtime package is now resolved in
  a targeted two-step operation (`require --no-update`, then package-only
  `update --with-dependencies`) with private VCS repository entries temporarily
  excluded from resolution. The original `composer.json` is always restored;
  a failed update also restores the original lock file. This prevents a
  dependency check such as `nikic/php-parser` from failing because Composer
  tried to authenticate against unrelated private plugin repositories.
- Added regression coverage for repository restoration and the targeted
  Composer command shape. The fix applies globally to all plugin operations
  using the agent preflight and does not change Mautic plugin source code.

## 0.11.18 - 2026-08-16

- Matched native Mautic scheduling for campaign actions stored with
  `trigger_mode=date` and a null `trigger_date`. Mautic executes that shape
  from the campaign comparison time; MCD now keeps root and decision-no action
  campaigns in the trigger ring instead of rejecting them as stale.
- Added regression coverage for Najboljamama campaign 16 and automatic
  replacement of persisted trigger SQL with the corrected packaged default.

## 0.11.17 - 2026-08-14

- Fixed first-run message-queue adoption on the immutable agent configuration.
  The daemon now replaces the frozen runtime snapshot instead of assigning to
  it, so default, cron and legacy-job migrations complete and synchronize with
  MCC without interrupting the scheduler loop.

## 0.11.16 - 2026-08-14

- Added dedicated per-instance `mautic:messages:send` scheduling for Mautic
  5, 6 and 7. The safe default is disabled with a 3600-second interval;
  Mautic 4 and unrelated scheduled jobs retain their existing behavior.
- Added startup reconciliation for direct and wrapper cron entries. Matching
  modern-instance cron is commented, its enabled state and cadence are moved
  into MCC/MCD settings, and passive mode restores only entries marked by this
  reconciler. Existing canonical settings always win. A temporary MCC outage
  keeps the adopted worker active locally and retries desired-state sync.
- Migrated legacy `[[jobs]]` message-queue settings when no canonical setting
  exists and suppressed the old worker afterward so it cannot bypass the new
  per-instance checkbox.
- Added count-only queue telemetry for total, due, future and exhausted rows.
  Database connections are closed deterministically and recipient addresses or
  message payloads are never included in agent state.

## 0.11.15 - 2026-08-14

- Added the generic MCC-catalog plugin operation scheduler with installed
  bundle checks, typed interval/quiet-window/cron values and safe declarative
  Mautic console argument templates.
- Replaced built-in Viber, Oracle Hospitality and Leuchtfeuer Housekeeping
  policy, plus the composite Mail.ru Postmaster worker, with catalog
  definitions. Plugin commands, defaults and cron match tokens no longer live
  in MCD scheduler branches or host configuration templates.
- Added generic legacy cron reconciliation and canonical migration. Existing
  tile/runtime values, including explicit enabled/disabled state and all mapped
  parameters, take precedence; otherwise a matching cron enables the operation
  and imports common minute/hour/daily cadences.
- Added catalog-defined multi-task operations, conditional arguments, safety
  task lanes, read-only database guards and bootstrap completion probes so
  complex plugin workflows use the same generic scheduler.

## 0.11.14 - 2026-08-14

- Deferred campaign email-counter reconciliation while any native campaign
  process is active on the same Mautic root. This prevents an intermediate
  `email_stats` count from being written into `emails.sent_count` while Mautic
  still holds its own pending counter increment, which produced false doubled
  UI statistics on Enoteka campaign 25 without duplicate recipient sends.
- Added compact, rate-limited evidence for deferred reconciliations; a deferred
  check does not consume the normal reconciliation cooldown and is retried
  after native campaign work exits.

## 0.11.13 - 2026-08-14

- Bounded fairness-watchdog logging after live farm validation. Persistent
  promoted work now logs once on transition and at most once per hour with a
  count, oldest wait and three-root sample; the complete bounded state remains
  available in MCC telemetry without per-minute full-root log lines.

## 0.11.12 - 2026-08-14

- Split the high-density farm scheduler into hardware-specific profiles:
  `farm-tiny`, `farm-mini`, `farm-midi`, `farm-maxi`, `farm-hiload` and
  `farm-ultra`. Each class keeps the elastic one-slot emergency reserve and
  per-instance fairness while MCC derives its actual limits from host CPU/RAM.
- Removed the generic `farm` profile from operator-facing CLI and MCC choices.
  Existing generic assignments are forcibly migrated on service reconciliation
  to the matching hardware class, including hosts pinned in manual mode.

## 0.11.11 - 2026-08-14

- Replaced isolated segment and campaign host lanes with one elastic scheduler
  budget. Segment work can use idle capacity while preserving one emergency
  slot for imports and campaign work on hosts with at least two slots.
- Added independent per-instance concurrency for high-density farms. When an
  instance has pending import or campaign work, its segment admission leaves a
  local slot available so background segment rebuilds cannot block priority
  work from the same Mautic database.
- Added `farm` and `ultra` profiles. MCC sizes farm hosts up to one scheduler
  command per CPU within a separate RAM budget, while standard hosts retain the
  conservative half-CPU limit; 24+ CPU, 96+ GiB hosts can use `ultra`.
- Removed repeated whitelist-root dispatch within one scheduler cycle and added
  a five-minute fairness watchdog. Compact watchdog state is reported in MCC
  scheduler telemetry without accumulating per-cycle log files.

## 0.11.10 - 2026-08-14

- Fixed host-wide import starvation when priority segment instances repeatedly
  filled the shared segment/import lane before a regular instance could claim
  a slot. Instances with a known pending Mautic import now dispatch first while
  preserving existing rotation and whitelist ordering for all other work.
- Added regression coverage for the Mensa import 56 shape: a regular instance
  with a delayed import must precede duplicated whitelist-segment dispatches
  from another instance, and normal ordering remains unchanged without pending
  imports.

## 0.11.9 - 2026-08-13

- Preserved `mautic:emails:send` cron workers in active MCD profiles. MCD does
  not replace the Symfony/SwiftMailer file-spool consumer, so disabling these
  workers could leave campaign messages queued after a successful trigger.
- Added automatic repair for active hosts where an earlier MCD release had
  commented those workers. Only MCD-created generic markers immediately
  wrapping an email-spool consumer are restored; campaign, segment, import,
  email-fetch, Viber, OHIP and other MCD-owned cron jobs remain disabled.

## 0.11.8 - 2026-08-13

- Added selective logical-issue remediation. Explicit segment IDs are accepted
  only when a fresh scan confirms that they belong to the referenced active
  issue; the maintenance-compatible command without IDs still disables the
  complete affected branch.
- Added bounded multi-issue remediation in one database transaction. Every
  selected segment receives its applicable issue IDs and reasons, while MCD
  retains per-issue before/after audit rows and rescans after the commit.

## 0.11.7 - 2026-08-12

- Fixed the contact-count report's Python 3.10 compatibility by using
  `timezone.utc`; the `0.11.6` test build stopped at the pre-switch import gate
  and was never activated or promoted to `approved`.

## 0.11.6 - 2026-08-12

- Added the read-only `report:contact-count` command for one selected Mautic
  instance. It counts each database contact once when `email` or `mobile`
  contains a non-whitespace value and excludes rows where both fields are
  empty.
- Added independently checkable totals for email-only, mobile-only, both-field
  and excluded-empty rows. The command fails on an internally inconsistent
  aggregate instead of returning an unreliable billing count.

## 0.11.5 - 2026-08-12

- Added an instance logical-issue guard for Mautic segments. MCD scans every
  five minutes for dependency cycles, self-references and known invalid filter
  values, persists concrete reasons and affected IDs, and prevents unsafe
  segments and their published descendants from entering the scheduler.
- Added bounded `logical-issues scan/status/remediate` CLI operations. The
  guarded remediation disables the affected published branch transactionally,
  appends the reason, issue ID, operator and UTC timestamp to each segment, and
  retains a compact before/after audit history.
- Classified missing and unpublished segment dependencies as visible warnings
  without scheduler blocking or automatic remediation. These configurations
  can be stale but do not always make native Mautic execution fail.
- Stored logical-issue state through the existing `runtime_sync` backend:
  `mysql_hybrid` remains authoritative and keeps the bounded SQLite fallback
  shadow. Detailed state is limited to 100 active issues and 50 remediation
  actions while the complete compact blocked-ID set remains effective. State
  for deleted instance roots is pruned from both backends during inventory
  refresh so the legacy fallback cannot become an unbounded history store.
- Added logical-issue snapshots to the authenticated MCC state payload. Cycle
  detection is iterative and the fleet scan reads only the segment fields it
  needs, avoiding recursion and unnecessary data on large installations.

## 0.11.4 - 2026-08-11

- Remove stale Docker runtime instances from local inventory when their
  root-owned descriptor is deleted. A rescan now reconciles descriptor-backed
  rows as authoritatively as normal autodiscovery rows, so deletion disappears
  from MCD and the next MCC state push.

## 0.11.3 - 2026-08-11

- Preserve descriptor-declared numeric container ownership in the filesystem
  permissions guard. Docker bind mounts are no longer rewritten to the host
  PHP user, while host-native Mautic permissions remain unchanged.
- Added an optional host-side database endpoint to Docker runtime descriptors.
  MCD uses it for scheduler queries, administrator management and other host
  database work while console commands continue through scoped `docker exec`.
- Made the manual permissions repair command honor the Docker descriptor user,
  including numeric `uid:gid` identities that do not exist in the host passwd
  database.

## 0.11.2 - 2026-08-11

- Account for firmware- and kernel-reserved RAM when mapping Linux `MemTotal`
  to nominal provider hardware classes. A 2-vCPU/4-GiB CX23 that exposes about
  3.73 GiB to Linux now correctly selects `mini` instead of `tiny`, while the
  lower CPU-or-memory class remains authoritative.

## 0.11.1 - 2026-08-11

- Added automatic scheduler-profile selection from the host's logical CPU and
  physical memory class. Fresh passive installations now select the
  conservative `tiny`, `mini`, `midi`, `maxi` or `hiload` baseline on MCD
  startup and retain the automatic policy across normal restarts.
- Persisted profile-selection authority independently of the MCD configuration.
  A CLI, MCC or direct configuration choice locks the host in manual mode, so
  later hardware detection cannot overwrite an operator decision. Existing
  active installations are migrated as manual and remain unchanged.
- Added `mcd-cli profile auto` to explicitly restore hardware-managed selection,
  and extended `mcd-cli profile status` with the selection mode, detected CPU,
  detected memory and current recommendation.

## 0.11.0 - 2026-08-11

- Added root-owned runtime descriptors for isolated Docker Mautic instances.
  Inventory and MCC state now retain the container identity, in-container root,
  console path, numeric execution user and immutable image reference without
  publishing database secrets.
- Routed normal MCD console execution through an exact `docker exec` target for
  descriptor-backed instances. Host PHP/FPM behavior remains unchanged for
  legacy installations, and Docker containers never receive the Docker socket.
- Kept host PHP-FPM tuning and in-place Mautic upgrades away from immutable
  container instances. Their application version is managed by the platform
  image while MCD continues database, status and console-level management.

## 0.10.35 - 2026-08-11

- Added instance-scoped Oracle Hospitality synchronization. When
  `OracleHospitalityBundle` is installed, active MCD profiles run
  `ohip:sync` every six hours by default; MCC runtime overrides can disable
  the task or change its interval per instance.
- Added `ohip:sync` to active-profile cron reconciliation so a legacy cron
  cannot race the MCD-managed task. Instances without the plugin remain
  untouched.

## 0.10.34 - 2026-08-11

- Dispatch campaigns returned by the native due query through an independent
  priority lane. Due triggers no longer wait behind campaign rebuild-ring
  traversal or segment activity on large shared hosts.
- Defer hourly native campaign fallback while an untracked Mautic campaign
  console process is still alive for the same instance. Child processes that
  outlive their tracked parent can no longer overlap a global fallback run.

## 0.10.33 - 2026-08-10

- Reserve an independent scheduler slot for the seven-second Mail.ru Postmaster
  campaign-guard refresh. Segment and campaign rebuild saturation can no longer
  starve the safety poll that must stop a campaign before its next send batch.

## 0.10.32 - 2026-08-10

- Detect active Mail.ru Postmaster campaign guards by Mautic's `type` column;
  `event_type` contains the generic value `action` and previously prevented the
  maximum-frequency domain polling loop from starting.

## 0.10.31 - 2026-08-10

- Prevented `nginx-4xx-scan` Fail2ban false positives caused by Safari and
  other browsers repeatedly requesting missing root icons. MCD now adds a
  narrow ignore rule for `404` responses on `/apple-touch-icon*.png` and
  `/favicon.ico`, reloads only that jail, and preserves all other 4xx scan
  detection.

## 0.10.30 - 2026-08-10

- Treat an already populated Mail.ru Postmaster statistics table as an
  existing installation when MCD first detects the plugin. MCD records that
  adoption and continues with the current-month job instead of launching an
  immediate 365-day backfill; fresh empty installations still receive the
  first full missing-month pass.

## 0.10.29 - 2026-08-10

- Run one persistent, per-instance 365-day Mail.ru Postmaster full
  synchronization on the first MCD launch after the plugin is detected. The
  completion marker is written only after a successful command; subsequent
  routine work returns to the current month and the configured weekly pass asks
  the plugin to fill only domain/month periods without completion evidence.
- Separate bulk Postmaster jobs from active campaign-guard refresh jobs. A
  first or weekly full sync can no longer delay the seven-second, current-day,
  single-domain polling used by campaign stoppers.

## 0.10.28 - 2026-08-10

- Fixed explicit plugin `update` operations to repair bundles in `BROKEN`
  registration state, matching the automatic and reinstall recovery paths.

## 0.10.27 - 2026-08-10

- Added a Mautic 7 plugin-registration preflight that installs the missing
  `nikic/php-parser:^5.0` runtime package through Composer without running
  project scripts. This prevents Symfony's translation extractor from aborting
  `mautic:plugins:reload` with `PhpParser\\NodeVisitor` missing.
- Made catalog status registration-aware: copied files and a matching MCD
  package marker are now reported as `BROKEN` until the bundle has an active
  `{prefix}plugins` row. Plugin operations also fail explicitly if Mautic exits
  without registering every selected bundle.
- Added regressions for the Fruške Terme Oracle Hospitality installation where
  package files were present but the plugin was absent from `/s/plugins`.

## 0.10.26 - 2026-08-10

- Kept campaigns with future scheduled event logs in the trigger ring and
  armed a precise database-clock wake-up for their earliest trigger date.
  Date actions can no longer wait for the next minute-level published audit
  after becoming due, allowing the hourly native fallback to win the race.
- Cached future wake-ups suppress repeated due-guard SQL probes and emit one
  compact scheduling line per changed date, including across repeated rebuilds,
  instead of stale-skip log churn.
- Added a production-shape regression for Merkurosiguranje campaign 56 with
  468 scheduled root email actions due at 07:00 UTC.
- Prioritized a campaign trigger immediately after its successful rebuild
  before round-robin launches another rebuild. This closes the Fruške Terme
  campaign 13 race where native fallback sent four newly rebuilt contacts
  before MCD revisited the trigger lane.

## 0.10.23 - 2026-08-10

- Added instance-scoped Mail.ru Postmaster synchronization for active MCD
  profiles. The scheduler auto-detects `MauticMailRuPostmasterBundle`, runs its
  current-month sync every ten minutes by default, and never dispatches the command
  to sibling instances without the plugin.
- Added a published-campaign probe for Postmaster guard nodes. While at least
  one guard campaign is active, MCD runs the plugin's domain-only refresh every
  seven seconds; inactive instances make no high-frequency API calls.
- Added stable global and per-instance runtime overrides for the Postmaster
  sync switch and interval. Active-profile cron reconciliation now recognizes
  the standalone Postmaster fallback command and prevents duplicate runs.
- Added a minute-level scheduled-full probe. The plugin performs its 365-day
  fallback at the weekday/time configured in Mautic, while optional MCC
  weekday/time values override the plugin schedule per instance.

## 0.10.22 - 2026-08-09

- Capped the exclusive native campaign fallback at 30 minutes and restarted
  its interval from completion, failure, or timeout. A looping native trigger
  can no longer hold the campaign root lock for hours or relaunch immediately
  after it is terminated.
- Added one bounded retry for automatic exact campaign triggers when the host's
  general task retry setting is `1`. Other automatic and manual task retry
  behavior is unchanged.
- Added regressions for the Alex Personal fallback lock incident that delayed
  segment-backed rebuild and trigger work for campaigns 165 through 168.

## 0.10.21 - 2026-08-09

- Fixed campaign trigger priority so IDs returned by the narrow due query are
  promoted ahead of recurring all-published audit entries. A large stale audit
  ring can no longer reinsert completed campaigns at the front on every plan
  refresh and starve a newly due campaign.
- Extended the campaign trigger progress watchdog with campaign-scoped
  `email_stats` progress. Long native sends are no longer killed while email
  rows are advancing but Mautic has not yet committed matching event-log
  updates.
- Added production-shape regressions for Abelapharm campaigns 1065/1066 and
  Hotelsunce campaign 57.

## 0.10.20 - 2026-08-08

- Fixed scheduled campaign event-log due checks to follow native timestamp
  storage by Mautic generation: Mautic 4 keeps the validated instance-local
  compatibility baseline, while Mautic 5, 6, and 7 compare Doctrine campaign
  event-log timestamps in UTC.
- Prevented modern Mautic exact triggers from running early and entering their
  repeat cooldown before the event is actually due. This closes the observed
  Hotelsunce campaign 35 gap where native fallback processed event 110/111
  before MCD's next exact trigger pass.

## 0.10.19 - 2026-08-08

- Fixed priority campaign-whitelist dispatch to follow native Mautic's active
  publication window. Unpublished, not-yet-published, and expired campaign IDs
  are removed from the priority rebuild/trigger lane within one audit interval
  instead of being rebuilt indefinitely merely because they remain configured
  in the whitelist.
- Guarded the whitelist publication check to fail closed and rate-limit its
  error log, preventing both unsafe launches and repetitive log noise when the
  instance database cannot be checked.

## 0.10.18 - 2026-08-07

- Fixed campaign audit-ring publication filtering to use Mautic's UTC database
  timestamps. On hosts with a non-UTC timezone, a newly scheduled campaign can
  no longer be rebuilt before `publish_up`, treated as already known, and then
  delayed behind the existing ring when its real publication time arrives.

## 0.10.17 - 2026-08-07

- Page-hit orphan cleanup now fails closed when the managed
  `idx_mcd_ph_lead_date` index is absent. This prevents an unindexed cleanup
  preview from scanning very large `page_hits` tables and blocking scheduler
  reaping, import polling, and MCC user-task dispatch.

## 0.10.16 - 2026-08-07

- Fixed State DB bootstrap to use the same effective configuration, including
  MCC runtime overrides, for status checks, database validation, connection
  defaults, and schema creation. This prevents valid `mysql_hybrid` bootstrap
  from being rejected when state settings are supplied by MCC rather than the
  local TOML file.

## 0.10.15 - 2026-08-07
- Fixed: explicit `state-db init` now accepts an existing state database whose
  MCD tables or schema-version record have not been initialized. This repairs
  interrupted `mysql_hybrid` rollouts instead of leaving the agent in noisy
  SQLite fallback mode.
- Guarded: bootstrap remains unavailable when MySQL state is already active or
  when the probe failed for an unrelated connection error.

## 0.10.14 - 2026-08-07
- Fixed: own-host MX delivery now opens TCP/25 through an exact, comment-tagged
  firewall rule instead of leaving Sendmail unreachable behind a default-drop
  host policy.
- Guarded: a dedicated idempotent systemd unit restores that one rule after a
  reboot and removes only its own rule when the final own-host domain is
  disabled. Local submission on 2525 and all unrelated firewall rules remain
  unchanged.

## 0.10.13 - 2026-08-07
- Fixed: mail-profile test delivery no longer supplies an empty Symfony
  return-path address when Mautic intentionally leaves the setting blank.
- Added: own-host mail can receive delivery-status and feedback-loop reports
  at exact `bounce@`, `fbl@` and `abuse@` addresses. MCD validates the managed
  domain, parses the message and writes idempotent email DNC state directly to
  the matching Mautic database.
- Guarded: Sendmail keeps the pre-existing system relay and submission
  settings while MCD owns a separately marked inbound listener and exact
  virtual-address map. Unknown recipients are rejected, changes have persistent
  baselines, and disabling the final own-host domain restores the original
  MTA files and removes the receive helper.

## 0.10.12 - 2026-08-06
- Fixed: own-host mail runtime uses `datetime.timezone.utc` instead of the
  Python 3.11-only `datetime.UTC` alias. MCD packages now pass pre-switch import
  smoke on supported Python 3.10 hosts, including Booka.

## 0.10.11 - 2026-08-06
- Added: mail-profile apply can preserve credentials from the active Mautic
  `mailer_dsn` while changing between `mautic+ses+api`, `ses+api` and
  `ses+https`. Blank credential fields retain their current values and supplied
  fields replace only the corresponding value.
- Guarded: SES API credentials are never reused for `ses+smtp`, which requires
  its own SMTP username and password.
- Added: after apply and test succeed, MCD reports the effective credentials to
  MCC through the authenticated mail-profile status endpoint for encrypted
  control-plane storage. Credentials are not included in command output.

## 0.10.10 - 2026-08-06
- Fixed: `MauticZenderBundle` is no longer classified as an email sender. MCD
  derives sender state only from the active `local.php` mail transport or DSN,
  including own-host `mcd-mail-submit` sendmail configurations.
- Added: instance state reports a secret-free snapshot of the active mail
  configuration: method, source file, region/host/port, sender addresses and
  credential-presence flags. DSN credentials are never included in state.
- Added: Amazon SES profiles support `mautic+ses+api`, `ses+api`, `ses+https`
  and `ses+smtp`. MCD installs the Symfony Amazon Mailer and HTTP client where
  required and provides a package-only mail profile preflight operation.

## 0.10.9 - 2026-08-06
- Fixed: managed image database-user cleanup now uses the agent's local SQL
  literal quoting helper instead of failing safely with an undefined helper
  after the database was removed.
- Regression: executes the complete convention-matched database and user
  cleanup path and verifies the exact guarded SQL statements.

## 0.10.8 - 2026-08-06
- Fixed: image deployment accepts a pre-existing instance parent only when it
  is an empty directory, while non-empty or non-directory paths remain blocked.
- Fixed: a failed image deployment rolls back only resources created for that
  attempt: local mail, exact nginx entries, a dedicated certificate, webroot,
  database and the convention-matched database user.
- Fixed: full instance deletion removes an empty dedicated instance parent and
  drops the matching MCD-image database user and dedicated single-domain
  certificate; custom database users and shared certificates are never
  inferred or removed.
- Safety: image database users must not pre-exist and are created without
  `IF NOT EXISTS`, preventing a stale account password from being paired with
  newly generated Mautic credentials.

## 0.10.7 - 2026-08-06
- Fixed: full instance deletion now disables any matching MCD-managed local
  mail domain before database, vhost or file removal. Missing local-mail state
  is an idempotent no-op; a real mail cleanup failure blocks deletion.
- Fixed: vhost deletion removes both the exact `sites-enabled` entry and its
  exact domain-matched `sites-available` configuration while preserving every
  unrelated virtual host. An nginx preflight blocks changes when the existing
  host configuration is already invalid.
- Regression: covers local-mail cleanup ordering, exact vhost ownership and
  complete enabled/available removal.

## 0.10.6 - 2026-08-06
- Fixed: URL-encoded mailer DSNs are escaped for the Symfony parameter bag, so
  values such as `%2Fusr` cannot be interpreted as missing DI parameters and
  break Mautic web requests after mail-profile activation.
- Fixed: direct mail tests decode Symfony percent literals before constructing
  the transport; the same handling covers own-host, SMTP, SES and SendGrid.
- Safety: own-host activation must now rebuild the Mautic cache before success.
  Rollback restores `local.php` and rebuilds the cache from the restored config.
- Regression: covers own-host and credential-bearing external DSN persistence.

## 0.10.5 - 2026-08-06
- Fixed: the isolated Sendmail systemd unit now stops with `SIGINT`, which the
  daemon handles immediately and cleanly, instead of waiting for the default
  90-second `SIGTERM` timeout when a second instance refreshes DKIM mappings.
- Safety: the isolated service has a bounded 15-second stop timeout; the system
  Sendmail service, configuration and queue remain outside MCD ownership.
- Regression: verifies the generated unit carries both stop safeguards.

## 0.10.4 - 2026-08-06
- Fixed: profile activation accepts hosts whose observed local hostname differs
  from their canonical MCC inventory name. Private material remains protected
  by MCC bearer authentication, source-IP binding and canonical profile/local-
  mail ownership checks before it reaches the agent.
- Regression: covers a legacy host identified locally as `Nikola` and
  canonically as `host-46-62-129-237` without weakening instance-domain scope.

## 0.10.3 - 2026-08-06
- Fixed: isolated Sendmail now compiles `QUEUE_DIR` as an m4 definition and
  refuses activation unless the resulting `sendmail.cf` contains the exact
  dedicated `/var/spool/mqueue-mcd` path. It cannot consume a sibling system
  Sendmail queue.
- Fixed: own-host SMTP submission normalizes message line endings to CRLF
  before passing byte payloads to Python `smtplib`, preventing Sendmail from
  rejecting Symfony Mailer messages with `Bare linefeed (LF) not allowed`.
- Regression: verifies the compiled queue guard, untouched system Sendmail
  configuration, isolated listener selection and SMTP wire line endings.

## 0.10.2 - 2026-08-06
- Fixed: profile activation test mail reads both supported Mautic `local.php`
  layouts: arrays returned by newer releases and the `$parameters = array(...)`
  assignment used by older installations. The test no longer rolls back a
  valid own-host configuration because it misread an existing Mautic config.
- Regression: covers both local configuration layouts while retaining the
  mandatory real-message test before profile activation.

## 0.10.1 - 2026-08-06
- Fixed: own-host activation on an existing Sendmail host now uses an isolated
  loopback listener, generated configuration and dedicated queue. MCD no longer
  edits `/etc/mail/sendmail.mc`, restarts the system Sendmail service or moves
  its existing queue, so sibling instances retain their current relay path.
- Fixed: the Mautic quota wrapper now reaches root-owned MCC mail state through
  a single validated sudoers helper and submits to the isolated listener. Daily
  and monthly recipient accounting remains atomic without exposing MCD state to
  the shared web user.
- Regression: covers dedicated Sendmail port/queue generation, isolated SMTP
  submission and quota rollback after delivery failure.

## 0.10.0 - 2026-08-06
- Added: MCD applies MCC-stored per-instance mail profiles for own-host mail,
  SMTP, Amazon SES API and SendGrid API. Missing Symfony mailer bridges are
  installed through the existing guarded Composer dependency workflow.
- Added: own-host mail uses per-instance DKIM keys, MCC-provided sender
  settings and atomic daily/monthly recipient quotas. Existing native Sendmail
  hosts remain on Sendmail; clean new hosts install Postfix and OpenDKIM.
- Added: profile activation sends a real Symfony Mailer test message to the
  current MCC user's email before MCC marks the profile active.
- Safety: MTA, OpenDKIM and Mautic changes roll back on failure; unmanaged MTAs
  are never replaced.
- Changed: instance mail state is stored only under `/etc/mcd/local-mail` and
  `/var/lib/mcd/local-mail`; no instance `.mcd` runtime directory is created.

## 0.9.287 - 2026-08-06
- Fixed: native fallback owns an exclusive per-instance campaign gate for its
  complete global update/trigger pair. MCD exact rebuild/trigger launches use
  the shared side of the same gate and cannot overlap the fallback, while
  segment and import scheduling remain independent.
- Added: an in-process dispatch coordinator closes the admission race before a
  child process starts; the root-level `flock` provides restart and
  cross-process protection. Lock contention defers fallback without recording
  a false native-command error.
- Regression: covers the observed Dexyco overlap between a global fallback
  trigger and exact campaign 176.

## 0.9.286 - 2026-08-06
- Fixed: newly discovered campaigns in the priority lane are inserted ahead of
  previously queued priority work for both rebuild and trigger scheduling. A
  fresh campaign can no longer wait behind an existing shared-host ring before
  its first MCD execution.
- Regression: covers MauticFarm-02 campaign 27 entering the existing
  `22/17/8/10` priority ring while preserving regular-ring round-robin order.

## 0.9.285 - 2026-08-06
- Fixed: the first `recovered_runs` schema migration backfills the cumulative
  counter once from retained durable fallback events. Upgrading from 0.9.283
  through 0.9.284 no longer leaves a permanent canonical/reported telemetry
  gap after a recovery event occurred before the new counter was installed.

## 0.9.284 - 2026-08-06
- Fixed: campaign watchdog and priority timeout termination now signal the
  complete isolated process group. A stopped `flock` wrapper can no longer
  leave its `sudo`/PHP campaign command running indefinitely as an orphan.
- Added: native fallback executions have a six-hour safety timeout and publish
  compact `deferred`/`running` runtime state to MCC, including schedule delay,
  PID, start time and final duration.
- Added: fallback statistics now accumulate `recovered_runs` in addition to
  recovered pending and email-stat deltas, making MCD misses directly countable.

## 0.9.283 - 2026-08-05
- Fixed: campaign trigger due-guard now evaluates scheduled event-log timestamps
  against a timezone-aware baseline (`now_event_log`) derived from the instance
  timezone. UTC-configured instances still use UTC, non-UTC instances use local
  campaign time for due checks.

## 0.9.282 - 2026-08-05
- Fixed: campaign trigger due-guard now treats scheduled event-log timestamps as due
  when they are stored in local time and differ from UTC by the instance timezone.
  This addresses hosts where `campaign_lead_event_log.trigger_date` is shifted
  versus UTC and otherwise appears as future by strict UTC comparison.

## 0.9.281 - 2026-08-05
- Preserved: configured campaign-pressure throttling still reduces the segment
  lane for long or concurrent campaign work. This resource guard is separate
  from scheduler ownership: segment tasks cannot consume campaign slots or
  prevent campaign update, trigger, and rebuild launches.

## 0.9.280 - 2026-08-05
- Changed: host scheduler capacity is split into independent segment/import and
  campaign lanes. Saturated `segment`, `segment_sql`, or `import` work cannot
  block `campaign_update`, `campaign_trigger`, or `campaign_rebuild`, and busy
  campaign workers no longer consume or throttle segment capacity.
- Regression: covers the Prodajadelova rebuild ring `22/17/8/10` on a shared
  host with all segment slots occupied, including isolation in both directions.

## 0.9.279 - 2026-08-05
- Fixed: exact campaign rebuild backlog now temporarily suspends new segment
  admission across the host. Existing segment work drains and frees a scheduler
  slot for campaign membership rebuilds instead of continuously refilling every
  slot and starving due campaigns on shared hosts.
- Regression: covers the Prodajadelova campaigns 8/10/17/22 shape where native
  `campaigns:update` recovered missing campaign membership after MCD rebuilds
  had not launched while segment work saturated the host scheduler.

## 0.9.278 - 2026-08-05
- Fixed: native fallback metrics now inspect the real prefixed Mautic event-log
  schema and support both current and legacy scheduling columns. Metric failures
  are best-effort diagnostics and can no longer suppress native commands.
- Changed: each fallback always runs the global native campaign update and
  trigger commands without campaign or batch arguments. Shared hosts admit only
  one fallback instance at a time and emit one compact completion log.
- Added: fallback outcomes and recovered-work deltas accumulate per instance in
  the agent state database across restarts. MCC receives cumulative totals and a
  bounded recent history with stable event IDs; operation events retain 30 days
  with a 5000-row cap so MCC can persist and deduplicate its canonical aggregate.

## 0.9.277 - 2026-08-05
- Added: an opt-in native campaign safety fallback. Once per configured interval,
  an active MCD host runs global `mautic:campaigns:update` followed by
  `mautic:campaigns:trigger` only after local campaign work is idle.
- Added: one compact MCC signal per fallback run with return code and before/after
  due-event and campaign-email counts, so recovery differences are auditable.
- Fixed: fallback metrics now render each Mautic database table prefix before
  querying, so prefixed installations do not skip the fallback preflight.

## 0.9.275 - 2026-08-03
- Fixed: full MCC state pushes now reload the shared instance inventory before
  serialization. An instance deleted by a separate `mcd-cli` process can no
  longer be reintroduced by the daemon's stale in-memory list.
- Added: full inventory snapshots are explicitly marked as complete. A failed
  inventory read is marked incomplete so MCC never prunes cache rows from a
  fallback payload.

## 0.9.274 - 2026-08-03
- Fixed: priority launches now update the same repeat guard and completion
  markers as normal scheduler launches. This prevents a normal planning pass
  from repeating a just-finished priority segment or campaign task.
- Added: regression coverage for cross-path repeat suppression after a
  successful priority execution.

## 0.9.273 - 2026-08-03
- Added: explicitly whitelisted segment and campaign work now has an internal
  priority dispatcher independent of the full shared-host planning pass.
  Per-task repeat guards still define the requested cadence.
- Fixed: normal and priority dispatch use the same non-blocking execution lock.
  Rebuild and trigger for one campaign also share a lock, preventing duplicate
  execution and rebuild/trigger overlap across the two scheduler paths.
- Fixed: campaign due-work is checked immediately after each priority rebuild
  and then at the configured audit cadence, without idle per-second log noise.

## 0.9.272 - 2026-08-03
- Added: scoped per-instance campaign whitelist settings, matching the existing
  segment whitelist ownership model on shared hosts.
- Fixed: priority instances are revisited between bounded chunks of regular
  instance dispatch. Priority segment and campaign work therefore follows its
  own repeat guard instead of waiting for a complete shared-host traversal.
- Fixed: priority campaign candidates remain in their ring after a successful
  run or a temporary no-due-work preflight. The shared task key and normal
  concurrency limits still prevent duplicate parallel execution.

## 0.9.271 - 2026-08-03
- Fixed: the agent source distribution now includes `requirements.txt`, which
  is required by MCC release validation and by the host self-update installer.

## 0.9.270 - 2026-08-03
- Fixed: active MCD profiles now enforce a one-minute campaign safety audit,
  including hosts carrying the previous five-minute runtime setting. The audit
  still launches native Mautic trigger commands only after exact due-work
  checks, so published but idle campaigns are not executed unnecessarily.
- Safety: passive profiles retain external cron ownership and are unchanged.

## 0.9.269 - 2026-07-31
- Added: MySQL hardware profiles now manage `max_user_connections` together
  with `max_connections`. This removes legacy fixed per-user caps that could
  be too low for dedicated hosts while retaining a plan-derived safeguard.

## 0.9.268 - 2026-07-28
- Fixed campaign scheduler fairness on shared MCD hosts: stale campaign-trigger
  audit candidates now have a bounded preflight scan per scheduler pass, so a
  large stale audit ring cannot delay a newly due campaign on another instance.
- Added regression coverage for the bounded stale scan and for root date-action
  campaigns with existing campaign leads but no event-log rows, matching the
  Neodent campaign 315 shape.

## 0.9.267 - 2026-07-26
- Fixed: the runtime agent version is now kept in sync with package metadata.
  Self-update no longer reports an already-installed release as stale.
- Added: a packaging regression test rejects a release when
  `mcd_agent.__version__` and `pyproject.toml` differ.

## 0.9.266 - 2026-07-26
- Fixed: instance migration now discovers and transfers Let's Encrypt
  certificates by their DNS SANs, so a certificate whose certbot directory is
  named for another covered domain is preserved on the target.
- Safety: migration finalization now requires a current target certificate
  covering the primary domain before DNS cutover. A missing or expired
  certificate leaves DNS unchanged and restores source maintenance instead of
  exposing the target through the default deny vhost.

## 0.9.265 - 2026-07-26
- Fixed: dedicated-host NVMe health uses each controller's reported warning
  and critical composite-temperature limits (`WCTEMP`/`CCTEMP`) instead of one
  overly conservative temperature ladder for every model. Older controllers
  without those fields retain the previous safe fallback thresholds.

## 0.9.264 - 2026-07-24
- Fixed: scheduler monitoring now treats a task as processing only when its
  matching Mautic PHP console process is live. Stale state-db and plan entries
  no longer appear as active work.
- Fixed: monitoring excludes the `sudo` launcher parent, so a Mautic command
  is counted once instead of as both `sudo` and PHP.

## 0.9.263 - 2026-07-24
- Fixed: cleanup now finds and removes known MCD-only `.mcd` directories under
  `/var/www` even when their legacy root is no longer present in MCD inventory.

## 0.9.262 - 2026-07-24
- Fixed: migration away from the retired per-instance Zabbix Mautic-version
  probe also removes its old inactive `mautic.conf.mcd-bak` file.

## 0.9.261 - 2026-07-24
- Fixed: MCD now always runs Mautic console commands through the configured
  host PHP binary and never creates or executes `{instance}/.mcd/php`.
  All PHP limits, upload settings, and timezone now come solely from the
  host CLI `conf.d` configuration.
- Changed: legacy MCD-only instance directories containing the former PHP
  wrapper and version cache are migrated and removed; generated wrappers are
  also cleaned from `/opt/mcd/generated/instances/`.
- Changed: retired the per-instance Zabbix `mautic.version[*]` UserParameter.
  Zabbix remains host monitoring only; MCD's non-migrated version cache lives
  under `/opt/mcd/generated/mautic-version/`.

## 0.9.260 - 2026-07-24
- Fixed: campaign scheduling now compares Mautic's UTC campaign windows,
  event trigger dates, and event-log timestamps only with UTC. This prevents
  a non-UTC host from treating a scheduled campaign as due by its local clock
  and launching native Mautic processing early.
- Fixed: persisted dual-clock campaign SQL templates migrate to the UTC-only
  defaults, including per-campaign trigger guards.

## 0.9.259 - 2026-07-23
- Fixed: MCC profile guard now treats local automatic config migrations of
  legacy SQL/runtime defaults as non-drift. This prevents a stale desired
  config from causing repeated recoveries and daemon restarts when the selected
  profile is already correct.

## 0.9.258 - 2026-07-23
- Added: MCC hardware profiles now provide a host-wide scheduler concurrency
  budget derived from vCPU and RAM, while explicit per-host runtime overrides
  remain authoritative.
- Fixed: scheduler task limits are enforced across all Mautic instances on a
  host instead of being multiplied by the number of instance roots.
- Changed: multi-instance dispatch rotates the first instance on every tick so
  available host slots are distributed fairly instead of consistently
  favoring the first inventory entries.

## 0.9.257 - 2026-07-23
- Fixed: command execution now ignores an instance `.mcd/php` wrapper when it
  is missing or is not safely executable by the managed instance user, and
  falls back to the configured PHP binary instead of failing every Mautic
  scheduler command.
- Fixed: `instance-runtime apply` restores generated PHP CLI wrappers to
  `0755` even when their contents are already current, repairing permission
  drift without rewriting the wrapper or creating an unnecessary snapshot.

## 0.9.256 - 2026-07-22
- Fixed: a `midi` host carrying the exact legacy single-ring runtime snapshot
  now restores the canonical dual-ring `3+1` segment and campaign topology.
  This prevents stale generated overrides from disabling priority segments and
  per-instance whitelists after a profile change.

## 0.9.255 - 2026-07-22
- Fixed: per-instance segment whitelists saved by MCC with a host-qualified
  instance UID are now resolved by the local scheduler and placed in the
  priority segment ring.
- Fixed: dual-ring segment dispatch now fills the configured priority and
  regular concurrency lanes (`3+1` on `midi`) instead of launching only one
  segment and starving the regular ring.
- Fixed: segment due detection now treats newly created or modified contacts
  as candidates for every published segment, including contacts that were not
  already members at the previous build.

## 0.9.254 - 2026-07-21
- Fixed: cluster physical full and incremental backups can use direct SSHFS
  StorageBox storage instead of checking or consuming the replica's local
  `/mnt/data` backup filesystem.
- Added: `backup.cluster.storage_mode=sshfs` keeps the StorageBox mounted only
  for the backup operation and releases it after completion.
- Added: replica-operated clusters can disable redundant local cluster
  xtrabackup jobs and run the nightly live-replica mydumper directly to
  StorageBox over SSHFS.

## 0.9.253 - 2026-07-21
- Safety: every self-update path, including explicit CLI apply and version-mismatch restarts, now defers while a backup lock is active instead of restarting MCD during a cluster dump.
- Safety: active offsite backup status reports live process state without walking the remote sshfs tree, avoiding a competing/blocking probe while mydumper is writing.
- Expanded cluster incident regression coverage for every passive user-task command, manual update during active backup, and active-offsite status probing.

## 0.9.252 - 2026-07-21
- Safety: backup cleanup now distinguishes an idle orphaned temporary mysqld from an active long-running mydumper client and removes only the orphan after a grace period.
- Safety: cluster backup status no longer remains `running` after its lock and processes are gone; stale interrupted runs are recorded as failed for the next supervised retry.
- Added regression coverage for idle orphan cleanup and active dump preservation.

## 0.9.251 - 2026-07-21

- Safety: passive cluster agents now reject MCD manual segment, campaign, and
  import dispatch instead of executing user tasks while legacy cron remains
  external and unchanged.
- Safety: direct `mcd-cli exec` user-task commands are rejected in passive
  profile, preventing a second MCD workload queue from racing the cluster's
  fallback cron.
- Added regression coverage for passive manual-dispatch rejection.

## 0.9.250 - 2026-07-20

- Added: dedicated hardware profiles now install and maintain the canonical
  five-level SMART/NVMe/md-RAID Zabbix monitor, including its packages,
  UserParameter, restricted sudo policy, and health script.

## 0.9.249 - 2026-07-18

- Fixed: idempotent instance-runtime reconciliation no longer snapshots every
  unchanged CLI wrapper and nginx vhost on each plan refresh. This prevents
  no-op runtime checks from exhausting host inodes.
- Fixed: instance-runtime reconciliation prunes bounded batches of old,
  timestamped rollback snapshots while retaining the newest 20 and leaving
  manual or nonconforming entries untouched.

## 0.9.248 - 2026-07-18

- Keep managed database-index sessions attached for long online DDL operations instead of timing out after 30 seconds while MariaDB continues the ALTER in the background.

## 0.9.247 - 2026-07-18

- Fixed: orphan page-hits cleanup now holds a cross-process MariaDB advisory
  lock for the complete preview/delete cycle. A restarted daemon cannot launch
  another cleanup while an older SQL operation is still active.
- Fixed: the global managed-index profile now creates
  `idx_mcd_ph_lead_date (lead_id, date_hit, id)` online. Orphan page-hits
  batches no longer require a full table sort when the host lacked this index.

## 0.9.246 - 2026-07-18

- Changed: cluster offsite `mydumper` now uses the completed local physical
  full directly through a temporary read-only MySQL process. MCD no longer
  creates a second local `offsite-mysql` physical staging copy, preventing
  backup jobs from consuming another full-database worth of disk space.

## 0.9.245 - 2026-07-18

- Safety: cluster physical full backups now estimate the database size and
  refuse to start unless the local target has enough free space to finish.
  The decision is recorded in backup state instead of producing another
  incomplete full after hours of writes.

## 0.9.244 - 2026-07-17

- Fixed: native Mautic forms embedded on external domains can receive their
  hidden-iframe submission response. The nginx baseline now omits
  `X-Frame-Options` only for `/form/submit`, while retaining `SAMEORIGIN` on
  every other route.

## 0.9.243 - 2026-07-17

- Fixed: the Mautic 7 HTML-email builder now receives `licenseKey: 'GPL'` at
  plugin initialization, matching the already-correct page and MJML builders.
- Fixed: the patched GrapesJS JavaScript uses a scoped cache-busting URL so
  browsers with the old builder cached immediately load the corrected asset.

## 0.9.242 - 2026-07-17

- Fixed: Mautic 7 GrapesJS rich-text editing now supplies CKEditor's `GPL`
  license fallback when the plugin bridge omits a license key. MCD patches both
  plugin source and the published `builder.js` artifact idempotently, preserving
  backups and refusing unexpected signatures.

## 0.9.241 - 2026-07-13

- Fixed: campaign scheduled-event due detection now accepts both UTC and the
  instance-local Mautic clock before launching native campaign processing.
  This prevents non-UTC installations from delaying scheduled event-log work
  by the timezone offset while leaving final execution-window enforcement to
  native Mautic.
- Fixed: the per-campaign due guard and decision `no` interval catch-up use the
  same dual-clock semantics, so scheduler planning and launch revalidation
  cannot disagree about locally stored campaign timestamps.

## 0.9.240 - 2026-07-10

- Fixed: campaign trigger due detection now includes decision `no` branches
  whose child action is already due but whose event-log rows have not yet been
  created by native Mautic. This prevents MCD from marking campaigns stale and
  skipping `mautic:campaigns:trigger -i <id>` for scheduled follow-up emails
  such as Apetit campaign 161.

## 0.9.237 - 2026-07-09

- Safety: retention deletion on storage mounts now enforces a hard boundary check
  and refuses to remove any candidate path that resolves outside the current
  cluster remote retention root.

## 0.9.236 - 2026-07-09

- Fixed: cluster offsite retention now treats date-prefixed `.superseded-*` wrappers
  as normal date-scoped candidates for pruning (unless explicitly marked as
  manually protected), preventing temporary retention artifacts from accumulating
  indefinitely when long-term local policy is tightened.

## 0.9.235 - 2026-07-09

- Fixed: `mautic-upgrade apply` now uses the full maintenance guard for every
  upgrade path. Before Mautic update commands run, MCD pauses the scheduler,
  stops cron, terminates tracked/external Mautic console work including orphan
  console processes with a grace window, and aborts the upgrade if any running
  task cannot be stopped. Cleanup still restores only the cron/pause state that
  the upgrade guard owned.

## 0.9.234 - 2026-07-08

- Fixed: `composer-move` now keeps ZIP vhost backups outside
  `/etc/nginx/sites-enabled`. When the active vhost is a legacy regular file
  in `sites-enabled`, MCD writes the switched Composer vhost to
  `sites-available`, replaces the enabled file with a symlink, and preserves
  old configs as inactive backups only.
- Fixed: daemon bootstrap and self-update dependency installation now run
  `ensurepip` when the agent venv lost `pip` after a system Python minor
  upgrade, allowing MCD to reinstall requirements instead of crashing on
  missing modules such as `cryptography`.

## 0.9.233 - 2026-07-08

- Fixed: local Mautic DB connections now try the unix socket before TCP
  `localhost` when the instance config uses an implicit local database,
  avoiding repeated MariaDB `Access denied` noise on hosts that only grant
  `user@localhost`.
- Fixed: instance runtime normalization now rewrites legacy generic nginx
  `fastcgi_pass unix:/run/php/php-fpm.sock` entries to the active versioned
  PHP-FPM socket for the instance, preventing reboot/package updates from
  leaving vhosts pointed at a missing generic socket.

## 0.9.232 - 2026-07-07

- Added: `mcd-cli report:email-activity` collects Mautic email activity
  metrics locally from the instance database for MCC Reports, including daily
  summary rows, per-email extended rows, clicks, failures, reads, unsubscribes,
  and optional fresh/old contact filtering.

## 0.9.231 - 2026-07-07

- Fixed: service-profile APT upgrades now run the same post-upgrade service
  recovery used by `mcd-cli apt-upgrade`. After a successful
  `apt-get upgrade`/`dist-upgrade`, MCD verifies and starts `nginx`,
  `mysql`/`mariadb`, `cron`, and detected `php-fpm` services, then validates
  nginx config. This prevents host package updates from leaving migrated or
  active instances offline after MariaDB/nginx package upgrades.

## 0.9.230 - 2026-07-07

- Fixed: nginx baseline default-deny generation now skips managed listen blocks
  for ports already owned by a legacy default vhost, avoiding harmless but noisy
  duplicate `server_name _` warnings while still preserving nginx syntax.

## 0.9.229 - 2026-07-07

- Fixed: nginx baseline default-deny generation now avoids claiming
  `default_server` on ports already owned by an active legacy default vhost,
  preventing package-update postchecks from rolling back with duplicate
  default server errors after vhost normalization.

## 0.9.228 - 2026-07-07

- Released: unified agent build from one source tree containing the MySQL 8
  generated-column migration import compatibility fix and the nginx baseline
  duplicate `autoindex` normalization fix.

## 0.9.227 - 2026-07-07

- Fixed: instance migration database imports now rewrite MySQL 8 generated
  `date_format()` bucket columns to MariaDB-compatible deterministic
  expressions while streaming the gzipped dump into the target. This prevents
  MySQL 8 -> MariaDB migrations from failing on Mautic campaign lead generated
  date columns such as `generated_date_added_hour`.

## 0.9.226 - 2026-07-07

- Fixed: nginx baseline normalization now removes legacy server-level
  `autoindex` directives when the managed hardening snippet is included in the
  same vhost, preventing package-update postchecks from rolling back with nginx
  duplicate directive errors after IPv6 `listen [::]` cleanup.

## 0.9.225 - 2026-07-07

- Fixed: Composer Mautic 6 to 7 upgrades now print Composer, Node.js, and npm
  preflight versions, normalize the Mautic 7 root `composer/installers`
  constraint to `^2.0`, and run `composer update --with-all-dependencies
  --dry-run` before the live dependency update. This catches package graph
  conflicts such as `etailors/mautic-amazon-ses` requiring Composer installers
  v2 before vendor code is changed.

## 0.9.224 - 2026-07-07

- Fixed: Composer Mautic 6 to 7 upgrades now normalize single-node Redis cache
  DSNs that point at `127.0.0.1` or `localhost` before the first Mautic 7
  `cache:clear`. This avoids the Mautic 7/Predis 3 loopback resolution bug
  where a scalar local Redis DSN is converted into an aggregate endpoint array
  and cache clear fails after Composer has already installed Mautic 7.

## 0.9.223 - 2026-07-07

- Fixed: successful instance migrations now restore source-host MCD
  maintenance mode and cron before reporting completion, preventing migrated
  source hosts from being left with scheduler paused after a clean run.

## 0.9.222 - 2026-07-07

- Fixed: Composer Mautic 6 to 7 upgrades now apply the Mautic 7 Twig include
  compatibility workaround before final cache verification, preventing login
  pages from failing with `Twig\Markup` return type errors after upgrade.
- Fixed: post-upgrade HTTP verification now falls back to the instance
  `site_url` from `local.php` when discovery did not attach a vhost domain.

## 0.9.221 - 2026-07-07

- Fixed: PHP-FPM socket fallback rendering no longer emits a duplicate
  semicolon in generated nginx `fastcgi_pass` directives.

## 0.9.220 - 2026-07-07

- Fixed: generated Mautic image nginx vhosts now fall back to the system
  `/run/php/php-fpm.sock` alternative when the cached target PHP-FPM version is
  stale or the exact versioned socket is not present on the host.

## 0.9.219 - 2026-07-07

- Fixed: MCD-managed package installation now runs `apt-get update` with
  `--allow-releaseinfo-change`, so routine repository metadata label changes
  do not block certbot DNS plugin installation, backup tooling preparation,
  Wazuh profile application, Mautic upgrades, or host package updates.

## 0.9.218 - 2026-07-07

- Fixed: Mautic image deploy can now issue certificates through Cloudflare
  DNS-01 when MCC deploys a domain with a managed Cloudflare DNS credential.
  This avoids Let’s Encrypt HTTP-01 timeouts on hosts whose Hetzner firewall
  only permits web traffic from Cloudflare.
- Fixed: generated image-deploy nginx vhosts now serve
  `/.well-known/acme-challenge/` as static files before the Mautic front
  controller for non-Cloudflare HTTP-01 hosts.

## 0.9.217 - 2026-07-06

- Fixed: `tiny` profile now enables periodic full-scan segment planning with
  the existing 60 second interval. Small Mautic 7 instances now pick up newly
  matching contacts that are not yet present in `lead_lists_leads`, matching
  native `mautic:segments:update` behavior instead of waiting for a manual
  segment update or the 24 hour stale-window fallback.

## 0.9.216 - 2026-07-05

- Fixed: cluster offsite backup cleanup now treats temporary prepared MySQL
  processes as stale when they outlive the configured backup dump timeout, even
  if their prepared datadir still exists. This prevents abandoned
  `mcd-offsite-mysql-*` processes from pinning reflinked xtrabackup data for
  days and blocking future AnanasRS-style cluster offsite runs.
- Fixed: cluster backup status now recovers a completed offsite backup from the
  remote daily marker when an older agent finalized the directory but failed to
  update local state. Recovery also corrects stale file archive paths left under
  the temporary `.incomplete-*` directory name.

## 0.9.215 - 2026-07-04

- Fixed: host and instance backup file archives now exclude MCD runtime
  `.mcd` directories. Mutable runtime files such as cached version metadata no
  longer make `tar` abort a valid backup with "file changed as we read it".

## 0.9.214 - 2026-07-04

- Fixed: campaign trigger due detection now follows native Mautic scheduled
  event-log semantics by selecting rows with `is_scheduled = 1` and due
  `trigger_date` even when `date_triggered` is already filled by an earlier
  scheduling/attempt pass. This keeps partially processed campaigns with due
  event-log work visible to the MCD trigger ring instead of marking them stale.
- Fixed: the per-campaign trigger due guard now uses the same scheduled-row
  rule, so a campaign selected by the trigger ring is not skipped merely
  because `date_triggered` is later than the original due timestamp.
- Added: regression coverage for scheduled due campaign event-log rows whose
  attempt timestamp is already populated.

## 0.9.213 - 2026-07-03

- Added: guarded Composer Mautic 6 to 7 upgrade support behind explicit
  `--allow-major`, including Composer/Node preflight, dynamic Mautic 7 target
  application, existing Doctrine migration metadata reconciliation, and a
  host-level PHP 8.4 system upgrade guard that blocks while other discovered
  host instances are below Mautic 7.
- Added: PHP 8.4 system upgrade handling for the 6 to 7 flow now migrates
  custom PHP ini files, rewrites nginx PHP-FPM references, validates nginx,
  restarts services, and purges PHP 8.3 only after the 8.4 switch succeeds.

## 0.9.212 - 2026-07-03

- Fixed: per-campaign trigger due guards now apply the same `publish_down`
  active-window check as the scheduler ring SQL, so expired campaigns cannot be
  treated as due after the ring planner has filtered them out.

## 0.9.211 - 2026-07-03

- Fixed: campaign trigger and rebuild due SQL now applies the active
  `publish_down` window to every scheduler branch. Expired published campaigns
  no longer stay in MCD trigger/rebuild rings after native Mautic would skip
  them, preventing old campaigns from delaying fresh active campaigns.
- Fixed: saved legacy campaign trigger/rebuild SQL overrides missing the
  branch-level `publish_down` guard are migrated back to the packaged default.

## 0.9.210 - 2026-07-03

- Fixed: host pressure scheduler pause no longer treats historically occupied
  swap as critical by itself. Swap reaches the pause level only when high swap
  usage is accompanied by low available memory, so active campaign/segment
  scheduling is not stalled on large hosts that have reclaimed RAM but still
  show old swap usage.

## 0.9.209 - 2026-07-03

- Fixed: MCC/manual `mcd-cli exec` operations now run the requested Mautic
  console command synchronously on the selected instance instead of marking the
  operation successful after only queueing a local scheduler request. This makes
  UI buttons behave like the native Mautic console command the operator chose.
- Fixed: manual campaign aliases `campaign:rebuild` and `campaign:trigger` now
  execute native plural Mautic commands (`mautic:campaigns:rebuild` and
  `mautic:campaigns:trigger`). The explicit `campaigns:rebuild` alias is also
  accepted.

## 0.9.208 - 2026-07-01

- Fixed: full MCC state pushes now use a longer default HTTP timeout so image
  installs and other inventory-changing operations do not leave MCC stale when
  the control plane takes more than five seconds to process a larger host
  payload. Lightweight monitor signals keep their short timeout.

## 0.9.207 - 2026-07-01

- Fixed: Mautic image installs now ignore archived `.mcd` runtime artifacts
  such as `.mcd/php` symlinks that point at the source host's generated MCD
  wrapper path. Other unsafe archive paths and links are still rejected.

## 0.9.206 - 2026-07-01

- Changed: successful native page_hits segment rebuilds are now considered
  long enough for SQL auto-promotion after 600 seconds instead of 1800 seconds.
  This keeps medium-long page-hit segments out of daytime native Mautic rebuild
  loops before they can accumulate database load.

## 0.9.205 - 2026-07-01

- Changed: page_hits-based SQL segment rebuilds now default to quiet-window-only
  execution. Auto-promoted page-hit segments still stay out of native Mautic
  classic rings during the day, but the direct SQL scan waits for the configured
  quiet window unless an operator explicitly disables the guard.

## 0.9.204 - 2026-07-01

- Fixed: SQL auto-promotion now combines AND-ed page-hit behavior filters on a
  single `{prefix}page_hits` alias instead of joining multiple page-hit
  subqueries by lead ID. This matches the native Mautic shape for URL plus
  hit-date segments and avoids expensive page_hits self-joins.
- Fixed: SQL-managed segments are now pruned from the native segment resume
  ring as well as the normal priority and regular rings, preventing a killed or
  timed-out native rebuild from re-entering the classic scheduler cycle after
  auto-promotion.

## 0.9.203 - 2026-07-01

- Fixed: SQL segment topological planning now preserves the incoming priority
  order among ready nodes. High-priority long SQL segments are no longer
  reordered behind smaller numeric segment IDs when there is no dependency
  reason to do so.

## 0.9.202 - 2026-07-01

- Fixed: the long SQL segment ring now rebuilds its in-memory deque from the
  priority plan each planner tick. Existing slow-only backlog order no longer
  prevents higher-priority auto-promoted problem segments from running first.

## 0.9.201 - 2026-07-01

- Changed: auto-promoted long SQL segment ring ordering now prioritizes
  segments with recent scheduler problems before purely slow historical
  rebuilds. This puts failing business-critical page-hit segments ahead of the
  long-only backlog.

## 0.9.200 - 2026-07-01

- Fixed: SQL segment slot accounting now also checks the live MySQL
  processlist for active `mcd_tmp_segment_leads` rebuild queries. If a
  client-side timeout or daemon restart leaves the server-side direct SQL
  rebuild running, MCD blocks additional SQL segment workers until that query
  is gone.

## 0.9.199 - 2026-07-01

- Fixed: long SQL segment slot accounting now treats fresh persisted
  `segment_sql_state` locks as active work after daemon restart or self-update.
  A restarted daemon no longer starts another heavy long SQL rebuild while a
  previous worker's SQL query is still represented by a fresh running lock.

## 0.9.198 - 2026-07-01

- Fixed: the dedicated long SQL segment ring now runs direct SQL rebuilds in a
  background worker. Heavy page-hit rebuilds no longer block the daemon's main
  scheduler loop, while still claiming the shared segment slot and blocking the
  same segment from native/classic launch until the SQL worker finishes.

## 0.9.197 - 2026-07-01

- Fixed: long native segment rebuild auto-promotion now considers segments from
  recent task history even when the normal due-segment query does not list
  them. This lets page-hit-driven segments stay in the dedicated SQL ring on
  their own repeat interval instead of requiring a temporary whitelist.

## 0.9.196 - 2026-07-01

- Added: SQL-safe page-hit segments that finish a native
  `mautic:segments:update -i <id>` but take longer than
  `segment_sql_auto_long_native_min_duration_sec` are now auto-promoted into a
  dedicated long SQL ring instead of requiring a manual whitelist.
- Added: the long SQL ring runs before the regular SQL ring and is controlled
  by `segment_sql_long_ring_max_per_tick`.
- Fixed: automatic SQL segment detection now understands Mautic behavior
  filters on `hit_url_date`, allowing URL contains plus page-hit date windows
  to match native Mautic segment semantics.
- Changed: page-hit filters alone are no longer enough to auto-promote a
  segment; MCD now requires repeated scheduler problems, checked-out state, or
  a recorded slow native rebuild.

## 0.9.195 - 2026-06-30

- Changed: republished the default-deny nginx baseline and active-profile cron
  wrapper reconciliation fixes under a fresh build number so self-update can
  converge hosts that already report `0.9.194`.

## 0.9.194 - 2026-06-30

- Fixed: active profile cron reconciliation now disables small wrapper scripts
  that invoke MCD-managed Mautic console jobs, such as legacy full
  `mautic:segments:update` shell wrappers.

## 0.9.193 - 2026-06-30

- Fixed: nginx baseline now installs a managed default deny vhost for port 80
  and 443 so stale DNS records or unknown Host headers cannot fall through to
  the first live Mautic vhost and expose another instance's login page.

## 0.9.192 - 2026-06-30

- Fixed: `mcd-cli instance-delete --delete-db` now falls back to the local MCD
  instance inventory for database credentials when `local.php` is missing or
  incomplete, allowing partial deletes to finish instead of failing with
  "database name is unavailable".
- Fixed: recovery deletes with an explicit `--db-name` can now drop the local
  database through the root MariaDB socket even when the old instance no longer
  has a readable DB user in `local.php` or inventory.
- Fixed: `mcd-cli instance-delete --delete-files` now quarantines the selected
  root path before recursive removal and repeats the delete if the original
  root is recreated during the removal window, making stale instance folders
  disappear reliably after a successful pre-delete backup.

## 0.9.191 - 2026-06-30

- Fixed: the absolute-root delete recovery path is now wired to the
  `instance-delete` command itself. Manual Mautic command shortcuts continue to
  require a discovered inventory match.

## 0.9.190 - 2026-06-30

- Fixed: `mcd-cli instance-delete` can now finish a partially completed delete
  when the instance inventory row is already gone but MCC passes the absolute
  `/var/www/...` root. Other operations still require a discovered inventory
  match.

## 0.9.189 - 2026-06-30

- Fixed: `mcd-cli instance-delete --delete-files` now removes the selected
  instance root with retries and permission repair. Delete operations no longer
  fail with `[Errno 39] Directory not empty` when the agent scanner or runtime
  metadata recreates `.mcd` entries during the removal window, and failures now
  report the remaining entries for diagnosis.

## 0.9.188 - 2026-06-30

- Fixed: one-shot instance backups written under a `deleted-instances`
  storage namespace are now manual-delete-only. `backup instance-run` records
  `retention_skipped_reason=deleted_instances_manual_delete_only` for those
  pre-delete backups and no longer prunes older deleted-instance backups by the
  normal retention copy count. Regular host and instance backups keep their
  existing retention behavior.

## 0.9.187 - 2026-06-30

- Fixed: `backup profile-set` now accepts backup-level `enabled`, `method`,
  and `retention_copies` fields and applies them to the effective runtime
  config before readiness validation. MCC-triggered operation backups can now
  enable backup on hosts where `[backup].enabled=false` instead of failing
  profile verification before a required pre-delete backup starts.

## 0.9.186 - 2026-06-30

- Added: APT service profiles can now enforce the nginx Cloudflare real-IP
  baseline from host edge-mode intent. Cloudflare-proxied hosts write the
  managed `/etc/nginx/conf.d/10-mcd-cloudflare-real-ip.conf` template with
  `CF-Connecting-IP`, Cloudflare CIDRs, `nginx -t` validation, rollback, and
  reload; direct-origin hosts remove only that MCD-managed trust file.
- Added: Mautic install readiness and APT state now report Cloudflare real-IP
  drift so MCC Check/Fix Host can surface and repair missing client-IP
  forwarding instead of hiding it behind generic nginx package checks.
- Changed: nginx baseline now requires `/etc/nginx/conf.d/*.conf` to be
  included from `nginx.conf`, keeping managed host-level nginx templates active
  alongside the existing `sites-enabled` layout.

## 0.9.185 - 2026-06-30

- Added: MCD now audits and safely repairs Mautic `emails.sent_count` drift
  after completed campaign trigger runs. This reconciles the denormalized UI
  counter upward to the actual `email_stats` count only when the related email
  has no pending campaign event-log work, covering large sends where Mautic
  wrote send stats but crashed before updating the cached email counter.
- Added: `mcd-cli email-counters audit|repair --campaign-id <id>` for
  controlled operator verification and repair without raw manual SQL.

## 0.9.184 - 2026-06-30

- Guarded: PHP service profile now writes a dedicated CLI opcache drop-in
  instead of reusing the FPM drop-in content. CLI tuning remains limited to
  opcache and realpath cache values, so host defaults such as
  `memory_limit=-1`, timezone, upload limits, timeouts, and session settings
  stay controlled by the normal PHP CLI config.

## 0.9.183 - 2026-06-30

- Fixed: per-instance PHP CLI wrappers now run with `memory_limit=-1` instead
  of forcing `512M`. Large Mautic campaign trigger/rebuild runs can otherwise
  OOM inside AWS/Guzzle/Doctrine, get retried by MCD in chunks, and stretch a
  daytime newsletter into the night.

## 0.9.182 - 2026-06-30

- Fixed: new nginx vhosts created by Mautic image install and instance
  migration are IPv4-only and no longer write `listen [::]` directives.
- Changed: those generated nginx vhosts now come from package template files
  under `mcd_agent/templates/nginx/`, so future vhost text changes can be made
  by replacing templates rather than editing generator logic.
- Fixed: nginx baseline normalization removes IPv6 `listen` directives from
  active vhosts when the host reports IPv6 disabled through MCD environment
  state.

## 0.9.181 - 2026-06-29

- Changed: instance runtime now rolls nginx vhosts back to the shared default
  PHP-FPM socket instead of generating and selecting per-instance PHP-FPM
  pools. The Mautic campaign date/time UI timezone issue is handled by the
  Mautic Locale Fix plugin, so per-instance FPM pools are no longer needed.
- Cleanup: instance runtime removes managed `mcd-*` pool files plus the
  managed `pool.d/99-mcd.conf` include and `pool.d/mcd` symlink once no active
  nginx vhost still references an MCD per-instance socket. The per-instance
  `.mcd/php` CLI wrapper remains for stable console execution.

## 0.9.180 - 2026-06-28

- Fixed: regular host backups no longer inherit the pre-delete
  `mcc/deleted-instances` remote root. Storage-only profile updates repair that
  polluted root back to `backup`, and host backup run/preflight/prune/restore
  defensively avoid using the deleted-instances namespace.
- Added: host-level mydumper backups now publish per-instance sidecar
  `mcc-backup-manifest.json` files and storage index entries. A single
  multi-instance backup can therefore appear as separate MCC restore cards
  without duplicating the stored backup payload.
- Added: `mcd-cli backup instance-run --remote-root-dir <path>` for one-shot
  pre-delete backup placement without persisting that path into the host's
  normal backup profile.
- Fixed: backup retention removes per-instance sidecar manifests/index entries
  when the parent host backup date is pruned.

## 0.9.179 - 2026-06-28

- Fixed: per-instance runtime now detects legacy nginx PHP-FPM sockets written
  as `/var/run/php/php<version>-fpm.sock` as well as `/run/php/...`. This covers
  older single-instance vhosts such as AnanasMK and Benu and lets them receive
  the per-instance FPM pool and CLI wrapper.

## 0.9.178 - 2026-06-28

- Added: per-instance PHP runtime materialization. MCD now derives the
  instance timezone from Mautic `local.php`, generates per-instance PHP-FPM
  pools under `/opt/mcd/generated/php/<version>/fpm/pools`, includes them via
  `/etc/php/<version>/fpm/pool.d/99-mcd.conf` and the `pool.d/mcd` symlink
  directory, rewrites matching nginx vhosts to the instance socket, and writes
  a matching CLI wrapper under `/opt/mcd/generated/instances/<slug>/php`.
- Safety: instance runtime apply validates `php-fpm -t` and `nginx -t` before
  reload and restores snapshots if validation fails.
- Fixed: repairing only the generated FPM include/symlink now counts as a
  runtime change and reloads PHP-FPM/nginx, so daemon self-repair cannot leave
  a restored include unapplied.
- Added: `mcd-cli instance-runtime status|apply` for operator inspection and
  controlled convergence of per-instance PHP runtime.

## 0.9.176 - 2026-06-25

- Fixed: Composer migration now marks the old ZIP root as retired after a
  successful vhost switch, and discovery skips retired roots. This prevents the
  MCC cache from keeping the old ZIP install as the active instance after the
  web server has moved to the Composer docroot.

## 0.9.175 - 2026-06-25

- Fixed: Composer migration now rewrites nginx `fastcgi_pass` to the selected
  PHP-FPM socket when switching the vhost to the Composer `docroot`. This avoids
  leaving Composer Mautic 6/7 installs behind an older PHP-FPM service after a
  successful move.

## 0.9.174 - 2026-06-23

- Changed: removed the Viber-specific schema repair from the generic plugin
  installer. Viber schema repair now belongs to the SalesSnapViberBundle
  package itself, so future plugin fixes ship with plugin versions instead of
  agent-specific hardcoding.

## 0.9.173 - 2026-06-23

- Fixed: Viber plugin install/update now runs a narrow schema repair after
  Mautic plugin reload. MCD creates the missing `viber_message_alerts` table
  and adds the missing `viber_message_stats` foreign keys/indexes
  idempotently, while clearing orphan references before constraint creation.
  This avoids unsafe full `doctrine:schema:update --force` runs on production
  instances.

## 0.9.172 - 2026-06-23

- Fixed: backup profile verification release aligned with MCC storage-asset auth
  preservation. Agents still reject unverified profiles before persistence, so
  MCC storage asset bugs cannot leave a host with a newly saved but unusable
  backup profile.

## 0.9.171 - 2026-06-23

- Fixed: `backup profile-set` now prepares and verifies backup readiness before
  persisting the profile. The agent validates storage auth, installs required
  backup tools when auto-install is enabled, verifies Mautic DB connectivity,
  mounts the configured storage, and performs a small write/delete probe. Failed
  checks leave the previous backup profile untouched.
- Added: `backup profile-set --skip-prepare-check` remains available for
  emergency operator recovery when a profile must be stored without live
  verification.

## 0.9.170 - 2026-06-23

- Safety: service-profile daemon runs are now advisory-only dry runs. MCD no
  longer rewrites MySQL, PHP-FPM, sysctl, APT, Wazuh, or DB index state from the
  automatic loop; real mutations require an explicit `mcd-cli service-profile
  apply` or `rescan` operator command.
- Fixed: Galera/PXC MySQL service-profile sanitization now preserves operational
  connection headroom by flooring cluster `max_connections` at 2000,
  `thread_cache_size` at 256, and `open_files_limit` at 262144 instead of
  rewriting large clusters back to the unsafe 600-connection profile.
- Fixed: Mautic DB index maintenance now imports `pymysql.err` explicitly so
  duplicate-index race handling keeps working with current PyMySQL builds.

## 0.9.169 - 2026-06-23

- Added: APT service profiles can now install and configure `zabbix-agent2`
  from the official Zabbix repository, write a managed agent2 drop-in with MCC
  server/active server/hostname settings, allow the Zabbix server through the
  host firewall, and report concrete Zabbix agent readiness back to MCC.
- Fixed: explicit APT service-profile applies now retry the Zabbix/MySQL
  monitor bootstrap path instead of remaining stuck behind an old failed
  one-shot marker.

## 0.9.168 - 2026-06-22

- Fixed: instance migration and ZIP-to-Composer migration nginx vhosts now keep
  Mautic's public `/app/assets/` and `/app/bundles/*/Assets/` paths reachable
  before private `/app` deny rules. This prevents migrated Composer installs
  from rendering missing admin icons/fonts while keeping application internals
  blocked.
- Fixed: nginx baseline normalization now repairs the same public asset rules
  on already-migrated vhosts and modernizes deprecated `listen ... http2`
  syntax to `http2 on;` on nginx versions that support it.

## 0.9.167 - 2026-06-22

- Fixed: nginx baseline now creates the Debian-compatible
  `/etc/nginx/snippets/fastcgi-php.conf` snippet on official nginx.org hosts.
  Fresh host preparation, Check/Fix, image installs and instance migrations no
  longer fail final nginx validation when the package lacks Debian's
  `snippets/fastcgi-php.conf` file.

## 0.9.166 - 2026-06-22

- Added: instance migration target preflight now supports explicit destructive
  cleanup flags for retrying failed migrations: `--wipe-target` removes the
  chosen target root after safety checks, and `--wipe-target-db` drops the
  chosen target database. Without these flags, existing target artifacts still
  block migration startup.

## 0.9.165 - 2026-06-22

- Fixed: nginx baseline now creates the Debian-style
  `/etc/nginx/sites-available` and `/etc/nginx/sites-enabled` directories when
  the official nginx.org package does not provide them. Host preparation and
  `Check / Fix` APT profile application now converge this layout before
  Mautic vhosts are written.
- Fixed: instance migration target finalization now verifies and creates the
  nginx sites layout before writing the migrated vhost, preventing Composer
  migrations to freshly prepared official-nginx hosts from failing at
  `target web config`.

## 0.9.164 - 2026-06-22

- Added: APT service profiles can now prepare migration-ready hosts by
  installing Node.js 20/npm from NodeSource, installing the verified official
  Composer binary at `/usr/local/bin/composer`, and creating `/var/www` with
  `www-data:www-data` ownership and `0755` mode when requested by MCC.
- Added: install readiness now reports Composer/Node/npm command paths and
  `/var/www` state so MCC `Prepare` / `Check / Fix` can show concrete missing
  prerequisites instead of hiding them under a generic APT profile check.

## 0.9.163 - 2026-06-22

- Fixed: APT service profiles now ensure a local FQDN mapping in `/etc/hosts`
  before package installation, preventing sendmail post-install scripts from
  sleeping on bare hosts with unqualified hostnames.
- Fixed: bare-host package preparation now enables and starts core services
  installed by the package profile, including nginx, Redis, PHP-FPM, MariaDB,
  MySQL/Percona, and sendmail.

## 0.9.162 - 2026-06-22

- Added: APT service profiles can now prepare bare hosts with explicit database
  repository targets for Percona Server 8.4, Percona XtraDB Cluster 8.4,
  MySQL 8.4, MariaDB 11.4, and legacy Percona 8.0. Percona 8.4 setup uses the
  LTS channels with HTTPS repositories so MCC host preparation does not depend
  on existing database package auto-detection.

## 0.9.161 - 2026-06-21

- Fixed: per-instance storage backups now enforce retention by copy count after
  a successful run. The retention prune now recognises instance timestamp
  directories such as `20260621-010203`, protects the current backup directory,
  and removes matching MCC backup index entries for pruned directories.

## 0.9.160 - 2026-06-21

- Fixed: `composer-move` now forces all Composer layout path keys in target
  `config/local.php`, including `cache_path`, `log_path`, `tmp_path`,
  `contact_export_dir`, upload, form upload, and report temp paths. Mautic 6
  ZIP instances whose source config omitted some of those keys no longer fall
  back to stale `app/../var` or `app/../media` defaults after the nginx switch.

## 0.9.159 - 2026-06-21

- Fixed: `composer-move` now recreates Composer runtime directories after cache
  cleanup, including `var/cache`, `var/logs`, `var/tmp`, and media upload
  folders under `docroot/media/files`, with writable permissions before nginx
  is switched. This prevents migrated instances from missing `var/logs` after
  the move.

## 0.9.158 - 2026-06-20

- Fixed: Composer plugin layout now matches live Mautic 7.1.2 installations
  such as `default7`, `grubinshowroom`, and `gmt`: custom plugins live under
  `docroot/plugins`, not root-level `plugins`. `composer-move` copies ZIP
  plugins to `docroot/plugins`, rewrites any plugin absolute paths to that
  target, and shared plugin discovery now prefers `docroot/plugins` for
  Composer roots while keeping root-level `plugins` for ZIP installs. Cluster
  asset checks also use Composer `docroot/app/bundles` before legacy
  `app/bundles`.

## 0.9.157 - 2026-06-20

- Fixed: `composer-move` now copies ZIP plugins into the Composer root
  `plugins/` directory instead of the public `docroot/plugins/` tree, and
  rewrites `local.php` mutable paths to their Composer layout targets before
  the generic root replacement. Media upload/report paths now point at
  `docroot/media`, preventing migrated instances from keeping broken
  `target/media` absolute paths.

## 0.9.156 - 2026-06-20

- Fixed: agent package sync now removes stale `mcd_agent-*.dist-info`
  metadata before reinstalling from `/opt/mcd/src`, so package metadata,
  console scripts and imported source report the same version after repair.

## 0.9.155 - 2026-06-20

- Fixed: manual `mcd-cli self-update apply` now also runs the package/source
  drift repair path when MCC reports the host is already up to date. This keeps
  manual repair behavior consistent with daemon auto-update checks.

## 0.9.154 - 2026-06-20

- Fixed: self-update now keeps the installed venv package in sync with the
  switched `/opt/mcd/src` source tree. Hosts that already had the new source but
  stale console scripts/site-packages now repair the package install and restart
  MCD instead of reporting a misleading up-to-date version.
- Added: agent version reporting now includes the installed package version and
  marks package/source drift as a version mismatch.

## 0.9.153 - 2026-06-20

- Added: `mcd-cli composer-move` converts a latest ZIP Mautic 6/7 instance to
  a Composer skeleton copied from MCC image assets. The command creates a new
  Composer root, copies mutable state from the ZIP source without deleting it,
  patches target-local paths, preserves the old nginx vhost as a prefixed
  backup, switches nginx to the Composer `docroot`, and rescans inventory after
  reload.
- Changed: image-install inventory imports are lazy so shared artifact helpers
  can be tested without loading host secret-store dependencies.

## 0.9.152 - 2026-06-20

- Fixed: `mautic_db_indexes` service-profile apply now treats MySQL duplicate
  key races as a successful noop only after refreshing `information_schema` and
  confirming that the expected index exists with the expected columns. This
  prevents false service-profile auto-apply failures when another apply run
  created the index first.
- Fixed: package metadata version is back in sync with the agent runtime
  version for the next self-update build.
- Fixed: legacy Mautic segment dependency expansion now ignores dependency
  filter references to segments that are not present in the published segment
  set, preventing deleted parent ids from being scheduled repeatedly.

## 0.9.151 - 2026-06-20

- Fixed: plugin catalog status now treats an installed semantic version newer
  than the published server version as current. Custom builds such as
  `1.0.36.1` no longer show as `UPDATE` against upstream `1.0.36`.

## 0.9.150 - 2026-06-20

- Added: state push now reports per-instance disk footprint metrics, including
  Mautic root disk usage, database size, selected subdirectory breakdowns, and
  per-instance measurement errors for MCC reporting.

## 0.9.149 - 2026-06-19

- Fixed: campaign rebuild dispatch now waits when the same campaign already has
  an active trigger run. This prevents MCD from overlapping
  `mautic:campaigns:rebuild -i ID` with `mautic:campaigns:trigger -i ID`,
  which can make large sends appear stuck behind Mautic's command lock.
- Added: regression coverage for same-campaign trigger/rebuild dispatch
  exclusion.

## 0.9.148 - 2026-06-19

- Fixed: campaign trigger scheduling now bootstraps published campaigns whose
  first/root event is a condition, not only campaigns whose first/root event is
  an action. This covers segment/field/device conditions that already have
  active `campaign_leads` but no `campaign_lead_event_log` rows yet; MCD now
  lets Mautic trigger the campaign so it can evaluate the condition and execute
  downstream channel actions such as email sends.
- Fixed: the per-campaign due guard now uses the same root action/condition
  bootstrap semantics as the scheduler due SQL, preventing audit-planned
  root-condition campaigns from being skipped as stale with
  `pending_logs=0 triggered_logs=0`.
- Added: regression coverage for root-condition campaign bootstrap SQL and the
  matching trigger due guard.

## 0.9.147 - 2026-06-18

- Changed: plugin competition is now driven by MCC repo metadata instead of
  Amazon-specific code paths in the agent. Each published plugin source can
  carry a dependency matrix that describes conditional coexistence and
  displacement rules.
- Added: plugin manifest consumption now evaluates dependency-matrix selection
  rules against the live instance context, so mainstream variants can coexist
  while enhanced variants can evict competitors when their matrix says so.
- Fixed: plugin DB conflict cleanup now uses the same dependency-matrix result
  as filesystem cleanup, keeping selected install aliases protected while
  removing the intended competitors.
- Added: regression coverage for dependency-matrix driven coexistence and
  displacement behavior.

## 0.9.146 - 2026-06-18

- Fixed: Amazon plugin conflict cleanup is now asymmetric. Native
  `AmazonSesBundle` no longer globally conflicts with
  `AmazonSnsCallbackBundle` / `MauticAmazonSesBundle`; only the callback
  bundles conflict with each other by default.
- Fixed: selecting the managed Amazon SES source `1.0.36.1` now always
  auto-removes callback competitors, and selecting a callback bundle now
  auto-removes `AmazonSesBundle` only when the installed runtime is the full
  managed `1.0.36.1` variant.
- Fixed: plugin DB conflict-row cleanup now follows the same computed
  auto-removal set as filesystem cleanup, preventing stale Amazon competitor
  rows from surviving a replacement.
- Added: regression coverage for Amazon SES managed-vs-upstream conflict
  handling and callback coexistence.

## 0.9.145 - 2026-06-15

- Added: Galera DB health now reports explicit routing/source eligibility.
  A node is eligible only when it is `Primary`, `Synced`, connected and
  `wsrep_ready=ON`; donor, joiner and non-Primary nodes are marked blocked
  even if MySQL or systemd still look alive.
- Changed: cluster DB health uses the same eligibility predicate for its
  overall `ok/degraded` status, preventing stale direct consumers from treating
  a recovering Galera node as safe.

## 0.9.144 - 2026-06-15

- Added: cluster replica DB health can now run configurable data-freshness
  checks against application tables. This catches stale async replicas whose
  `Seconds_Behind_Source` is `0` only because their source is itself stale or
  detached from the Primary Galera component.
- Added: replica DB health payload now includes source host, source server id,
  SQL state and compact IO/SQL errors so MCC can show why a replica is not
  reliable for reporting.
- Added: regression coverage for stale replica data detection and safe MySQL
  identifier handling in freshness checks.

## 0.9.143 - 2026-06-15

- Fixed: SQL-driven segment rebuilds now refresh Mautic's segment contact
  count cache after replacing `lead_lists_leads`. This keeps the Mautic segment
  list UI in sync with direct SQL rebuild results instead of showing stale
  `No Contacts` badges.
- Added: regression coverage that SQL-ring rebuilds call the Mautic count-cache
  refresh helper after successful membership replacement.

## 0.9.142 - 2026-06-14

- Fixed: import scheduler now fast-follows recently active Mautic imports.
  After an import batch exits, MCD immediately rechecks queued/delayed imports
  before allowing segment workers to take the shared slot, preserving import
  priority without increasing idle DB polling.
- Added: regression coverage for the fast-follow import polling guard.

## 0.9.141 - 2026-06-14

- Fixed: import scheduler now recovers factual orphaned Mautic background
  imports. If a row is left `IN_PROGRESS` but no live `mautic:import` CLI
  worker exists for the instance after a grace period, MCD requeues it as
  `DELAYED` so the next normal import batch can resume from the saved line.
- Safety: the recovery only touches background `IN_PROGRESS` imports with an
  existing source file and unfinished line counter; manual/browser imports are
  left alone.
- Added: regression coverage that import recovery is skipped while a CLI
  worker is alive and runs only when the worker is absent.

## 0.9.140 - 2026-06-14

- Fixed: import scheduling now follows Mautic 4/5/6/7 import status
  constants. MCD counts only queued/delayed imports as launchable and no
  longer treats `IN_PROGRESS` rows as a reason to start another
  `mautic:import` worker.
- Fixed: import pending SQL now mirrors Mautic's own completion rule by
  treating delayed imports as pending only while `properties.line < line_count`,
  avoiding an extra launch after the final batch.
- Fixed: legacy runtime SQL overrides for import pending checks are migrated
  away from unsafe `IN_PROGRESS` and inclusive final-line predicates.
- Added: regression coverage for Mautic import status mapping and import SQL
  migration.

## 0.9.139 - 2026-06-14

- Fixed: campaign trigger event-log due checks now follow Mautic's UTC
  `trigger_date` semantics and require scheduled event-log rows, preventing
  automatic triggers from starting hours early on instances with non-UTC UI
  timezones.
- Fixed: campaign trigger progress watchdog now also handles stuck due work.
  If a trigger has factual due rows but no event-log progress across repeated
  checks, MCD terminates the stuck PHP process and applies a short cooldown to
  prevent immediate relaunch loops while preserving the failure reason in logs.
- Added: regression coverage for UTC-only event-log due SQL migration and
  stuck due campaign-trigger watchdog cooldown.

## 0.9.138 - 2026-06-14

- Fixed: runtime version identity now matches the published agent package for
  the import monitoring release. This prevents self-update from installing code
  with a newer `pyproject.toml` while `mcd_agent.__version__` still reports the
  previous build.

## 0.9.137 - 2026-06-14

- Fixed: realtime monitoring now reads Mautic imports from the local
  `{prefix}imports` table and publishes actual import ids and states instead
  of internal MCD task ids.
- Added: delayed import rows are exposed as queued monitor items with a
  `delayed` variant, while failed/stopped imports are exposed as finished
  errors.
- Added: regression coverage for import monitor payloads and signal passthrough.

## 0.9.136 - 2026-06-14

- Fixed: campaign trigger due guard now treats Mautic pre-scheduled event-log
  rows as pending when `date_triggered` is earlier than `trigger_date`. This
  keeps scheduled campaigns in the trigger lane until the real execution updates
  `date_triggered` and channel fields.
- Added: regression coverage and release-checklist acceptance criteria for
  pre-scheduled Mautic event-log rows.

## 0.9.135 - 2026-06-13

- Fixed: Mautic 6 plugin operations now prealign all installed metadata-less
  plugin DB versions before `mautic:plugin:install`, matching the existing
  config metadata compatibility patch scope. This prevents unrelated installed
  plugins with newer manifest versions from crashing reload with
  `PluginUpdateEvent metadata=null` while updating another plugin.
- Added: regression coverage for Mautic 6 plugin reload prealign across
  installed metadata-less plugins.

## 0.9.134 - 2026-06-13

- Added: campaign trigger dispatch now revalidates due work immediately before
  launching an automatic per-campaign trigger, preventing stale ring entries
  from relaunching already-completed campaigns.
- Added: automatic campaign trigger progress watchdog. If a running trigger has
  no due database work and no event-log progress across repeated factual checks,
  MCD terminates the stale PHP process and marks the task completed without
  retrying it.
- Added: regression coverage for stale campaign-trigger ring skips and
  progress-watchdog cleanup.

## 0.9.133 - 2026-06-12

- Added: scheduler monitor cycles now preserve SQL-ring segment identity via
  `item_variants.sql`, allowing MCC realtime monitoring to render SQL segment
  queue items separately from standard Mautic segment rebuilds.
- Fixed: lightweight signal collection keeps monitor item variant metadata so
  SQL segment state survives the MCD -> MCC cache path.

## 0.9.132 - 2026-06-12

- Fixed: SQL segment auto-detection now supports exact `behaviors.hit_url = ...`
  filters in addition to `contains`/`like`. Page-hit segments with mixed
  contains/exact URL conditions can stay in the MCD SQL rebuild ring instead of
  falling back to Mautic's standard segment command.

## 0.9.131 - 2026-06-11

- Changed: campaign-pressure segment throttling now treats campaigns as heavy
  only after configurable thresholds are reached. A queued or short-running
  campaign no longer reduces segment workers by itself.
- Added: runtime knobs `campaign_pressure_min_running_sec` and
  `campaign_pressure_min_running_count` for controlling when campaign activity
  should throttle segment dispatch.
- Fixed: regression coverage now verifies that dependent segment chains still
  serialize while unrelated chains can use other workers without campaign
  pressure being triggered by light campaign activity.

## 0.9.130 - 2026-06-11

- Changed: scheduler ring dispatch now launches at most one new automatic task
  per scheduler pass for segment/campaign pools instead of burst-filling all
  free slots at once. Configured parallel limits still cap concurrent workers,
  but new work enters gradually as workers become available.
- Fixed: campaign trigger/rebuild dispatch now alternates the first lane even
  without a shared campaign cap, preventing trigger backlogs from starving
  rebuild work after one-at-a-time dispatch.
- Fixed: active campaign pressure can put segment dispatch into throttle.
  During this pressure, whitelist segments may still run through the configured
  whitelist slot, while non-whitelist and SQL-ring segment rebuilds wait.
- Fixed: SQL-ring segment rebuilds now respect the same dependency-chain worker
  lock as standard Mautic segment jobs, so one dependent chain runs sequentially
  while unrelated chains can still use other workers.
- Added: regression coverage for campaign-pressure segment throttling and
  one-at-a-time ring dispatch.

## 0.9.129 - 2026-06-10

- Fixed: Wazuh service-profile apply now upgrades an already installed
  `wazuh-agent` package when apt reports a newer candidate, instead of only
  reconciling repository/config/service state and returning a misleading
  success.

## 0.9.128 - 2026-06-10

- Added: host `apt_state` now includes cached `wazuh-agent` installation
  state, package version, repository presence, service status and parsed
  manager config so MCC Wazuh Operations can show what is actually installed
  on each host.
- Added: regression coverage for Wazuh state in the apt state payload.

## 0.9.127 - 2026-06-10

- Added: Wazuh service profile apply support. MCD can install or reconcile
  `wazuh-agent`, keep the Wazuh apt repository/key present, write manager and
  enrollment settings, and start the agent service from MCC-managed profile
  data.
- Fixed: existing Wazuh agents still get the Wazuh apt repository/key
  reconciled without forcing a package reinstall, so normal apt maintenance can
  keep them updated.
- Added: regression coverage for Wazuh service profile apply behavior.

## 0.9.126 - 2026-06-08

- Fixed: cluster offsite backup status no longer turns a successful backup into
  `failed` after the remote backup mount is unmounted. MCD now keeps the
  persisted archive verification result unless the offsite path is actually
  visible for revalidation.
- Added: regression coverage for unmounted cluster offsite backup status.

## 0.9.125 - 2026-06-06

- Fixed: scheduler monitor Queued lanes now show only launchable items. Items
  held by per-entity cooldowns or dependency guards stay out of Queued, so MCC
  no longer shows campaign triggers or segments as stuck with free Processing
  slots.
- Added: regression coverage for campaign trigger cooldown visibility in the
  monitor cycle.

## 0.9.124 - 2026-06-06

- Fixed: scheduler monitor cycle snapshots now include campaign rebuild and
  campaign trigger rings, not only segment rings. MCC instance monitoring can
  show upcoming campaign rebuild/trigger ovals in the Queued lane before they
  start.
- Fixed: campaign rebuild/trigger launches are marked inside the current
  monitor cycle, so campaign ovals move through Queued, Processing and
  Finished consistently.
- Added: regression coverage for campaign monitor cycle snapshots.

## 0.9.123 - 2026-06-05

- Added: MCD now pushes lightweight monitor-only scheduler/PHP-console signal
  changes directly to MCC via `/api/v1/agent/signals`. Instance monitoring no
  longer waits for the full periodic state payload to refresh Queued,
  Processing and Finished lanes after scheduler transitions.
- Added: regression coverage for monitor-only signal collection and push change
  detection.

## 0.9.122 - 2026-06-05

- Fixed: scheduler signal payload now exposes planned monitor cycles to MCC,
  so Queued/Finished segment ovals reflect the current ring pass instead of
  only showing recent history.
- Added: regression coverage for full signal collection carrying planned
  scheduler cycles.

## 0.9.121 - 2026-06-05

- Added: segment scheduler monitoring now publishes a per-root cycle snapshot
  with queued, running and already-launched segment ids. MCC can show the
  current ring pass in the Queued lane instead of relying only on recent task
  history.
- Fixed: segment monitor cycles reset after all planned ids complete, so the
  next ring pass returns ids to Queued and Finished does not accumulate repeat
  ovals for the same segment.
- Added: regression coverage for segment monitor cycle snapshots.

## 0.9.120 - 2026-06-05

- Added: scheduler signals now include recently finished MCD tasks, so MCC live
  monitoring can show fast segment/rebuild/trigger jobs after they complete
  instead of losing them between polling intervals.
- Added: regression coverage for recent scheduler history in MCD signals.

## 0.9.119 - 2026-06-05

- Fixed: active campaign profiles now self-heal legacy runtime configs that
  disabled `campaigns:rebuild` while leaving `campaigns:trigger` slots active.
  Campaign triggers again require a same-campaign rebuild pass before launch.
- Changed: the generated system config now enables the campaign rebuild worker
  by default.
- Added: regression coverage for legacy trigger-only campaign scheduler
  configs.

## 0.9.118 - 2026-06-04

- Fixed: SQL-auto `url_in_last_N_days` / `url_title_in_last_N_days`
  page-hit filters now use Mautic's local calendar-day window
  (`DATE(DATE_SUB({now_local}, INTERVAL N DAY))`) instead of a sliding
  `N * 24h` window. This brings SQL preview/rebuild results in line with
  native `mautic:segments:update` for behavior segments.
- Added: regression coverage for page-hit last-days SQL generation.

## 0.9.117 - 2026-06-04

- Fixed: SQL-auto segment rebuilds now compile Mautic relative date filters
  (`today`, `yesterday`, `tomorrow`, `+/-N days`, `N days ago`) as date
  expressions based on the instance-local `{now_local}` clock instead of
  comparing date fields with literal strings such as `'-1 day'`. This prevents
  optimized SQL rebuilds from emptying valid segments like AnanasMK segment 191.
- Added: regression coverage for Mautic serialized relative date filters in
  SQL-auto segment rule detection.

## 0.9.116 - 2026-06-04

- Fixed: campaign trigger audit ids now persist between scheduler ticks until
  the next audit refresh. This prevents all-published audit candidates from
  being dropped by the narrower due SQL before they receive a trigger slot.
- Fixed: campaign triggers now wait for a same-campaign `campaigns:update`
  rebuild pass after planning; after a successful trigger, the next trigger
  cycle requires a new rebuild first.
- Added: regression coverage for audit-discovered campaign ids surviving
  repeated due-SQL planning cycles.

## 0.9.115 - 2026-06-01

- Fixed: Mautic lock cleanup now also clears stale Symfony command lock files
  from `var/cache/run/sf.mautic-*.lock` when their recorded PID is no longer
  alive. This prevents dead segment, campaign, or message locks from blocking
  future scheduler/manual runs.
- Fixed: segment dependency planning now follows Mautic major behavior. Mautic
  7 schedules terminal dependent segments only because core rebuilds
  leadlist-filter dependencies recursively; Mautic <=6 plans dependency
  chains explicitly in parent-before-child order.
- Changed: MCD segment dispatch now treats a connected dependency chain as one
  active slot, so dependent segments are not launched in parallel while
  unrelated chains can still use other slots.
- Added: regression coverage for stale file-lock cleanup, including live-PID
  protection and age-based cleanup for locks without a readable PID.
- Added: regression coverage for Mautic 7 terminal planning and legacy
  dependency expansion.

## 0.9.112 - 2026-06-01

- Fixed: instance migration now rehomes Mautic instance-local paths after file
  sync. Target `cache_path`, `log_path`, `tmp_path`, import directories, and
  media/temp upload directories are rewritten to the target instance root so a
  migrated instance cannot share another local instance cache or writable paths.
- Added: regression coverage for target-local path rewriting during instance
  migration.

## 0.9.111 - 2026-06-01

- Fixed: relay-mode instance migration database streams now prefer
  `mariadb-dump` and no longer request database events. This avoids MariaDB
  failures when `event_scheduler` is disabled on the source.
- Added: regression coverage for the source DB stream dump command.

## 0.9.110 - 2026-06-01

- Changed: instance migration target-pull now imports the database only once,
  after source maintenance is enabled and the final file sync is complete.
  This avoids the previous double full database transfer during migrations.
- Added: regression coverage proving target-pull migration performs one
  post-maintenance database import.

## 0.9.109 - 2026-05-31

- Fixed: plugin conflict DB cleanup now protects selected runtime install
  bundle aliases, not only manifest bundle keys. Managed variants such as
  `AmazonSesManagedBundle` install as `AmazonSesBundle`; their active DB row
  must not be deleted as a conflicting bundle before Mautic reload.
- Added: regression coverage for managed plugin alias DB cleanup.

## 0.9.108 - 2026-05-31

- Fixed: Mautic 6 plugin updates now pre-align selected metadata-less plugin
  DB rows to the installed file version before plugin reload. This avoids the
  Mautic 6 `PluginUpdateEvent` null Doctrine metadata crash without modifying
  Mautic core.
- Added: regression tests for metadata-less Mautic 6 plugin version prealign
  and recovery reload after a previous failed plugin update.

## 0.9.107 - 2026-05-31

- Fixed: plugin install/update metadata workaround now normalizes every
  installed plugin `Config/config.php` before Mautic plugin reload. Mautic 6 can
  fail reload if any installed plugin config has missing metadata, even when
  only one selected plugin is being updated.

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
## 1.2.110

- Fixed profile apply/runtime convergence: named profile capacity is loaded from
  the packaged profile catalog, stale legacy runtime keys cannot replace the
  effective profile snapshot, and profile status/state push expose source and
  precedence diagnostics. Hardware recommendation rules are read from the same
  catalog without changing the selected hardware service profile.
## 1.5.6 - 2026-10-07

- Gate the minimum 60-second segment scan behind terminal completion of the
  current ring; the heartbeat observes liveness and does not start a second
  scan while native, SQL or dependency work remains.
- Persist finite segment retry state and dead-letter exhausted failures without
  blocking completion of independent ring work.
## 1.5.7 - 2026-10-07

- Separate explicit priority and super-priority segment lanes from ordinary
  and SQL queues, with deduplicated dependency-chain exclusion.
- Keep Mautic 7 special work root-only for native cascade and execute implicit
  upstream chains in MCD for Mautic 6 and older.
- Keep priority capacity profile/throttling-driven while super-priority
  automation remains fixed one-at-a-time and throttle-immune.
## 1.5.10 - 2026-10-07

- Promote already-present dirty segment IDs to the front of their active ring
  before dispatch, preventing global-watermark fan-out work from starving
  behind an old ring order.

## 1.5.9 - 2026-10-07

- Preserve explicit special priority membership when a custom or legacy host
  uses a single base ring; zero priority capacity keeps the special work
  queued instead of demoting it into the ordinary ring.

## 1.5.8 - 2026-10-07

- Make profile-manifest validation forward-compatible with future MCC runtime
  keys: known values remain strict, unknown values are ignored for execution
  and preserved in readback.
- Apply `max_segment_retries` and `segment_priority_stale_age_sec` directly
  from the MCC manifest without legacy fallback or hardcoded replacement.
## 1.5.64 - 2026-10-09

- Keep ready image archives self-contained: image deployment no longer performs
  mail preflight, Mautic cache/bootstrap validation, Composer work, or platform
  checks while installing a new instance.
- Preserve runtime mail configuration/wiring and report cache failures with the
  exact selected PHP command and stderr when runtime validation is explicitly
  requested outside the ready-image install path.
- Add regressions proving the image install flow does not invoke Composer,
  platform checks, mail preflight, or `cache:clear`.
