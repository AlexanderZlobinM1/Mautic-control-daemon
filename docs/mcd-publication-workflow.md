# MCD publication workflow

Publish MCD owns canonical merge, release packaging, immutable artifact pins,
catalog metadata, HTTPS readback and channel promotion. MCD Inbox owns runtime
source and tests. MCC owns its channel consumers and host assignments. Portal
owns external recipes. Do not modify another owner's checkout.

## Mandatory Definition of Done

A runtime fix is complete only after all applicable gates below have evidence:

1. Reproduce the original failure and add a regression for it. Exercise the
   affected queue/ring, identities and instances, including unrelated work
   continuing; a single host or happy-path unit test is not global acceptance.
2. Commit and push the owner changes, merge them into canonical release source,
   and verify the remote merge commit. A worktree, feature branch or live-only
   patch is not delivery. Apply the two-line hotfix policy below.
3. Publish through Publish MCD from the verified canonical commit. Record the
   immutable package and QA SHA-256, manifest and channel readback. Never
   overwrite or republish different bytes under an existing version.
4. Operations performs the authorized standard rollout, preserving configuration
   and running post-checks. Read back installed/running/package identity and
   health; keep MCC server marker evidence distinct from MCD package evidence.
   A repository-cache hash is not the published runtime archive hash.
5. Accept the original scenario through the ordinary workflow: queue/status/
   receipts, settings desired/effective/ACK, restart/recovery and continued
   unrelated work as applicable. Do not manufacture acceptance by clearing
   state, editing host configuration or manually running the affected task.
6. Return source and merge refs, tests and their limits, published artifact or
   server marker, remote readback, health and timestamped acceptance evidence.
   Clean the owner's worktree and coordinate remaining work with its owner.

Code complete, merged, published, deployed and accepted are distinct states.
Keep the fix open when a required gate is pending; report the concrete blocker
and owner rather than declaring success. Never leave an accepted fix only in a
working branch or on one host. Documentation/test-only changes are published
by canonical Git merge and remote readback; they do not authorize rebuilding an
already immutable runtime version. The 1.7 architecture HOLD remains binding.

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
contents. Stable Approved/latest is read from the authoritative release catalog;
changing it requires the ordinary authorized release process.

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
