# MCD publication workflow

Publish MCD owns canonical merge, release packaging, immutable artifact pins,
catalog metadata, HTTPS readback and channel promotion. MCD Inbox owns runtime
source and tests. MCC owns its channel consumers and host assignments. Portal
owns external recipes. Do not modify another owner's checkout.

## Channels

Approved identifies accepted runtime releases. Test selects the candidate under
release validation. Dev is a separate development channel for the 1.7 line,
using the same catalog/channel machinery, never a second release registry.

An empty Dev channel has no selected release. The agreed existing-catalog
representation is `channels.dev: null` (MCC 1.2.300 contract). API/CLI reject host
assignment, UI shows Dev (empty), and agents receive no target. A future pointer
requires an explicitly accepted build flagged `dev`. Never invent a version,
publish an unlisted QA fixture as a release, or point Dev at Approved as a
placeholder. Unlisted R31 and other nominal 1.6.x QA fixtures are not Dev releases.

Dev selection is explicit. It must not become latest, a missing-channel fallback,
an enrollment default or an automatic host assignment. Adding Dev changes no host
assignment. Initial operator policy assigns zero hosts to Dev. Record a verified
assignment count from the assignment owner rather than inferring it from catalog
contents. Stable Approved/latest remains 1.6.39 until separately authorized.

Test and Dev are independent; never reinterpret Test as Dev. A Dev publication
does not imply Test/Approved promotion or whole-1.7 acceptance. Preserve historical
approved flags, immutable artifact bytes, runtime_only evidence and rollback pins.

## Release validation

Start from a clean immutable owner handoff. Canonical builds require the actual
origin/main gate; isolated QA assembly is separately identified and is not a
canonical release. Publish immutable artifacts once, verify actual HTTPS bytes
and SHA-256, and atomically compare-and-swap catalog metadata with rollback copies.
Promote only after the explicitly required source/native/channel gates pass.
Describe baseline failures and proof limits honestly. Keep host rollout separate.

## Temporary two-line HOTFIX policy

For every defect, first record applicability to both maintained 1.6 and developing
1.7. Backport or forward-port the minimal relevant fix only when applicable; an
unaffected line gets an explicit rationale. Do not copy all commits, recipes,
profiles or 1.7 architecture into 1.6 as a hotfix.

Shared-logic fixes are one synchronous hotfix cycle, not a deferred backport.
Rings, queues, scheduler and shared core Mautic logic require paired source fixes,
tests and commit/evidence links in stable 1.6 and developing 1.7. Do not close the
hotfix or publish the stable fix while its Dev source/test counterpart is pending.
If whole 1.7 is still on HOLD, record its fix and tests in the development branch;
this does not authorize Approved promotion, production or host assignment. A truly
branch-specific fix requires concrete not-applicable evidence for the other line,
not mechanically copying incompatible implementation. Audit existing migration
hotfixes for paired coverage without interrupting the native QA actor.

Each affected line needs its own exact source commit, tests, artifact hashes,
channel decision and receipt. A PASS on one line is not acceptance of the other.
Whole-1.7 acceptance remains on HOLD independently of Dev availability and ongoing
unlisted/native QA. Channel administration must not interrupt those QA actors.

## Operator exception: this MCD 1.7 development campaign

The operator explicitly authorizes direct 1.7 development on exactly
`135.181.110.14` and `46.225.78.247`. This narrow campaign exception supersedes
the earlier zero-Dev-host development restriction; it is not a general policy for
other projects, production or fleet hosts. `46.224.209.196` is forbidden.
Channel assignment is a separate owner-controlled operation, not inferred from
this development authorization.

MCD code remains owned by MCD Inbox; Portal code/scenarios by Portal owner.
Serialize shared host mutations and restarts through MCD and Infra leases.
Before changes, capture baseline services/instance facts and exact owned backups.
Keep local diffs/evidence while iterating directly, debugging and testing on these
hosts. Commit only the final proven result, not every experiment. Coordinate DNS
and shared infrastructure changes separately.

Publish must not produce another unlisted Rxx artifact, VM package cycle,
Test/Approved release or reinstall for each 1.7 experiment. Receive the final
publication handoff after the owners demonstrate working behavior and regression
checks. Do not auto-update live Dev source or deploy automatically from Dev;
never overwrite in-progress changes or add unrelated host assignments.

Preserve terminal/evidence from any already-running QA actor without interrupting
it, then transition development to the two-host workflow rather than initiating
new package/VM cycles. Existing immutable 39/40/QA bytes remain unchanged.
The separately authorized stable40 Test hotfix lane may reach terminal acceptance
using its owner proofs. It is not the template for Dev iteration. Shared hotfixes
still require the synchronous paired stable/Dev source-and-test cycle above;
stable publication occurs only in its separately agreed stable release cycle.
