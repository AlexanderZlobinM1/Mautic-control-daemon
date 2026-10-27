#!/bin/sh
set -eu
# Synthetic adapter fixture only; this is not a release scenario.
# MCD runs the signed artifact; MCC only pins and orchestrates it.
context_bytes=$(cat /proc/self/fd/3)
[ -n "$context_bytes" ]
step() { :; }
backup_create() { step backup_create; }
permissions() { step permissions; }
json_repair() { step json_repair; }
system_upgrade() { step system_upgrade; }
source_install() { step source_install; }
cache_prepare() { step cache_prepare; }
patch_post_source_install() { step patch_post_source_install; }
patch_before_cache_warmup() { step patch_before_cache_warmup; }
patch_before_asset_generation() { step patch_before_asset_generation; }
patch_preflight_frontend_assets() { step patch_preflight_frontend_assets; }
script_post_autoload_dump() { step script_post_autoload_dump; }
script_post_update_cmd() { step script_post_update_cmd; }
cache_warmup() { step cache_warmup; }
patch_before_doctrine_migrations() { step patch_before_doctrine_migrations; }
doctrine_migrations() { step doctrine_migrations; }
assetmapper_verify() { step assetmapper_verify; }
sender_dependencies() { step sender_dependencies; }
amazon_dependencies() { step amazon_dependencies; }
locale_dependencies() { step locale_dependencies; }
post_verify() { step post_verify; }
backup_create; permissions; json_repair; system_upgrade; source_install; cache_prepare
patch_post_source_install; patch_before_cache_warmup; patch_before_asset_generation
patch_preflight_frontend_assets; script_post_autoload_dump; script_post_update_cmd; cache_warmup
patch_before_doctrine_migrations; doctrine_migrations; assetmapper_verify; sender_dependencies
amazon_dependencies; locale_dependencies; post_verify
result='{"contract_version":"1.0","exit_code":0,"frame_type":"result","schema":"mcd-self-hosted-executable-output-v1","status":"succeeded","completed":20,"total":20,"rollback":"not_attempted"}'
printf '\000\000\000\173%s' "$result"
