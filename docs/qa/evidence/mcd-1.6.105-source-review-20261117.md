# MCD 1.6.105 source review evidence

Date: 2026-11-17 (Europe/Belgrade)

## Scope

- fail-closed plugin package admission by authoritative Mautic release metadata;
- durable host-level APT upgrade enqueue/status/cancel and terminal heartbeat;
- no production host mutation and no repeated Alex operation during this review.

## Public manifest readback

The public manifests were fetched read-only from
`https://servercontrol.sales-snap.com/mauticctl/packages/mautic{major}/manifest.json`.

| Mautic major | Plugin rows | Rows with compatibility constraints | Raw manifest SHA-256 |
|---|---:|---:|---|
| 4 | 10 | 0 | `4754187e6b5ad19f46c3ac30506db40e872469b118528cefd2e40221607aac97` |
| 5 | 20 | 20 | `14e6d1ab99db00ab74c6f5ed7a5480ef4ff5b3d91d95aed01ce1e50d74b6f572` |
| 6 | 39 | 39 | `3c6bb7561418127086b8d7fdc20bdb05665969764e2352c4aa10c204bdaadf97` |
| 7 | 42 | 42 | `f476c689cd0659e3a37d200465fa252081d2932970c5c0ca0f4dd2f9c24f3464` |

The M7 manifest contains package projections that do not declare Mautic 7.2
compatibility, including `hostnetauthbundle:6-7` and an M6-only
`aiemailsectionsbundle:6-7`. Before this change they remained selectable on a
7.2 installation. The new admission uses exact static release metadata and
rejects those projections before filesystem or Mautic mutation.

## APT lifecycle boundary

The observed Alex plugin operation
`ops-authguard71-alex-9918963-20261117` already completed successfully and its
MCD terminal receipt reached MCC through `mcd_state_heartbeat`. The subsequent
MCC CLI `Job not found` was therefore a legacy MCC status-routing defect, not a
lost MCD plugin task; no duplicate operation was launched.

Separately, source review proved that the existing `apt-upgrade` command was
synchronous and had no durable MCD operation ID. MCD 1.6.105 adds a persisted
host queue and `mcd-apt-operation-result-v1` while retaining the existing
external/distro APT implementation. The job does not require a Mautic instance,
is not killed by the generic timeout, is not implicitly retried after partial
failure, and is exported in the ordinary MCC state heartbeat.

## Regression

- focused lifecycle and compatibility set: `45 passed`;
- full stable suite: `2214 passed, 57 skipped, 141 subtests passed`;
- `git diff --check`: pass.

Live rollout and acceptance evidence remain publication gates and are not
claimed by this source-review receipt.
