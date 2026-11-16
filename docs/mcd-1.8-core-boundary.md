# MCD 1.8 core and component boundary

MCD 1.8 changes the package boundary, not merely its distribution format. The
main `mcd-agent` package contains only the distro-neutral task scheduler core.
It does not contain implementations of Mautic, host, backup, plugin, reporting,
commercial or infrastructure operations.

## What remains in the main package

The core accepts typed tasks and owns only their lifecycle:

1. durable task storage, queues, lanes and claims;
2. generic priority and budget policy expressed by task metadata;
3. deduplication, idempotency, retry and terminal state;
4. process launch, supervision, restart reconciliation and re-adoption;
5. structured progress, real-activity liveness and watchdog decisions;
6. receipts, evidence and task status readback;
7. discovery and validation of installed task-provider contracts.

Component names, operation names and payloads are opaque identifiers/data to
the core. The core must not branch on `campaign`, `segment`, `backup`, `nginx`,
`plugin`, `Mautic`, Debian, Red Hat or another product/OS concept.

Priority, superpriority, dedicated lanes and budget exemption remain available
as generic scheduler mechanics. Which campaign, segment or other domain object
is converted into such a task is decided by an external component. Therefore
the 1.8 core preserves proven scheduling guarantees without retaining domain
algorithms in the main package.

## What moves outside the main package

Every domain implementation is an independently versioned component or plugin,
including:

- Mautic command planning and execution;
- campaign, segment, import and periodic-job discovery/planning;
- installation, upgrade, migration, backup and restore;
- Mautic plugin inventory and plugin operations;
- Nginx, PHP, database, mail, security and other host-service operations;
- reports, diagnostics and commercial/control-plane adapters;
- MCC and Portal transports beyond the neutral task-ingress/status interfaces;
- native package-manager integration and MCD update orchestration.

An external provider receives one versioned task envelope, reports structured
progress and returns one terminal receipt. It does not create another scheduler,
task store, retry engine or receipt authority. The main package remains the sole
task-lifecycle engine.

## Distro-specific providers

Executable providers are built and accepted separately for each supported
distro family. Debian-family hosts install DEB providers from the APT repository
on `repo.sales-snap.com`; Red Hat-family hosts install RPM providers from the
corresponding repository on the same host. Other distributions follow the same
model through their native repository format.

There is no implicit fallback from Debian to Red Hat, from Red Hat to Debian, or
from an unknown distro to a generic executable provider. A provider declares its
task-contract version, component identity, implementation version and exact
distro family. Missing, duplicate or incompatible providers fail before task
mutation. Common source code may be shared internally, but installable artifacts,
dependencies and acceptance evidence remain family-specific.

Hardware/workload profile values may remain universal data. Their OS-specific
application belongs to the selected family provider; the scheduler core neither
calculates provider formulas nor knows paths, owners, service names or config
layouts.

## Compatibility and cutover

This contract does not authorize deleting behavior from the maintained 1.7
line. Extraction is performed operation by operation behind the existing task
lifecycle, with result/receipt parity and restart-recovery regression coverage.
The old in-package implementation is removed only after its external provider
passes direct and MCD-driven execution for every supported family.

MCD 1.8 may be declared only when a built main-package artifact audit proves
that it contains the scheduler core and contracts only, and contains no domain
or distro implementation. The machine-readable source of this boundary is
`etc/mcd-1.8-package-boundary.toml`.
