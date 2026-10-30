# Passive commercial bootstrap: Ubuntu 24.04 amd64

Candidate entrypoint: `tools/mcd_passive_install.py`. This is a fresh-install
entrypoint, not an upgrade. Existing installations are refused unless an exact
completed passive receipt, unchanged config/unit, matching installed version,
durable agent/installation binding, key and request/instance identity prove replay.
Completed replay performs no new enrollment; partial installs remain refused.
Provide the existing account commercial key on protected stdin before enrollment.
Pass the portable instance DNS through `--instance-identity`; it is distinct from
the machine hostname. New enrollment sends `identity_kind="dns"` and the plain
DNS identity, with no computed host fingerprint. Stable `--request-id` supplies
the two Portal idempotency keys. `--bridge-sha256` is verified before importing
the bridge, rather than relying on an unchecked caller assertion.
Never put the key in command arguments, script text or receipts.

The installer verifies the HTTPS runtime archive SHA-256, validates the runtime
manifest/payload and expected version with the separately SHA-verified MCD bridge,
installs the wheel in a dedicated venv, redeems the same key, uses host registration
and atomically imports issued binding through `commercial_registration`.
It writes passive profile and SQLite state; it does not start or enable a worker.
After daemon-reload it reads actual systemd ActiveState and requires inactive.
The unit uses KillMode=process so later supervised worker restarts do not kill
detached jobs. Enrollment does not authorize or execute any scenario.
Failures preserve partial install for diagnosis, so partial retry requires explicit recovery.
No activation code, agent keypair or signature infrastructure is used.

Prerequisites: root, Python 3.12, python3-venv/pip, trusted HTTPS CA, systemd.
Portal must inject the already existing account key before bootstrap, and supply
immutable runtime URI, SHA-256, manifest SHA-256, expected version and request ID.
The bridge script itself must be separately pinned by SHA-256 before invocation.

## Scenario package profile

Passive enrollment itself requires no PHP, web server, cache or database server.
It must not provision Mautic packages as an enrollment side effect.
SQLite state does not change existing MariaDB databases or credentials.

The sole approved scenario target is Ubuntu 24.04, Nginx from nginx.org,
PHP 8.5 from the approved Ondrej channel, MariaDB 11.4 from the official MariaDB
channel, latest Mautic 7.2.x, and Redis-backed PHP/Mautic sessions. Channels must
already be configured; neither bootstrap nor a fallback silently adds a repository.
The external Portal-owned scenario verifies candidates and reports a typed blocker
when the approved package family is unavailable. Native Noble 8.3/10.11 is not
an alternative installation profile.

The accepted CLI and FPM Redis configuration uses php8.5-redis,
session.save_handler=redis and session.save_path="tcp://127.0.0.1:6379?database=10&prefix=mautic_session:&timeout=2.5&read_timeout=2.5".
Locking is enabled with lock_expire=300, lock_retries=-1, lock_wait_time=10000;
session.gc_maxlifetime=86400. This policy belongs to the external scenario and
requires real read/write and concurrent-lock QA against a single Redis master.

## QA limits

`tools/passive_install_qa.py` uses a local trusted HTTPS Portal contract double
inside Ubuntu 24.04 amd64. It checks same-key enrollment/host registration,
passive receipt, no installer worker start, exact completed replay and wrong-key
rejection. By default daemon-reload is stubbed. With `REAL_SYSTEMD_QA=1` in a
disposable VM, it uses real systemd and separately checks service start/restart/
stop with an empty command queue and child-process survival. These fixture tests
do not prove live Portal behavior; real endpoint acceptance needs separate evidence.
Do not label a candidate approved for production until those checks are supplied.
