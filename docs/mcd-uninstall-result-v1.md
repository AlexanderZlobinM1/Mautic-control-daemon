# MCD native uninstall result v1

For a reachable host with an installed MCD executable, MCC invokes:

```text
mcd-cli uninstall --yes --machine-result --operation-id <operation-id>
```

The command prints exactly one terminal line:

```text
MCD_UNINSTALL_RESULT={...}
```

The JSON uses schema `mcd-uninstall-result-v1`. `ok=true` is valid only after
owned cron marker pairs are restored and read back, the exact MCD service is
stopped/disabled, verified MCD artifacts are absent, and any requested purge is
complete. `outcome` is `removed`, `disabled`, or idempotent `already_absent`.

Every failure is terminal with `ok=false`, `outcome=failed`, `error_code` and
`error`. The receipt also contains per-user cron status, completed system steps,
removed and preserved paths, MCD version, operation ID, and `receipt_sha256`.
The digest is SHA-256 over canonical compact sorted JSON before the digest field
is added.

MCC must not duplicate cron restoration or host filesystem removal. If the host
is unreachable or the MCD executable is already absent before execution, MCC
may perform its separate inventory-only forget path without waiting for SSH or
an agent acknowledgement. A timeout after native execution started is not that
case and must remain unresolved/failed until its terminal receipt is reconciled.
