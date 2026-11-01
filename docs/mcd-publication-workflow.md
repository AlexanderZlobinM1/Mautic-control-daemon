# MCD publication workflow

Publish MCD owns canonical merge, release packaging, immutable artifact pins,
catalog metadata, HTTPS readback and channel promotion. MCD Inbox owns runtime
source and tests. MCC owns its channel consumers and host assignments. Portal
owns external recipes. Do not modify another owner's checkout.

## Channels

Approved identifies accepted runtime releases. Test selects the candidate under
release validation. Dev is a separate development channel for the 1.7 line,
using the same catalog/channel machinery, never a second release registry.

An empty Dev channel has no selected release. Register the empty channel using
the representation agreed with MCC's existing consumer; never invent a version,
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

Each affected line needs its own exact source commit, tests, artifact hashes,
channel decision and receipt. A PASS on one line is not acceptance of the other.
Whole-1.7 acceptance remains on HOLD independently of Dev availability and ongoing
unlisted/native QA. Channel administration must not interrupt those QA actors.
