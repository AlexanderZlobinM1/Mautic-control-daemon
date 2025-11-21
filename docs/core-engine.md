# Core Engine – Technical Description (v0.1)

## Overview

The current core engine is a minimal Python 3 daemon which:

- discovers one or more Mautic installations on the server by locating their `local.php` files,
- reads database credentials directly from each Mautic `local.php` (no extra config entities),
- detects the path to each instance's `bin/console`,
- provides a process manager which can launch the following Mautic CLI commands:
  - `mautic:segment:update <id>`
  - `mautic:campaigns:rebuild <id>`
  - `mautic:campaigns:trigger <id>`
  - `mautic:import`
- runs in a loop and is ready to host the scheduling logic based on database state.

The priority/queue logic based on database queries is **not implemented yet** – only the skeleton is in place.

## Language and runtime

- Language: **Python 3**
- External dependencies (see `daemon/requirements.txt`):
  - `PyYAML` – reading daemon configuration from `config/daemon.yml`
  - `PyMySQL` – connecting to Mautic databases

## File structure (implemented part)

- `config/daemon.yml` – main configuration file for the daemon.
- `daemon/`
  - `__init__.py` – package marker.
  - `main.py` – entrypoint, runs the daemon.
  - `core/`
    - `__init__.py` – core package marker.
    - `config.py` – loads and merges daemon configuration.
    - `instances.py` – discovers Mautic instances and reads DB config from `local.php`.
    - `executor.py` – process manager for Mautic CLI commands.
    - `daemon.py` – main loop and per-instance processing skeleton.
- `daemon/requirements.txt` – Python dependencies.

## Configuration: config/daemon.yml

Current options:

```yaml
php_binary: /usr/bin/php    # path to PHP CLI binary

search_paths:               # directories to scan recursively for Mautic installs
  - /var/www

loop_interval_seconds: 10   # delay between daemon iterations

segment:
  max_parallel_updates: 2   # max concurrent segment:update per instance
  stale_after_minutes: 30   # reserved for future "stale segment" logic

log_level: INFO             # Python logging level
