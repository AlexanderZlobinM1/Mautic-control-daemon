# Passive commercial bootstrap: Ubuntu 24.04 amd64

Candidate entrypoint: `tools/mcd_passive_install.py`. This is a fresh-install
entrypoint, not an upgrade. Existing installations are refused unless an exact
completed passive receipt, unchanged config/unit, matching installed version,
durable agent/installation binding, key and request/instance identity prove replay.
Completed replay performs no new enrollment; partial installs remain refused.
Provide the existing account commercial key on protected stdin before enrollment.
Pass the portable instance DNS through `--instance-identity`; it is distinct from
the machine hostname used for host fingerprinting. Stable `--request-id` supplies
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

For a separately authorized Ubuntu Noble base-package stage, the distribution
repositories expose PHP 8.3 (`php-fpm` candidate `2:8.3+93ubuntu2`) and MariaDB
10.11 (`mariadb-server` candidate `1:10.11.14-0ubuntu0.24.04.1` in QA).
Use nginx, redis-server, mariadb-server, php8.3-fpm/php8.3-cli and only the PHP
extensions required by the selected Mautic artifact. PHP 8.4/8.5 is not supplied
by this profile. Mautic version compatibility still requires the selected
artifact's own supported-version evidence; distribution availability alone is
not proof of application compatibility.

## QA limits

`tools/passive_install_qa.py` uses a local trusted HTTPS Portal contract double
inside Ubuntu 24.04 amd64. It checks same-key enrollment/host registration,
passive receipt, no installer worker start, exact completed replay and wrong-key
rejection. By default daemon-reload is stubbed. With `REAL_SYSTEMD_QA=1` in a
disposable VM, it uses real systemd and separately checks service start/restart/
stop with an empty command queue and child-process survival. These fixture tests
do not prove live Portal behavior; real endpoint acceptance needs separate evidence.
Do not label a candidate approved for production until those checks are supplied.
