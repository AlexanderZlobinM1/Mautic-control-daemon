from __future__ import annotations

import unittest
from unittest.mock import patch

from mcd_agent.image_compatibility import (
    ImageCompatibilityError,
    capability,
    collect_fresh_facts,
    evaluate_request,
    load_request_bytes,
    validate_request,
)
from mcd_agent.state_push import _advertise_image_compatibility


class ImageCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.request = {
            "schema": "mcc-image-compatibility-request-v1",
            "request_id": "r1",
            "operation": "add_instance",
            "destination": {"domain": "x.example", "root": "/var/www/x", "database": "baza_x", "state_key": "x"},
            "candidates": [{"image_ref": "m7", "name": "m7", "files_digest": "a" * 64, "db_digest": "b" * 64, "requirements": {"php_min": "8.2", "db_min": {"mariadb": [10, 11, 0]}, "target_min_free_bytes": 10, "requires": ["php_fpm", "database"]}}],
        }

    def test_compatible_uses_fpm_and_authenticated_database_facts(self):
        result = evaluate_request(self.request, facts={"php_fpm_version": "8.3.1", "database_engine": "mariadb", "database_version": "10.11.8", "database_active": True, "target_free_bytes": 100, "php_fpm": True, "database": True, "observed_at": "2026-10-09T10:00:00Z"})
        self.assertEqual(result["status"], "compatible")
        self.assertIs(result["compatible"], True)
        self.assertEqual(result["compatible_image_refs"], ["m7"])

    def test_unknown_never_becomes_compatible(self):
        result = evaluate_request(self.request, facts={"target_free_bytes": 100})
        self.assertEqual(result["status"], "unknown")
        self.assertIs(result["compatible"], False)
        self.assertEqual(result["compatible_image_refs"], [])

    def test_strict_schema_rejects_unknown_fields_tokens_duplicates_and_invalid_digest(self):
        cases = []
        extra = {**self.request, "unexpected": True}
        cases.append(extra)
        token = {**self.request, "candidates": [{**self.request["candidates"][0], "requirements": {"requires": ["shell"]}}]}
        cases.append(token)
        duplicate = {**self.request, "candidates": [self.request["candidates"][0], self.request["candidates"][0]]}
        cases.append(duplicate)
        digest = {**self.request, "candidates": [{**self.request["candidates"][0], "files_digest": "A" * 64}]}
        cases.append(digest)
        for payload in cases:
            with self.subTest(payload=payload), self.assertRaises(ImageCompatibilityError):
                validate_request(payload)

    def test_json_loader_rejects_duplicate_keys_and_nonfinite_numbers(self):
        with self.assertRaisesRegex(ImageCompatibilityError, "duplicate_key"):
            load_request_bytes(b'{"schema":"x","schema":"y"}')
        with self.assertRaisesRegex(ImageCompatibilityError, "constant_invalid"):
            load_request_bytes(b'{"value":NaN}')

    def test_database_matrix_is_engine_specific_and_digest_bound(self):
        request = {**self.request, "candidates": [{**self.request["candidates"][0]}]}
        request["candidates"][0]["requirements"] = {
            "php_min": "8.2",
            "db_min": {"mysql": [8, 4, 0]},
            "requires": ["php_fpm", "database"],
        }
        result = evaluate_request(request, facts={
            "php_fpm_version": "8.3",
            "database_engine": "mariadb",
            "database_version": "10.11.8",
            "database_active": True,
            "php_fpm": True,
            "database": True,
            "observed_at": "2026-10-09T10:00:00Z",
        })
        self.assertIs(result["compatible"], False)
        self.assertEqual(result["candidates"][0]["files_digest"], "a" * 64)
        self.assertEqual(result["candidates"][0]["db_digest"], "b" * 64)
        self.assertEqual(result["candidates"][0]["blockers"][0]["code"], "database_engine_not_supported")

    def test_real_catalog_prefixed_digests_are_accepted_and_preserved_exactly(self):
        candidate = {**self.request["candidates"][0]}
        candidate["files_digest"] = "sha256:" + "4" * 64
        candidate["db_digest"] = "sha256:" + "5" * 64
        request = {**self.request, "candidates": [candidate]}
        result = evaluate_request(request, facts={
            "php_fpm_version": "8.4",
            "database_engine": "mariadb",
            "database_version": "10.11.8",
            "database_active": True,
            "target_free_bytes": 10_000_000_000,
            "php_fpm": True,
            "database": True,
            "observed_at": "2026-10-09T10:00:00Z",
        })
        self.assertEqual(result["candidates"][0]["files_digest"], candidate["files_digest"])
        self.assertEqual(result["candidates"][0]["db_digest"], candidate["db_digest"])
        self.assertIs(result["compatible"], True)

    def test_database_is_not_a_blocker_when_catalog_declares_no_database_requirement(self):
        candidate = {**self.request["candidates"][0]}
        candidate["requirements"] = {"php_min": "8.4", "target_min_free_bytes": 9_276_552_488}
        request = {**self.request, "candidates": [candidate]}
        result = evaluate_request(request, facts={
            "php_fpm_version": "8.4",
            "database_engine": "",
            "database_version": "",
            "database_active": None,
            "target_free_bytes": 10_000_000_000,
            "observed_at": "2026-10-09T10:00:00Z",
        })
        self.assertIs(result["compatible"], True)
        self.assertEqual(result["candidates"][0]["blockers"], [])

    def test_fresh_probe_uses_active_fpm_authenticated_database_and_target_space(self):
        readiness = {
            "ipv6_disabled": True,
            "nginx": {"installed": True, "active": True},
            "php": {"fpm": {"8.2": {"active": True}, "8.4": {"active": True}}},
            "composer": {"installed": True},
            "node": {"installed": True},
            "npm": {"installed": True},
            "certbot": {"installed": True},
        }
        with (
            patch("mcd_agent.image_compatibility._collect_readiness_facts", return_value=readiness),
            patch("mcd_agent.mautic_image_install._mysql_exec", return_value="10.11.8-MariaDB"),
        ):
            facts = collect_fresh_facts(self.request["destination"])
        self.assertEqual(facts["php_fpm_version"], "8.4")
        self.assertEqual((facts["database_engine"], facts["database_version"]), ("mariadb", "10.11.8"))
        self.assertIs(facts["database"], True)
        self.assertIsInstance(facts["target_free_bytes"], int)

    def test_capability_advertises_agreed_command_and_strict_schemas(self):
        advertised = capability()
        self.assertEqual(advertised["command"], "mcd-cli image-compatibility --request-json <file|-> --json")
        self.assertEqual(advertised["request_schema"], "mcc-image-compatibility-request-v1")
        self.assertEqual(advertised["response_schema"], "mcd-image-compatibility-response-v1")
        self.assertIs(advertised["read_only"], True)
        self.assertIs(advertised["fresh_probe"], True)

    def test_normal_state_payload_advertises_exact_capability_in_preserved_envelope(self):
        payload = {
            "schema": "mcd-state-v1",
            "mautic_install_readiness": {"schema": "mcd-mautic-install-readiness-v1"},
        }
        advertised = _advertise_image_compatibility(payload)
        self.assertEqual(payload["image_compatibility_capability"], advertised)
        self.assertEqual(payload["mautic_install_readiness"]["image_compatibility_capability"], advertised)
        self.assertEqual(advertised["contract_sha256"], capability()["contract_sha256"])
        self.assertEqual(advertised["command"], "mcd-cli image-compatibility --request-json <file|-> --json")


if __name__ == "__main__":
    unittest.main()
