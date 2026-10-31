# MCD 1.7.0 boundary — candidate, not compliant yet

1.7.0 is an architecture release. No further 1.6.x release may be used to claim
this migration. Renaming the version while embedded flows remain is insufficient.

The latest operator clarification supersedes broad earlier recipe classifications.
MCD retains its own agent logic, direct Mautic commands and direct Mautic
management, including scheduling, task queues and process supervision. Working
behavior in every entrypoint is the acceptance baseline, not only installation.

External wrapping owns system-dependent concrete actions: OS/service/package/
repository provisioning, OS-specific paths/owners and configuration generators.
MCC owns distribution/versioning and universal profile/matrix data. MCD retains
generic artifact retrieval/cache/integrity/execution and progress/results/state.
All Mautic bin/console commands, including doctrine:migrations:migrate, remain
MCD responsibilities with native task/supervision/receipt semantics. An external
composition may request the existing MCD Mautic command operation at its proper
stage; it must not duplicate Mautic CLI implementation or semantics. Pre-existing
broken upgrade/migration repair remains deferred.

The existing custom manifest v1 gains only a relative `entrypoint` for a standard
tar.gz containing external `scenarios/` and `profiles/`. The whole archive SHA
covers included files; regular files/directories only, no links or escaping
paths. Entry point: `scenarios/debian-family/skeleton.py`. Working directory is
the extracted root. Arguments remain external data, for example `--composition
profiles/<selection>.json --selection <approved-key> --domain <dns>`.

A human untars the same archive and invokes the same entrypoint. The script
owns algorithm ordering and algorithm-specific downloads. It invokes installed
mcd-cli direct Mautic operations when required, never bin/console itself and
never recursively its own composition. Standalone requires neither a daemon nor
Portal; installed mcd-cli is a legitimate Mautic-operation prerequisite. No
managed recipe branch is allowed. JSONline progress/results work on a console
and with an MCD consumer; existing commercial framing may adapt output only.

Cache admission is for the current selected TASK. The exact task manifest/pins
identify required objects. A valid manually copied object is equivalent to an
MCD-downloaded object. Reuse valid bytes with zero artifact GET/HEAD calls.
Offline execution does not require global CURRENT/latest reachability. Verify
all selected composition/profile/child/image-input pins before system mutation;
missing/corrupt required objects fail without partial start or version fallback.
Do not require optional/unselected repository contents.

Current custom cache layout is `<custom_cache_dir>/manifest.json` plus
`downloads/<exact-key>/<exact-key>-<sha256>.<suffix>`; bundles use `.tar.gz`.
Manual population copies the selected manifest and exact artifact bytes there.
`custom --fetch-only --expected-sha256 <sha> <key>` verifies/prefetches without
execution. Whole-task selected child/image closure validation remains a required
external-composition preflight gate; current cache tests alone do not prove it.

MCD/scripts never change DNS or receive DNS credentials. Existing certificate
references or an MCC-owned prerequisite/wait/resume operation replace DNS-01.
Current native 135 audit found legacy DNS-01 renewal references and an active
Certbot timer. Metadata only was read; these host artifacts need a separate
MCC/Operations migration preserving working certificates.

Pending classification inventory (not a deletion authorization) includes managed image installation; Mautic
upgrade/manual-upgrade/composer-move/instance-migration and restore workflows;
OS provisioning via APT/repository/service/PHP/DB/Redis/Docker/Wazuh/mail; profile/default
matrices/provider recommendations; external config writers and their automatic
callers; agent self-update/uninstall recipes. Direct Mautic management, agent logic, internal metadata persistence and
read-only observations must be separated from system recipes, not blanket-deleted
or blanket-exempted. The earlier 17-file audit is an input, not completion proof.

Required release gates:

- All forbidden source implementations, seeds, imports, callers, fallbacks and
  obsolete active documentation/tests removed or replaced with generic dispatch.
- Independent MCC registry/distribution and existing commercial broker verified.
- Warm/manual cache uses zero artifact network calls; complete offline task
  runs, incomplete/corrupt task never begins system changes.
- Isolated direct and MCD executions of the same bundle select the same children
  and profiles and produce matching step/progress/result evidence.
- Static and native active-path audit proves DNS mutation is MCC-only.
- Required regression, relevant scenario tests, normal test→approved publication
  and working-instance preservation receipts completed.

None of these gates is satisfied merely by this document or a version change.


## Global entrypoints and incremental sandbox scope

These rules apply equally to manual CLI, standalone execution, corporate MCC
and commercial Portal. Portal is a transport/broker consumer, never a dependency
of manual execution or an alternate execution engine.

The current extraction batch preserves working behavior. Pre-existing broken
upgrade/migration flows are inventoried and deferred, not repaired as part of
this batch and never reported as passing. Fix regressions introduced by the
extraction itself. Use the canonical Mautic vanilla sandbox executor for leases;
no production, fleet rollout or edits to existing user instances.

Only Debian/Ubuntu-family implementation is in scope. Observed distro/version
and architecture select an MCC-owned Debian family catalog; MCD must not infer
provider plans or embed configuration/profile matrices. First online sync fetches
the complete required family scenario stack. Subsequent online manifest checks
fetch only changed, missing or corrupt objects. This synchronization scope does
not change offline admission: a complete selected task closure remains runnable
without a current remote manifest or unrelated family contents.

Current implementation gaps (not implied complete by the contract):

- CLI custom cache and bundle extraction have isolated fixture coverage, but no
  family catalog endpoint/schema has been integrated yet.
- Commercial archive media/entrypoint propagation and launcher extraction remain
  pending. SSMCDX01 framing and existing transport authorization are preserved.
- Broker artifact prefetch is not advertised yet; its adapter must use the same
  generic byte cache, configured origin/auth and exact expected size/SHA.
- `read_host_facts` now has an unshipped additive `hardware_observation` containing
  measured positive `cpu_count` and `memory_kib`, or null when unavailable. It
  changes neither applicability facts/digest nor profile selection authority.
- Contract 2.1 validation now accepts a job without stage metadata. If stage
  metadata exists, its shape and execution binding are checked generically;
  names, recipes and ordering belong to the external composition.


## Universal profiles, family-specific generators

Hardware system profiles and HOST workload profiles are universal external data
owned by MCC. They retain independent meanings; extraction does not change the
existing initial recommendation rule. Neither distro/family nor target creates
another copy/version of these values or matrices. Tasks reference shared objects
by digest; one cached object can serve tasks from different families.

Configuration generation belongs to external family/distro blocks. The flow is
universal profile values → selected external generator → actual config files.
Paths, owners, service names and file layouts are generator/target data; they do
not become profile formulas. Related distros share a generator wherever behavior
is identical, with overrides only for actual differences.

The first selected-family synchronization includes its scenarios/generators plus
referenced universal profile objects. A `profiles/` path in a task is a local
reference/layout, not authorization to fork universal values into Debian-specific
profile authority. Final family/task manifests are still awaiting actual MCC
exports; no universal content-addressed storage implementation is claimed here.


## Direct Mautic operation candidate (unpublished)

External algorithms invoke `mcd-cli exec --instance-uid <stable-uid> --command
cache:warmup --timeout 600 --console-arg=--env=prod --console-arg=--no-debug
--console-arg=--no-interaction`. An optional `--config` uses the existing CLI
configuration mechanism. No daemon/Portal is required. The script does not pass
PHP, root or runtime user. `--command version` selects Mautic `--version`;
`--command doctrine:migrations:migrate` selects that direct console operation.
Arguments are literal argv entries, not shell text or workflow templates.

The new selector requires exactly one registered UID with complete absolute root,
`runtime_php_bin` and `runtime_user`. Unknown/ambiguous/incomplete identity fails
before execution. There is no first-instance or global-PHP fallback. Legacy
inventory runtime metadata may be incomplete: canonical enrichment/registration
is an outstanding dependency, not permission to guess in an external script.
Existing root/PHP/user CLI invocation remains compatible. Stdout contains existing
CLI/executor output (possibly update notices); exit code follows that executor.
The executor combines child stdout/stderr; this is not a new JSON wire contract.

Coverage includes two distinct instance runtimes, unknown/ambiguous/incomplete
records, literal arguments through a real fixture subprocess and its nonzero
exit, and existing passive behavior. This is not native Mautic/PHP evidence.


### Pre-warmup runtime registration (approved boundary; unpublished candidate)

The operator approved declaring actual created facts before registration as an
external non-Mautic handler. After registration, Mautic operations use UID only.
The following syntax is an unpublished candidate, not a completed release.

Candidate: `mcd-cli instances add --config <cfg> --name <name> --root <created-root>
--console-path <created-console> --instance-uid <stable-uid>
--runtime-php-bin <declared-absolute-php> --runtime-user <declared-user>`.
The external installer declares facts of the runtime it has just created;
subsequent Mautic operations use only the stable UID. This operation changes
inventory metadata, not OS configuration, and does not discover or guess PHP.
UID/root conflicts and partial runtime declarations fail before saving.

Rescan preserves UID and registered host PHP/user for the same root. Explicit
manual host registrations with complete runtime survive a pre-vhost rescan even
if discovery cannot see that root yet. Neighbor identities are not rebound.
Tests cover two runtimes, empty/pre-vhost discovery, later discovery, conflicts
and incomplete declarations with real temporary inventory databases. Native
Mautic execution evidence is still pending; none of this is published yet.


Registration add with `--json --operation-id <existing-job-or-request-id>` returns
`status`, `instance_uid`, `root`, `created` and `registering_operation_id`. Only a
new record returns the operation ID; an existing/replayed record returns false
and null and must not be removed by installer rollback. Identity/runtime conflicts
fail before writing; an existing replay preserves DB metadata. `BEGIN IMMEDIATE`
serializes receipt creation in the existing inventory.

Owned rollback is `instances remove --instance-uid <uid> --root <root>
--operation-id <existing-id> --json`. Exact UID/root/recorded operation must match;
there is no fallback to name. Result is `{status: "ok", removed: bool}`. This
reuses ordinary task identity and adds no secret token or key layer. Rescan
preserves recorded registration operation alongside UID/runtime. Legacy broad
name/root removal remains separate and must not be used by the installer.

Latest full candidate run before operation-ID naming simplification: 1943 passed, 61 failed, 57 skipped, 105 subtests passed.
The exact 61 failure identifiers match the 1.6.38 baseline (zero new failures).
These are local regression results, not native installation acceptance.
