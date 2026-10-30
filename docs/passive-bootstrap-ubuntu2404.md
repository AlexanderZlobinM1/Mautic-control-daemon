# Passive commercial bootstrap: Ubuntu 24.04 amd64

Candidate entrypoint: `tools/mcd_passive_install.py`. This is a fresh-install
entrypoint, not an upgrade. Existing `/opt/mcd` or either MCD unit is refused.
Provide the existing account commercial key on protected stdin before enrollment.
Never put the key in command arguments, script text or receipts.

The installer verifies the HTTPS runtime archive SHA-256, validates the runtime
manifest/payload and expected version with the separately SHA-verified MCD bridge,
installs the wheel in a dedicated venv, redeems the same key, uses host registration
and atomically imports issued binding through `commercial_registration`.
It writes passive profile and SQLite state; it does not start or enable a worker.
Failures preserve partial install for diagnosis, so retry requires explicit recovery.
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
passive receipt, no worker start and refusal of a repeated fresh install.
The container does not run systemd PID1: daemon-reload is stubbed and any service
start is rejected. This does not prove live Portal or real systemd behavior.
Do not label a candidate approved for production until those checks are supplied.
