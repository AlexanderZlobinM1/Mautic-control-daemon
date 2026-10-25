# Bootstrap PRECHECK scenario v1

This installer/Portal contract applies before MCD exists on a self-hosted
machine. It performs no installation, provisioning, ordering, persistence, or
secret storage.

The signed scenario is `mcc-bootstrap-precheck-scenario-v1` with
`ttl_seconds <= 900`. Checks are limited to SSH authentication, root/sudo,
host fingerprint, Ubuntu support, amd64/arm64, CPU/RAM/disk,
ports/listeners, package manager/repositories/egress, time sync, existing
stack/MCD classification, and exact DNS-to-server proof.

DNS proof uses a nonce and temporary responder. The responder must be removed
before the receipt is accepted. The receipt is
`mcd-bootstrap-precheck-receipt-v1`, binds to input and host fingerprints,
contains only `PASS|WARN|FAIL` codes with remediation, has an expiry, and
proves cleanup. Passwords, keys, credentials, and tokens are forbidden in
results. A failed or expired receipt cannot authorize installation.
