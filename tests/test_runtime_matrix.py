from __future__ import annotations

from mcd_agent.runtime_matrix import (
    CONTACT_FIELD_METADATA_OPERATION,
    MAUTIC_PATCH_PREFLIGHT_OPERATION,
    MAUTIC_PATCH_PLAN_V2_CAPABILITY,
    MAUTIC_PATCH_PREFLIGHT_V2_OPERATION,
    SEGMENT_LAZY_UPDATE_CAPABILITY,
    SEGMENT_LAZY_UPDATE_INSTANCE_CAPABILITY,
    build_runtime_profile,
)


def test_four_runtime_layout_combinations_have_stable_distinct_styles() -> None:
    profiles = [
        build_runtime_profile(runtime=runtime, install_type=install_type)
        for runtime in ("host", "docker")
        for install_type in ("zip", "composer")
    ]
    assert {profile.style_token for profile in profiles} == {
        "host-zip",
        "host-composer",
        "docker-zip",
        "docker-composer",
    }


def test_host_zip_supports_composer_move_but_host_composer_does_not() -> None:
    zipped = build_runtime_profile(runtime="host", install_type="zip")
    composer = build_runtime_profile(runtime="host", install_type="composer")
    assert zipped.allows("composer-move")
    assert not composer.allows("composer-move")
    assert zipped.allows("core-upgrade")
    assert composer.allows("core-upgrade")
    assert zipped.allows(MAUTIC_PATCH_PREFLIGHT_OPERATION)
    assert composer.allows(MAUTIC_PATCH_PREFLIGHT_OPERATION)
    assert zipped.allows(MAUTIC_PATCH_PLAN_V2_CAPABILITY)
    assert zipped.allows(MAUTIC_PATCH_PREFLIGHT_V2_OPERATION)
    assert composer.allows(MAUTIC_PATCH_PLAN_V2_CAPABILITY)
    assert composer.allows(MAUTIC_PATCH_PREFLIGHT_V2_OPERATION)
    assert zipped.allows("backup")
    assert composer.allows("backup")
    assert zipped.allows(CONTACT_FIELD_METADATA_OPERATION)
    assert CONTACT_FIELD_METADATA_OPERATION in zipped.capabilities
    assert SEGMENT_LAZY_UPDATE_CAPABILITY in zipped.capabilities
    assert SEGMENT_LAZY_UPDATE_INSTANCE_CAPABILITY in zipped.capabilities


def test_contact_field_metadata_is_advertised_for_every_database_runtime() -> None:
    for runtime, install_type in (("host", "zip"), ("host", "composer"), ("docker", "composer")):
        profile = build_runtime_profile(
            runtime=runtime,
            install_type=install_type,
            capabilities=["database"],
        )
        assert profile.allows(CONTACT_FIELD_METADATA_OPERATION)
        assert CONTACT_FIELD_METADATA_OPERATION in profile.capabilities

    without_database = build_runtime_profile(
        runtime="docker", install_type="composer", capabilities=["console"]
    )
    assert not without_database.allows(CONTACT_FIELD_METADATA_OPERATION)
    assert CONTACT_FIELD_METADATA_OPERATION not in without_database.capabilities


def test_host_explicit_runtime_advertises_atomic_patch_preflight() -> None:
    declared = ["console", "database", "filesystem", "plugin-read", "plugin-write"]
    for install_type in ("zip", "composer"):
        profile = build_runtime_profile(
            runtime="host", install_type=install_type, capabilities=declared
        )
        assert profile.allows(MAUTIC_PATCH_PREFLIGHT_OPERATION)
        assert MAUTIC_PATCH_PREFLIGHT_OPERATION in profile.capabilities
        assert MAUTIC_PATCH_PLAN_V2_CAPABILITY in profile.capabilities
        assert MAUTIC_PATCH_PREFLIGHT_V2_OPERATION in profile.capabilities
        assert not profile.allows("core-upgrade")

    missing_filesystem = build_runtime_profile(
        runtime="host", install_type="composer", capabilities=["console", "database"]
    )
    assert not missing_filesystem.allows(MAUTIC_PATCH_PREFLIGHT_OPERATION)
    assert MAUTIC_PATCH_PREFLIGHT_OPERATION not in missing_filesystem.capabilities
    assert MAUTIC_PATCH_PLAN_V2_CAPABILITY not in missing_filesystem.capabilities
    assert MAUTIC_PATCH_PREFLIGHT_V2_OPERATION not in missing_filesystem.capabilities


def test_v2_capabilities_are_not_advertised_without_the_executor(monkeypatch) -> None:
    monkeypatch.setattr("mcd_agent.runtime_matrix._patch_v2_executor_available", lambda: False)
    profile = build_runtime_profile(runtime="host", install_type="zip")
    assert profile.allows(MAUTIC_PATCH_PREFLIGHT_OPERATION)
    assert not profile.allows(MAUTIC_PATCH_PLAN_V2_CAPABILITY)
    assert not profile.allows(MAUTIC_PATCH_PREFLIGHT_V2_OPERATION)
    assert MAUTIC_PATCH_PLAN_V2_CAPABILITY not in profile.capabilities
    assert MAUTIC_PATCH_PREFLIGHT_V2_OPERATION not in profile.capabilities


def test_docker_composer_uses_image_upgrade_and_explicit_plugin_capability() -> None:
    profile = build_runtime_profile(
        runtime="docker",
        install_type="composer",
        capabilities=[
            "console",
            "database",
            "plugin-read",
            "plugin-write",
            "bulk-operations",
            "image-managed-upgrade",
        ],
    )
    assert profile.allows("console-jobs")
    assert profile.allows("bulk-operations")
    assert profile.allows("plugin-install")
    assert profile.allows("image-sync")
    assert not profile.allows("core-upgrade")
    assert not profile.allows(MAUTIC_PATCH_PREFLIGHT_OPERATION)
    assert not profile.allows("composer-move")
    assert not profile.allows("runtime-package-mutation")


def test_unknown_layout_fails_closed_for_mutating_operations() -> None:
    profile = build_runtime_profile(
        runtime="docker",
        install_type="unknown",
        capabilities=["console", "plugin-write", "migration-source"],
    )
    assert not profile.supported
    assert profile.style_token == "unsupported"
    assert not profile.allows("plugin-install")
    assert not profile.allows("migration-source")


def test_docker_migration_requires_explicit_adapter_capability() -> None:
    profile = build_runtime_profile(
        runtime="docker",
        install_type="composer",
        capabilities=["database", "migration-adapter", "migration-source"],
    )
    assert profile.allows("migration-source")
    without_adapter = build_runtime_profile(
        runtime="docker",
        install_type="composer",
        capabilities=["database", "migration-source"],
    )
    assert not without_adapter.allows("migration-source")
