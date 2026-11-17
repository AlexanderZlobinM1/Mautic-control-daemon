# Plugin automatic-update policy v1

The plugin automatic-update opt-out is one bidirectional runtime setting. A
direct MCD command and an MCC request address the same exact tuple:

- stable bare instance UID;
- manifest `plugin_uid`;
- exact manifest `package_uid` projection.

The setting only excludes that package projection from automatic update or
remediation. It never installs, removes, changes, or selects a plugin, and it
does not block a later explicit manual install or update.

## Runtime setting

The shared runtime key is `plugin_auto_update_instance_settings`:

```toml
plugin_auto_update_instance_settings = { "www-1d94d5bb" = { "hostnet.auth.managed:6-7" = { plugin_uid = "hostnet.auth:6-7", auto_update_disabled = true } } }
```

`unset` persists `auto_update_disabled = false` instead of deleting the row.
This explicit tombstone allows the existing deep-merge runtime transport to
clear an older remote `true` value. Different package projections remain
independent even when they share one installed bundle directory.

There is no wildcard package identity in v1. A legacy host-wide
`all_plugins_disabled` switch must not be translated to `*`, silently deleted,
or treated as this exact per-package contract.

## CLI

All selectors and the caller operation ID are mandatory:

```text
mcd-cli plugin-auto-update-policy get   --config PATH --instance-uid UID --plugin-uid PLUGIN_UID --package-uid PACKAGE_UID --operation-id ID
mcd-cli plugin-auto-update-policy set   --config PATH --instance-uid UID --plugin-uid PLUGIN_UID --package-uid PACKAGE_UID --operation-id ID
mcd-cli plugin-auto-update-policy unset --config PATH --instance-uid UID --plugin-uid PLUGIN_UID --package-uid PACKAGE_UID --operation-id ID
```

The command emits exactly one JSON object with schema
`mcd-plugin-auto-update-policy-result-v1`. Its receipt binds the action,
operation ID, exact selectors, effective flag, idempotent-change result, local
readback and MCC synchronization result. If local persistence succeeds but the
MCC synchronization attempt fails, the result remains an error while reporting
the confirmed local value; a retry is safe.

For a registered agent, synchronization first reads the current MCC host and
instance revisions. MCD merges only the selected package projection into the
current bare-UID instance row and submits that row with both revisions. A
revision conflict triggers one bounded refetch, rebase, and retry. Other package
projections in the current MCC row are preserved. MCD reports success only when
that revisioned synchronization succeeds (or when MCC transport is explicitly
disabled for standalone operation).

MCC UI and MCC CLI must normalize through the same MCC operation/service and
invoke this one MCD contract. They must not implement separate selection,
validation, storage, or plugin-mutation paths.
