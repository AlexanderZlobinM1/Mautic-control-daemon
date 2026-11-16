import json
from types import SimpleNamespace

import pytest

from mcd_agent.daemon import (
    _campaign_super_priority_effective_setting,
    _segment_super_priority_effective_setting,
    _exclude_campaign_super_priority_ids,
)
from mcd_agent.runtime_overrides import merge_instance_desired_states, runtime_key_observation


@pytest.mark.parametrize("campaigns", [[78, 82], []])
def test_campaign_list_roundtrips_without_changing_other_instances_or_segments(campaigns):
    key = "campaign_super_priority_instance_settings"
    first, second = "first.example", "second.example"
    baseline = {key: {first: [82], second: [21]},
                "segment_super_priority_instance_settings": {first: [93, 94]}}
    desired = json.loads(json.dumps({
        first: {"revision": 8, "runtime_overrides": {
            key: campaigns, "segment_super_priority_instance_settings": [93, 94]}}}))
    merged = merge_instance_desired_states(baseline, desired)
    cfg = SimpleNamespace(profile_name="custom", disable_whitelist=False,
                          campaign_super_priority=[], segment_super_priority=[], **merged)
    inst = SimpleNamespace(instance_uid=first, root="/srv/first", name=first,
                           primary_domain=first, domains=[])
    other = SimpleNamespace(instance_uid=second, root="/srv/second", name=second,
                            primary_domain=second, domains=[])
    assert _campaign_super_priority_effective_setting(cfg, inst) == set(campaigns)
    assert _campaign_super_priority_effective_setting(cfg, other) == {21}
    assert _segment_super_priority_effective_setting(cfg, inst) == {93, 94}
    observed = runtime_key_observation(cfg, {key: campaigns}, revision=8,
                                       source="mcc", scope_key=first)
    assert observed["keys"][key]["status"] == "unchanged"
    assert observed["keys"][key]["effective"] == campaigns
    ordinary = _exclude_campaign_super_priority_ids([78, 82, 90], set(campaigns))
    assert set(ordinary) == {78, 82, 90} - set(campaigns)
    if not campaigns:
        assert 78 in ordinary
        assert 82 in ordinary


def test_authoritative_row_without_campaign_field_does_not_invent_campaigns():
    key = "campaign_super_priority_instance_settings"
    runtime = {key: {"first.example": [78]}}
    merged = merge_instance_desired_states(runtime, {
        "first.example": {"revision": 9, "runtime_overrides": {
            "segment_super_priority_instance_settings": [93, 94]}}})
    assert key not in merged
