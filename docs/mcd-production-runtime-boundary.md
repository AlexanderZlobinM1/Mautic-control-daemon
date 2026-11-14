# MCD production runtime boundary

Production MCD is an installed wheel in `/opt/mcd/venv` and is launched from
`WorkingDirectory=/opt/mcd` with the managed venv Python. `/opt/mcd/src` and
any source checkout are forbidden production dependencies: they must not be a
systemd working directory, `PYTHONPATH` entry, import origin, update source or
repair target. A repair must never recreate or symlink that path.

The runtime boundary is fail-closed. Installed-wheel import origin, systemd
unit readback, scheduler status and worker re-adoption evidence are recorded
separately. A scheduler status probe is not completion evidence; Operations
must provide a bounded pilot cycle before fleet promotion. Configuration,
state, queues and Mautic worker processes are outside the unit repair mutation
boundary.
