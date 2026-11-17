# Mautic patch plans

The static v1 patch catalog is retired. MCD rejects v1 apply plans instead of
using embedded payloads. The generic v2 adapter remains available for existing
v2 plans and rollback. New catalog execution uses the immutable v3 interface in
`mcd_agent/contracts/mautic-patch-resolution-v1.json` and its versioned fixtures.

MCC selects applicability, dependencies, conflicts and ordering from operator
Assets. MCD validates the selected plan and executes its declared file patches.
Disabled or unverified catalog records never acquire an agent-side fallback.
A missing catalog or unsupported predicate gives an explicit blocked result.

## Root and gate contract

Paths are relative to the Mautic application root containing `app/`, `plugins/`
and `bin/console`. Composer's project/lock root is separate; the application may
be `docroot/`. The workflow verifies a unique application root and rejects
ambiguous or symlink layouts. Payload paths are not stripped or relocated.

`source_paths` includes both diff paths and gate-only identity files. Only diff
paths are mutated. Common identity SHA gates may appear in both groups:
`vulnerable_all; fixed_all; mixed_or_unknown=error` matches complete groups,
not individual gates. Vulnerable-only is candidate; fixed-only is already;
both or neither is ambiguous. `path_state` uses `expected_state=absent|present`;
`sha256` uses `expected_sha256` over exact bytes; `exact_count` uses a literal
`needle` and integer `expected_count`. The exact JSON fields are in the contract.

File apply is serialized per application root and atomic per phase. The ledger
preserves original bytes, mode, uid/gid and exact before/after hashes. Rollback
checks the immutable plan binding, unchanged identity gates and every recorded
postimage before restoring files or removing created files. Drift is not
repaired by overwriting it. Legacy snapshots are imported only through a
catalog-declared descriptor after validating source gates, paths and hashes.

## Large-plan transport

Large binary plans are transferred by the authenticated MCC job to an existing
private root-owned regular file. Use an absolute path, mode `0600`, and the
original canonical plan digest/run ID:

```sh
mcd-cli mautic-upgrade apply --root /instance --mode composer --target VERSION \
  --patch-plan-file /private/plan.json --patch-plan-sha256 CANONICAL_SHA \
  --patch-run-id ORIGINAL_RUN --yes --allow-minor
```

`mautic-patch-plan` and `mautic-patch-backup-authorize` use `--plan-file` and
`--plan-sha256`. Inline JSON remains available for small plans. The file digest
is SHA-256 of sorted compact ASCII-escaped JSON followed by one LF, as in the
resolution contract; it is not a hash of arbitrary formatting. Wrong owner,
permissions, symlink, duplicate JSON keys, oversize or digest mismatch is rejected.
MCC removes its transferred plan file on the job's terminal path.

## Upgrade staging and backup

A v3 upgrade prepares the exact target in an isolated source copy with
application scripts disabled before maintenance, permissions or live source
installation. It validates target version and all patch conditions there,
records package/lock and selected source hashes, and rechecks the original
source before mutation. Composer installs the staged lock; ZIP uses the same
verified archive hash. Installed target version/layout/file hashes are checked
again before patch application. Each supported phase has its own plan-bound
ledger; terminal failure attempts reverse-phase patch rollback. The staging
copy is removed on success, cancellation or failure.

The supported upgrade record phases are `post_source_install`,
`before_cache_warmup`, `before_asset_generation`, `preflight_frontend_assets`
and `before_doctrine_migrations`; unsupported workflow phases fail before live
mutation. Runtime phases are selected independently at their existing cadence.

The signed schema-repair backup attestation applies to guarded M6-to-7 repair.
Its JSON envelope binds instance/root/source/target/run/plan SHA and MCC-validated
backup evidence; it is supplied after backup without changing the immutable
plan. Signature bytes and fields are pinned in the contract. For 7-to-7 the
repair attestation is not required. This does not waive the ordinary MCC backup
policy or the agent's `--backup` request; regression covers that distinction.

## Legacy caller migration and disabled policy

| Previous caller | Current route or safe policy |
|---|---|
| M6 metadata file remediation | Catalog `before_plugin_reload`, existing required/off policy and unknown-version opt-in; no embedded method replacement |
| M7 import-tag daemon/operator actions | Catalog `before_background_imports`; operator rollback names its original `--run-id` |
| GrapesJS/CKEditor periodic remediation | Catalog `before_asset_generation`; payload and version applicability come from Assets |
| Generic upgrade/static v1 patch stage | Generic v3 staged target and exact phase execution; static v1 plans rejected |
| Pre-upgrade import-tag revert and post-upgrade direct patch | Removed; source upgrade uses its immutable plan and daemon reconciliation uses the catalog |
| Twig include hotfix | Historical function cannot mutate source; missing dependency predicate remains a readiness blocker |
| M4 Hostnet composite compatibility | Disabled historical action, no source mutation |
| Plugin config metadata insertion | Disabled; plugin source changes belong to its owner |
| DB plugins.metadata NULL/empty/invalid-JSON rewrite | Removed and capability not advertised: upstream source does not establish that column as the event metadata source |
| Temporary M6 native-migration core replacement | Removed; exact reload failure may invoke catalog runtime repair, otherwise stays failed without core mutation |
| Retired patch automatic restore | No inferred backup/path/marker; explicit catalog legacy descriptor required |

M6 policy `off` prevents automatic remediation, including plugin reload recovery.
When the version is unknown, local opt-in, independently confirmed major,
explicit catalog permission and source gates are all required. Local version
window settings no longer supply catalog applicability decisions.

Preventive `before_plugin_reload` plans are also consumable without agent
registration. The configured public plugin manifest may bind one same-origin,
digest-verified Assets projection. MCD binds the immutable v3 plan template to
the observed major, owner-published semantic version range, install type and
local execution context, then uses the same plan validators, exact source gates
and executor as a registered agent. Unknown version blocks a required
projection; a known version outside every declared range is a safe no-op. A
required projection that is unavailable, invalid, missing or ambiguous blocks
before native reload. Local execution evidence is durable before optional MCC
notification; notification failure is never an execution rollback or replay
signal.

Nonempty `git_patch_v1.parameters` is rejected until a typed capability exists.
Role row count, configured-prefix migration-pending/namespace facts, dependency
package predicates, method-scoped predicates and migration/DDL rollback barriers
are not inferred from file signatures. Records needing those facts stay disabled
and remain explicit upgrade-readiness blockers. Source-only consumer acceptance
is not full upgrade readiness.

The ROLE readiness policy is pending migration plus non-admin role count
(`is_admin=0`) greater than zero, not total role count greater than one.
One non-admin role requires remediation; one admin role alone does not.
Missing schema, prefix, column or exact migration encoding blocks rather than
producing a zero count. This typed capability is not advertised by 1.2.63.

## Consumer acceptance

MCD 1.2.63 executed the owner-published 4,126,599-byte Git binary payload with
SHA-256 `fac7c6c72549cb3630dded1648c069d87d867bf9fb2050e2a4765b3c7944f321`
from Operations ref `3681e699ba6bdd387f6e9d6741c2fc6f1b3bc3ec` in an isolated
Composer project/docroot application fixture with exact fresh-package identity
files. File CLI transport, complete-group gates, three exact postimages,
identity-file immutability, idempotency, created-file rollback, partial/identity/
postimage drift rejection and root/symlink containment passed. The source record
remained disabled; no customer upgrade was performed. The immutable consumer
source/package ref accompanies the release result.
