# MCD production runtime boundary

Production MCD is an installed wheel in `/opt/mcd/venv` and is launched from
`WorkingDirectory=/opt/mcd` with the managed venv Python. `/opt/mcd/src` and
any source checkout are forbidden production dependencies: they must not be a
systemd working directory, `PYTHONPATH` entry, import origin, update source or
repair target. A repair must never recreate or symlink that path.

The 1.6.41 release gate fails closed unless an installed-wheel probe, systemd
unit readback, scheduler admission/status, and worker re-adoption evidence are
captured. Configuration, state, queues and Mautic worker processes are outside
the unit repair mutation boundary.
