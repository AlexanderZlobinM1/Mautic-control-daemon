# MCD update invariants

The rule in this document is mandatory and non-waivable. Source, behavioral
tests, runtime artifact, and target-host evidence must agree with it. A
missing or conflicting layer is an acceptance failure, not a reason to weaken
the rule.

This is an implementation and release-evidence reference, not a second
algorithm. The normative scheduler algorithm is `ALGORITHM.md`. A release is
not complete until the source, tests, published artifact, and target-host
receipt all agree with both documents.

## Admission and blockers

- Ordinary Mautic/PHP work (`segments`, campaigns, imports, plugin operations,
  and other application commands) is not an MCD self-update blocker.
- Only an explicit MCD-owned critical barrier may defer an update: backup,
  source/update lease, deployment, migration, or another barrier added to the
  MCD lock contract with a durable reason and retry state.
- The updater's `scheduler.pause` is not itself proof of an active critical
  operation. A new update barrier records its operation, PID, and PID-start
  identity beside the flag; a legacy/orphaned `mcd-update-admission` flag is
  recovered only while the source/update lease is free. Active owner/lease
  evidence keeps the barrier, while manual/maintenance values such as
  `paused` are never removed by updater recovery.
- A running application PID must never be killed or waited on merely because
  an MCD update is being installed.
- This applies to every ordinary application command, not only the examples
  listed above. A new blocker may be introduced only as an explicit
  MCD-owned lock/barrier added to this algorithm and its behavioral tests.

## Scheduler result semantics

- `segment_filter_safety` is diagnostic-only. A heuristic such as `today -1`
  is not a Mautic return code and must not silently remove an executable
  segment from the ring or mark it terminal before the profile-bound command
  starts. Normal Mautic refusal follows the ordinary `TaskStore` start/retry
  path and ends in a red terminal receipt.
- Only proven unsafe segment topology (`dependency_cycle` or
  `self_reference`, including their published descendants) is converted to an
  explicit terminal data error after durable admission, with
  `data_error:<code>`. The healthy tail must continue, the root must reach
  terminal, and the next cycle must be independently admitted. This is a task
  data error, never an MCD update barrier or a reason to leave assigned work
  pending forever.

## Durable state and adoption

The update/restart/reinstall path must preserve and re-adopt, without reset or
duplicate spawn, all durable runtime state:

- TaskStore running rows and PID/start identity;
- ordinary, static, import, retry, and follow-up queues;
- claims, running state, terminal receipts, monitor projections, and runtime
  sync snapshots.

The same state must remain root-local and fairly dispatchable: one host,
instance, or root may not become a global work loop or starve unrelated roots;
foreign-root metadata is never liveness or update-barrier evidence for the
local root.

Persisted segment-chain reverse indexes are scoped by canonical root, task-key
owner, closure membership, and cycle generation. A legacy or foreign reverse
index is metadata only: it cannot keep a different root claimed or defer its
ordinary work. A live owner with matching PID start identity remains the sole
source of liveness for its genuine closure, so overlapping IDs in separate
roots stay isolated without sacrificing restart adoption.

The supervisor unit uses the installed venv package and `KillMode=process`.
After restart, the daemon adopts surviving application workers from TaskStore;
source-tree paths are not used as worker identity.

For UID-scoped runtime policy, a partial instance record is merged into that
UID's canonical row. An omitted UID or omitted field is not an intentional
clear. Removing the last local field must publish an explicit empty
`instance_desired_states[uid]` row with the current per-instance revision;
otherwise a replace request can preserve an old MCC sparse map and resurrect
the supposedly deleted override after the next rebase. A native clear of the
whole sparse map is likewise represented by an explicit empty map, never by
omission.

## Runtime-only release payload

The production MCD archive is runtime-only. It may contain the importable
`mcd_agent` package, runtime resources, dependency requirements, a wheel, and
`mcd-runtime-manifest.json`. It must not contain checkout/provenance material:
tests, docs, README/algorithm/changelog files, `pyproject.toml`, build output,
or `.git` data.

The source checkout remains an input to the release gate and may be retained
only as bounded `src`/`src.next` staging or rollback material during the
atomic switch. After installed-package smoke, service/import verification, and
adoption/rollback proof, no active production checkout or duplicate test tree
may remain.

The runtime manifest hashes its payload but not itself and carries only the
source commit identity. The archive SHA, source-archive SHA, wheel SHA, and
manifest SHA are bound by an external release receipt. This keeps provenance
detached and avoids a self-referential field while preserving independent
auditability of the installed package.

## Legacy-to-runtime bridge

The legacy 1.5.x updater is allowed to fail closed when it receives a
runtime-only archive: a missing `pyproject.toml` is a compatibility signal, not
permission to repack the immutable runtime artifact or to fall back to a
checkout package. The one-time transition is performed by the separately
transported `tools/mcd_runtime_bootstrap.py` component installer. It must use
the local MCD update lease and scheduler admission barrier, fail closed on an
active or unreadable backup lock, install and verify the wheel before the
source switch, keep the previous source as bounded rollback material, and
leave the supervisor on `/opt/mcd` with `KillMode=process`.

This bridge may change only MCD runtime files, the managed venv package,
systemd's MCD drop-in, and durable MCD receipts/state. It must not edit MCC
source, update policy, Mautic profile/config/data, DNS, or ordinary Mautic
workers. The disposable bridge receipt is required before any live retry.

## Required evidence

Every candidate handoff must include:

1. source commit and exact source-archive identity;
2. runtime archive SHA, manifest SHA, wheel/resource hashes, and a listing
   proving forbidden checkout material is absent;
3. pre-switch runtime smoke and installed import path under venv
   `site-packages`;
4. successful migration, rollback, and re-adoption receipts in disposable
   environments;
5. target-host evidence that ordinary work survived restart, retained its PID
   or start identity where applicable, was adopted from TaskStore, and did not
   duplicate.

For a host still running a pre-runtime-only updater, the handoff additionally
requires the `mcd-runtime-bridge-qa-v1` disposable receipt and the live bridge
receipt before the normal targeted self-update sequence can continue.

The user-facing algorithm in [`ALGORITHM.md`](../ALGORITHM.md) is the detailed
scheduler contract. Changes to this file require matching source/tests and a
new immutable candidate; writing the rule alone never closes the task.
