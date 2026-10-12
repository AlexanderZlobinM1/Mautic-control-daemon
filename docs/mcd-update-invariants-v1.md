# MCD update invariants

This is an acceptance contract, not an operator preference. A release is not
complete until the source, tests, published artifact, and target-host receipt
all agree with these rules.

## Admission and blockers

- Ordinary Mautic/PHP work (`segments`, campaigns, imports, plugin operations,
  and other application commands) is not an MCD self-update blocker.
- Only an explicit MCD-owned critical barrier may defer an update: backup,
  source/update lease, deployment, migration, or another barrier added to the
  MCD lock contract with a durable reason and retry state.
- A running application PID must never be killed or waited on merely because
  an MCD update is being installed.

## Durable state and adoption

The update/restart/reinstall path must preserve and re-adopt, without reset or
duplicate spawn, all durable runtime state:

- TaskStore running rows and PID/start identity;
- ordinary, static, import, retry, and follow-up queues;
- claims, running state, terminal receipts, monitor projections, and runtime
  sync snapshots.

The supervisor unit uses the installed venv package and `KillMode=process`.
After restart, the daemon adopts surviving application workers from TaskStore;
source-tree paths are not used as worker identity.

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

The user-facing algorithm in [`ALGORITHM.md`](../ALGORITHM.md) is the detailed
scheduler contract. Changes to this file require matching source/tests and a
new immutable candidate; writing the rule alone never closes the task.
