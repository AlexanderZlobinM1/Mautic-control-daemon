# External detector/scenario storage lifecycle

MCD does not contain detector definitions, OS matrices, scenario JSON, shell,
Ansible, or provisioning seeds.

1. MCC superadmin publication creates an immutable versioned artifact in the
   MCC artifact/object store and catalog metadata in the catalog database.
2. MCC signs the canonical artifact statement and records revision, digest,
   size, compatibility, detector requirements and capability requirements.
3. Portal imports only the read-only catalog projection and verified artifact
   metadata. Portal owns entitlement, subscription and installation binding.
4. Portal wraps the exact artifact revision/digest in a signed job and serves
   bytes through a short-lived installation-scoped grant.
5. MCD reads normalized local facts after installation, evaluates the external
   detector rules, verifies digest/signature/revision and requirements, then
   invokes only its bounded primitive registry.
6. MCD persists only bounded execution checkpoints/receipts and opaque
   references; it never seeds or rewrites scenario definitions.
7. Revisions are immutable. A changed detector/scenario requires a new
   published revision and signature; mismatch, unsupported or ambiguous facts
   fail closed before mutation.

Runtime proof for this candidate: `mcd_agent/` contains no onboarding artifact,
Ubuntu matrix, playbook or scenario JSON. The only scenario files used by tests
are external test fixtures; no startup path generates initial scenarios.
