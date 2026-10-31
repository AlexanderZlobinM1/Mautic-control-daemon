# External agent bootstrap boundary

The fresh agent bootstrap recipe belongs to the shared external source:
`scenarios/debian-family/common/blocks/mcd-bootstrap/1.0.0/passive_install.py`.
It is not packaged as an installation recipe or configuration/unit seed in MCD.

The initial shared export is byte-identical to the already published artifact:
SHA-256 `514e030f5aed122557c577bfd5b994ab78dce120495993cc730bec3bbc82ee0c`,
13,506 bytes. This source move changes no historical artifact, registration,
configuration, receipt or completed-replay semantics.

The publisher obtains the approved external export and checks its SHA and size
before copying the existing `mcd-runtime-<version>.passive-install.py` sidecar.
Catalog `passive_bootstrap` metadata and Portal `DnsBootstrapSpec` retain
separate bootstrap, runtime archive, bridge and manifest pins. Missing or
mismatched external bytes fail publication; there is no embedded recipe fallback.

Recipe tests and isolated systemd/bootstrap QA move with the external component.
MCD continues to provide generic HTTPS transport, verified runtime activation,
registration import and receipts. The runtime entrypoint does not run pip or
install dependencies after an import failure. External bootstrap prepares its
runtime prerequisites before starting the agent.

This boundary does not claim that all other embedded installation recipes have
already been removed. That broader migration requires verified external block
replacements and a separate source audit.
