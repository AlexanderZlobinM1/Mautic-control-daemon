# Plugin Cache Reset v1

MCD agent 1.2.8 exposes capability and evidence schema
`mcd-plugin-cache-reset-v1` for plugin filesystem operations.

Before plugin files are removed, installed or replaced, MCD validates the
effective `[plugins].cache_relative_paths` list together with the external
filesystem owner/group/mode contract. Existing configs without a `[plugins]`
section receive the release defaults for the supported Mautic 6/7 component:
`var/cache/prod`, the Mautic runtime user for owner/group, and mode `0775`.
Explicit MCC/operator values remain authoritative. The paths are strict
relative paths under the selected instance managed root; MCD never accepts a
host-specific path from a one-off operation command. An explicitly missing or
invalid effective list stops the operation before any plugin, registration or
dependency mutation and reports `files_applied=false`.

After that preflight and before the first Symfony console boot, the selected
external filesystem component atomically detaches every declared active cache
directory and immediately recreates it with the declared instance runtime
owner/group/mode. The detached cache is disposable and is deleted best-effort;
concurrent Symfony writes during that deletion cannot fail the plugin job. The
normal cache clear and native plugin reload then generate a clean cache from
the current plugin filesystem.

The cache is disposable runtime output and is never snapshotted or restored.
If cache clear or plugin reload fails, the stale compiled generation remains
deleted so a later plugin job or web request cannot inherit references to code
that is no longer present. Ordinary `remove` retains plugin integration settings;
`purge` remains the separately selected destructive settings operation.

Every reset emits one line:

```text
MCD_PLUGIN_CACHE_RESET_EVIDENCE=<JSON>
```

The object contains `schema`, `operation`, `root`, `status`, `reset_paths`,
`cleanup_pending_paths`, and `mode`. A pending path is disposable cache garbage,
not a snapshot or recovery source.
