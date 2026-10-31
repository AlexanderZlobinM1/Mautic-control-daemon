# MCD execution boundary

Target architecture: 1.7.0. This is a hard prohibition, not a recommendation.

MCD retains its own agent logic and launches direct Mautic commands/management
itself. Preserve scheduler, task queue, process supervision, progress, receipts
and working CLI/MCC/standalone/commercial behavior. Do not remove these under a
blanket interpretation of installation, upgrade or configuration.

System-dependent concrete actions belong to independently versioned external
wrapping distributed by MCC: OS packages/repositories, service provisioning,
OS-specific paths/owners, system config generators, hardware/profile matrices
and provider formulas. Generic retrieval/cache/integrity/execution remains MCD.
Compatibility entrypoints must preserve working arguments/results.

All Mautic bin/console commands, including doctrine:migrations:migrate, are
executed by MCD itself with its existing task/supervision/receipt semantics.
External compositions may request the existing MCD command operation at the
proper stage; they must not duplicate Mautic CLI implementation or semantics.
Repair of pre-existing broken upgrade/migration flows remains deferred.

The same external scripts and profiles must work directly with parameters and
through MCD. Only the output consumer differs. Manual execution does not require a daemon or Portal. An installed mcd-cli is
a legitimate prerequisite for direct Mautic operations. The script owns its
algorithm, ordering and algorithm-specific downloads; it invokes existing
mcd-cli direct Mautic operations, never bin/console itself and never recursively
dispatches its own composition. Do not duplicate Mautic CLI semantics in scripts.

DNS mutations belong exclusively to MCC. MCD and external scripts must not
perform DNS-01 mutations, call DNS provider APIs or own/receive DNS credentials.
They may report prerequisites and consume existing certificate references.

For the selected task, verify the complete required artifact/profile/component
closure before the first system mutation. Reuse matching cached bytes without
artifact GET/HEAD requests, including manually populated cache files. Offline
execution requires exact selected-task expected digests, not a live catalog,
a global latest version or transport provenance. Missing/corrupt required
objects fail concretely; unselected optional objects are irrelevant. Fetch
only missing/corrupt required objects when transport is available.

MCC remains distribution authority. Commercial transport uses the existing
MCD → Portal → MCC broker/cache; never give an MCC credential to a browser or
commercial agent. Add no separate engine, profile authority or security/key layer.

Do not declare or publish 1.7.0 until all forbidden system-dependent paths/callers are
externalized, external replacements are verified and direct/MCD execution of the
same artifact has passed. Keep remaining legacy paths explicitly pending.
Preserve working instances, immutable published history and unrelated changes.

Hardware profiles and HOST workload profiles are universal external data,
independent of distro/family. Keep their MCC authority and independent meanings;
cache one object by digest and reference it across family tasks. Do not duplicate
or version profile/matrix values under per-family trees. Configuration generators
are external family/distro blocks that consume those universal values. Reuse
identical generators; overrides contain only actual path/owner/layout differences.
Family first sync includes generators/scenarios and referenced universal objects,
not copied profile trees. Do not introduce profile formulas or new behavior while
extracting working mechanics. Pre-existing broken flows remain deferred.

Before registration the external non-Mautic installer may declare actual created
runtime facts through the existing inventory. After registration all Mautic
operations use stable UID only. Registration receipts reuse the existing job or
request operation ID; add no UUID ownership credentials or independent key layer.
Rollback removes only a newly created record matching UID/root/operation ID.
Existing broad name-based removal is not an installer rollback contract.

Installed hosts receive ready runtime artifacts only. Development project trees,
tests, build files and checkouts must never be an installed runtime dependency.
Use venv site-packages for imports/CLI and verified /opt/mcd/runtime artifact cache;
never prefer or recreate /opt/mcd/src for version/import/update logic. Legacy
migration validates all consumers and successor runtime before retiring the old
tree. Preserve working tasks, config and original baseline evidence.
