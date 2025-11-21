# Mautic Control Daemon

Central daemon for managing background tasks and load of Mautic-based marketing instances.

Main goals:

- Orchestrate Mautic CLI tasks (segments, campaigns, email sending, etc.)
- Apply dynamic limits based on DB/CPU load
- Provide a unified configuration and monitoring entrypoint
- Allow adding new functional blocks without breaking existing ones

Detailed specification: see [`docs/TZ-daemon.md`](docs/TZ-daemon.md).
