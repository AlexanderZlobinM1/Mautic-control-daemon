import math
import pytest
from mcd_agent.commercial_portal import canonical_standalone_settings, build_standalone_settings_snapshot, standalone_settings_etag


def test_snapshot_hashes_same_normalized_body_without_mutation():
    original = {"nested": [1.0, -2.0, -0.0, 0.25, False, None], "zero": 0}
    snapshot = build_standalone_settings_snapshot(original, revision=7, sync={})
    assert snapshot["values"] == {"nested": [1, -2, 0, 0.25, False, None], "zero": 0}
    assert type(snapshot["values"]["nested"][0]) is int
    assert type(original["nested"][0]) is float
    assert snapshot["etag"] == standalone_settings_etag(snapshot["values"])
    assert snapshot["revision"] == 7
    assert snapshot["values"]["nested"][4] is False


@pytest.mark.parametrize("number", [math.inf, -math.inf, math.nan])
def test_nonfinite_numbers_fail_closed(number):
    with pytest.raises(ValueError, match="invalid_number"):
        canonical_standalone_settings({"nested": [number]})
