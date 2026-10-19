from __future__ import annotations

import subprocess
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from mcd_agent import daemon


class ExternalTaskAdmissionTests(unittest.TestCase):
    def test_external_console_processes_are_one_logical_task(self) -> None:
        root = "/var/www/client"
        ps_output = (
            "4101 1200 php /var/www/client/bin/console mautic:segments:update -i 92\n"
            "4102 900 php /var/www/client/bin/console mautic:segments:update -i 92\n"
        )
        with patch.object(
            subprocess,
            "run",
            return_value=SimpleNamespace(returncode=0, stdout=ps_output),
        ), patch.object(
            daemon,
            "_pid_cmdline_args",
            side_effect=lambda _pid: [
                "php",
                "/var/www/client/bin/console",
                "mautic:segments:update",
                "-i",
                "92",
            ],
        ):
            rows = daemon.list_external_runtime_task_summaries([root])

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["pid"], 4101)
        self.assertEqual(
            daemon._external_task_key(root, "segment", 92, 4101),
            daemon._external_task_key(root, "segment", 92, 4102),
        )

