# Commercial self-host flow gap analysis

Compared candidate: MCD `1.5.200`, source `2d0030176f6633350fdcbcc6446ffdec6f9403b5`.

## Covered by MCD

- one-time Portal activation redeem, host-bound agent key and passive installation;
- Portal-only outbound job/artifact transport;
- Portal Ed25519 and MCC publisher signature verification;
- exact digest/size/contract/capability/platform checks;
- fixed typed onboarding registry with no arbitrary shell or SQL;
- checkpoint/idempotency/replay, bounded rollback handlers and signed receipts;
- usage observations with HMAC/idempotency;
- durable cursor/pending receipt and outage/restart recovery;
- commercial worker isolation from scheduler admission;
- corporate MCC↔MCD and standalone paths unchanged.

## Explicitly outside MCD

- Portal Add Installation UI, SSH host/user/password/key/DNS collection;
- ephemeral SSH credential handling and installer bootstrap transport;
- OS, PHP, database, Redis, web server, Certbot, Composer and plugin
  provisioning policy;
- subscription, entitlement and active-installation limits;
- Portal server-to-server MCC projection/intents.

The installer may install and activate MCD once. After that exchange, MCD is
outbound-only to Portal and remains passive by default. Provisioning policy is
carried by the signed versioned scenario, never embedded in the daemon.

## Minimal additive closure

MCD now validates the onboarding artifact security boundary, required forbidden
primitives, declared stage/terminal receipt schemas, and replay policy before
executing any stage. A mismatch fails closed before acknowledgement/execution.

No production release or installation is authorized by this document.
