import inspect

import pytest

from mcd_agent import mautic_upgrade as upgrade
from mcd_agent.mautic_patch_plan import contract
from test_mautic_manual_upgrade import invocation, wire_upgrade


@pytest.mark.parametrize("mode", ["zip", "composer"])
def test_normal_same_line_has_no_special_exclusion_admission(invocation, monkeypatch, mode):
    args, events = wire_upgrade(invocation, monkeypatch, mode)
    def unexpected(*args, **kwargs): raise AssertionError("global patch DB admission invoked")
    monkeypatch.setattr("mcd_agent.mautic_patch_fact_binding.bound_provider", unexpected)
    assert upgrade.run_upgrade_apply(**args, mcc_preflighted_single_instance=True) == 0
    expected = ["maintenance", "permissions", "install", "cleanup"]
    if mode == "zip":
        expected.insert(0, "stage")
    assert events == expected


def test_special_exclusion_public_surface_is_removed():
    parameters = inspect.signature(upgrade.run_upgrade_apply).parameters
    assert not any(name.startswith("patch_exclusion_guard") for name in parameters)
    assert "conditional_exclusion_guard_v1" not in contract()["features"]
