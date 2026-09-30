# MCD planning migration provenance

The MCD v2 planning set was moved from the archived monorepo into this
independent MCD tree on 2026-09-30.

- Source: `mcd-legacy-20260930/planning/v2/`
- Destination: `mcd/planning/v2/`
- Migrated files: five Markdown planning documents
- Excluded artifact: `planning/.DS_Store`
- Classification: MCD planning only; these documents are requirements and
  capability notes, not implementation instructions or release evidence.

Links that pointed at the former MCD monorepo documentation were updated to
the neutral shared-contracts tree or the independent MCD documentation map.
MCC source and customer/host data were not migrated.
