from __future__ import annotations

import json
from pathlib import Path

import pytest

from mcd_agent.commercial_generic import (
    GenericCommandContractError,
    _result,
    validate_generic_terminal_receipt,
)
from mcd_agent.commercial_portal import canonical_json
from mcd_agent.instance_uid import instance_uid_matches


FIXTURES = Path(__file__).parents[1] / "mcd_agent" / "contracts" / "fixtures"
MAX_PUBLIC_RESULT_BYTES = 16 * 1024


def _load(name: str) -> dict[str, object]:
    value = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def test_shared_depth_eight_fixture_is_accepted() -> None:
    value = _load("upgrade-os-result-depth8-valid-v1.json")
    assert _result(value, "portal_mcd_generic_terminal_receipt_invalid") == value


def test_shared_depth_nine_fixture_is_rejected() -> None:
    value = _load("upgrade-os-result-depth9-invalid-v1.json")
    with pytest.raises(
        GenericCommandContractError,
        match="portal_mcd_generic_terminal_receipt_invalid",
    ):
        _result(value, "portal_mcd_generic_terminal_receipt_invalid")


def test_shared_identity_fixture_normalizes_legacy_same_host_only() -> None:
    value = _load("upgrade-os-instance-identity-v1.json")
    host_name = value["host_name"]
    persisted = value["persisted_instance_uid"]
    assert isinstance(host_name, str)
    assert isinstance(persisted, str)
    cases = value["cases"]
    assert isinstance(cases, list)
    for case in cases:
        assert isinstance(case, dict)
        accepted = instance_uid_matches(str(case["input"]), persisted, host_name)
        assert accepted is case["accepted"]
        if accepted:
            assert case["canonical_instance_uid"] == persisted


@pytest.mark.parametrize(
    "name,status",
    [
        ("upgrade-os-terminal-success-v1.json", "succeeded"),
        ("upgrade-os-terminal-failure-v1.json", "failed"),
    ],
)
def test_shared_terminal_fixtures_are_valid_bounded_and_secret_free(
    name: str,
    status: str,
) -> None:
    value = _load(name)
    assert validate_generic_terminal_receipt(value)["status"] == status
    result = value["result"]
    assert isinstance(result, dict)
    assert len(canonical_json(result)) <= MAX_PUBLIC_RESULT_BYTES
    raw = json.dumps(result, sort_keys=True).lower()
    for forbidden in (
        '"argv"',
        '"command_line"',
        '"raw_stdout"',
        '"raw_stderr"',
        '"target_db_credentials_json"',
    ):
        assert forbidden not in raw
