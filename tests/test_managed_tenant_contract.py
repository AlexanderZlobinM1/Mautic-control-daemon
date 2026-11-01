from mcd_agent.commercial_generic import (
    GenericCommandContractError,
    validate_generic_command_job,
    validate_managed_installation_tenant,
)
from mcd_agent.commercial_registration import import_activation_material, RegistrationMaterialError
from mcd_agent.commercial_portal import CommercialState
import json
import sqlite3


def _job(account_id=None):
    installation = {"installation_id": "installation-1", "dns_name": "customer.example"}
    if account_id is not None:
        installation["account_id"] = account_id
    return {
        "schema": "portal-mcd-generic-command-job-v1",
        "contract_version": "1.0",
        "job_id": "job-1",
        "request_id": "request-1",
        "idempotency_key": "request-1:command",
        "command_id": "command-1",
        "delivery_cursor": 1,
        "installation": installation,
        "capability": {"capability_id": "mautic", "version": "1.0"},
        "command": {
            "command_id": "mautic_version",
            "version": "1.0",
            "execution": "generic_primitive",
            "mutates_state": False,
        },
        "input": {},
        "issued_at": "2026-01-01T00:00:00Z",
        "expires_at": "2099-01-01T00:00:00Z",
    }


def _inventory(account_id=None):
    installation = {"installation_id": "installation-1", "dns_name": "customer.example"}
    if account_id is not None:
        installation["account_id"] = account_id
    return {
        "installation": installation,
        "capabilities": [{
            "capability_id": "mautic",
            "version": "1.0",
            "commands": [{
                "command_id": "mautic_version",
                "version": "1.0",
                "execution": "generic_primitive",
                "mutates_state": False,
                "input_schema_ref": "portal://mcd/input/mautic-version-v1",
            }],
        }],
    }


def test_own_identity_remains_backward_compatible_without_tenant():
    assert "account_id" not in validate_generic_command_job(_job(), inventory=_inventory())["installation"]


def test_managed_tenant_is_preserved_and_must_match_admitted_uid():
    job = validate_generic_command_job(_job("tenant-a"), inventory=_inventory("tenant-a"))
    assert job["installation"]["account_id"] == "tenant-a"
    assert validate_managed_installation_tenant(
        job["installation"],
        installation_ids={"instance.uid": "installation-1"},
        managed_instances={"instance.uid": {"account_id": "tenant-a"}},
    ) == "tenant-a"


def test_cross_tenant_job_is_rejected_before_execution_contract():
    try:
        validate_managed_installation_tenant(
            _job("tenant-b")["installation"],
            installation_ids={"instance.uid": "installation-1"},
            managed_instances={"instance.uid": {"account_id": "tenant-a"}},
        )
    except GenericCommandContractError as exc:
        assert str(exc) == "portal_mcd_managed_tenant_binding_invalid"
    else:
        raise AssertionError("cross-tenant job was accepted")


def test_inventory_and_job_tenants_must_be_identical():
    try:
        validate_generic_command_job(_job("tenant-b"), inventory=_inventory("tenant-a"))
    except GenericCommandContractError as exc:
        assert str(exc) == "portal_mcd_managed_tenant_binding_invalid"
    else:
        raise AssertionError("cross-tenant inventory/job pair was accepted")


def test_managed_carrier_requires_tenant_per_uid(tmp_path):
    state = CommercialState(str(tmp_path / "state.db"))
    with sqlite3.connect(state.path) as db:
        db.execute("CREATE TABLE instances(instance_uid TEXT,root TEXT,primary_domain TEXT,domains_json TEXT)")
        db.execute("INSERT INTO instances VALUES (?,?,?,?)", ("instance.uid", "/srv/app", "customer.example", "[]"))
    material = {
        "schema": "mcd-portal-enrollment-material-v1",
        "contract_version": "1.0",
        "identity_kind": "dns",
        "local_identity": "routing.example",
        "ownership": "sales_snap_managed",
        "account_id": "provider-account",
        "agent_id": "agent-1",
        "commercial_key_id": "key-1",
        "portal_agent_credential": "fixture",
        "portal_api_base": "https://portal.example/api/v1/commercial",
        "installation_ids": {"instance.uid": "installation-1"},
        "managed_instances": {"instance.uid": {
            "instance_uid": "instance.uid",
            "installation_id": "installation-1",
            "root": "/srv/app",
            "dns_name": "customer.example",
            "mcc_host_name": "routing.example",
            "mcc_job_id": "job-1",
            "order_id": "order-1",
        }},
    }
    try:
        import_activation_material(state, material)
    except RegistrationMaterialError as exc:
        assert str(exc) == "portal_registration_managed_tenant_invalid"
    else:
        raise AssertionError("managed carrier without per-UID tenant was accepted")
