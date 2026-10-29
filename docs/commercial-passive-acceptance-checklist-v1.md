# MCD 1.5.200 passive commercial acceptance checklist

This checklist is for exactly one staged RFP host and one disposable/shadow
installation. It is not authorization for fleet rollout, tenant migration,
customer mail, scheduled Mautic work, or default7/image changes.

Candidate: commit `2d0030176f6633350fdcbcc6446ffdec6f9403b5`, MCD `1.5.200`.
The immutable runtime artifact and receipt are the paths and hashes recorded
in the Operations handoff. Do not copy source checkout files to the host.

## 1. Read-only baseline before activation

Record, without secrets:

```sh
hostname -f
uname -m
systemctl is-active mcd-agent
systemctl is-active cron || true
crontab -l 2>/dev/null || true
sudo -u www-data crontab -l 2>/dev/null || true
```

Record the Mautic instance UID/root, current MCD version, native cron lines,
running Mautic task summary, and current tenant mail/task counters. Do not
edit cron or tenant data. Save a SHA-256 of the redacted baseline evidence.

## 2. Portal activation and passive registration

Portal creates one activation code through the live Portal UI/API with:

- `max_uses=1`;
- `expires_in_seconds=900`;
- one account-scoped code, never pasted into chat or logs.

The operator runs the normal MCD-owned CLI path with the code supplied only as
the process argument/input at execution time:

```sh
mcd-cli commercial --config "$MCD_CONFIG" activate \
  --portal-url https://portal.sales-snap.com \
  --activation-code "$PORTAL_ACTIVATION_CODE" \
  --json
```

Expected activation evidence is redacted JSON containing:
`schema=portal-mcd-activation-response-v1`, `contract_version=1.0`, one
`agent_id`, and credential expiry. No Portal job signing key is required or
persisted. Never print or
persist the raw activation code or credential in evidence.

Register the one disposable installation in passive state:

```sh
mcd-cli commercial --config "$MCD_CONFIG" register \
  --local-identity "$SHADOW_LOCAL_IDENTITY" \
  --display-name "RFP shadow" \
  --mautic-version "$MAUTIC_VERSION" \
  --application-root-fingerprint "$APP_ROOT_FINGERPRINT" \
  --activation-state passive --json
```

Expected evidence: one `installation_id`, `activation_state=passive`, bound to
the same `agent_id`; no active installation slot consumed.

## 3. Local safety gates

The base MCD profile may remain `passive`, `standalone`, or `corporate`; durable
commercial enrollment independently enables the Portal worker. The worker
must report `mcd-commercial-worker-status-v1` with `status=enabled` and no
secret fields.
The following must remain unchanged throughout acceptance:

- native cron entries and cron ownership;
- Mautic scheduled-job state and task counters;
- tenant data, contacts, mail queues and message queues;
- corporate MCC configuration and credentials;
- standalone configuration and behavior.

Any missing Portal URL, credential/host binding, or agent/install identity is a
typed `disabled` status and blocks the outbound worker. Portal polling,
adoption, inventory, settings, noop, receipt delivery and job admission use
only the commercial API key over HTTPS. Downloaded artifacts are accepted only
when size and SHA-256 match the immutable pin/catalog entry; no Portal or
publisher signing key is required.

## 4. Portal-only no-op job and receipt

Portal delivers one harmless bounded scenario through
`portal-mcd-job-delivery-v1`. The package must contain:

- exact Portal artifact digest and immutable revision;
- applicable MCD contract/capabilities;
- `package.download.path` and short-lived `grant`;
- installation and agent scope matching the registered identities.

Expected sequence:

1. outbound HTTPS poll only to `https://portal.sales-snap.com`;
2. exact artifact download through Portal grant;
3. API-key-authenticated response validation and SHA-256 artifact verification;
4. passive/no-op typed stages only;
5. `mcd-onboarding-stage-receipt-v1` stage receipts;
6. one `mcd-onboarding-terminal-receipt-v1`.

No direct MCC connection, MCC credential, cron mutation, scheduled Mautic work,
or tenant mutation is permitted. Capture destination/request metadata without
capturing Authorization values.

## 5. Usage round-trip

Send only the two permitted raw observations for the shadow installation:

- `contact_count_current`;
- `confirmed_successful_sends_cumulative`.

Each sample must have stable `sample_id`/sequence, UTC `observed_at`, external
`cadence_policy_seconds`, non-empty `evidence_ref`, and
`Idempotency-Key=sample_id`. The request uses
`X-Portal-Usage-Signature=hmac-sha256=<base64url>` over exact JSON bytes with
the bound agent credential. Replaying identical bytes must return the original
ack; changed bytes with the same sample ID must be rejected. Queued, attempted,
or failed sends are never reported as successful sends.

## 6. Restart, outage, and activation replay

Before restart, record the Portal cursor and any pending terminal receipt from
the protected commercial state store. Restart the MCD service through the
normal MCD operation path. Expected result: the same cursor/pending receipt is
reloaded, completed stages are not repeated, and a pending receipt retries with
the same idempotency key.

During a bounded Portal outage, the commercial worker may report a typed
retry/failure status but must not stop the scheduler or alter corporate/
standalone work. Restore Portal and verify LKG/retry recovery.

Redeeming the same activation code again must fail because `max_uses=1`.
After TTL expiry it must fail as expired. Do not retry activation against a
different host or installation.

## 7. Rollback and acceptance evidence

If acceptance fails, stop only the passive commercial worker, preserve its
redacted status/receipt evidence, and use the standard MCD runtime-package
rollback to the prior installed candidate. Do not remove tenant data, edit
cron, or alter Mautic application state. The rollback receipt must bind the
prior package/source ref and archive SHA.

Operations returns one evidence bundle containing:

- redacted baseline and post-check hashes;
- agent/installation IDs and passive state;
- worker status and Portal-only destination evidence;
- job, stage, terminal and usage acknowledgement metadata;
- restart/outage/retry and activation replay results;
- cron/Mautic/tenant invariants;
- corporate and standalone regression results.

Acceptance is PASS only when all invariants hold. This checklist does not
authorize publication or fleet rollout.
