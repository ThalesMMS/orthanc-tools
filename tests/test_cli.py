import argparse
import sys
import types
import unittest
from unittest import mock

from orthanc_tools import cli


class CliHelpTests(unittest.TestCase):
    def test_top_level_help_describes_workflows_in_task_terms(self) -> None:
        help_text = cli.build_parser().format_help()

        self.assertIn("usage: orthanc-tools [-h] COMMAND", help_text)
        self.assertIn("Workflows:", help_text)
        self.assertIn("sync-remote", help_text)
        self.assertIn("Mirror a remote PACS into local Orthanc.", help_text)
        self.assertIn("Use for continuous sync and drift repair.", help_text)
        self.assertIn("backfill-by-date", help_text)
        self.assertIn("Use for one-shot operational backfill.", help_text)
        self.assertIn("backup-remote-to-zip", help_text)
        self.assertIn("Use for remote backup to disk.", help_text)
        self.assertIn("export-local-to-zip", help_text)
        self.assertIn("Use for an already-local archive.", help_text)

    def test_workflow_delegation_uses_stable_command_specific_prog(self) -> None:
        seen_argv: list[str] = []
        main_module = sys.modules["__main__"]
        old_main_spec = main_module.__spec__
        sentinel_spec = types.SimpleNamespace(name="orthanc_tools.__main__")
        main_module.__spec__ = sentinel_spec

        def capture_argv(_module_name: str, run_name: str) -> None:
            self.assertEqual(run_name, "__main__")
            seen_argv[:] = sys.argv
            self.assertEqual(argparse.ArgumentParser().prog, "orthanc-tools sync-remote")

        try:
            with mock.patch("runpy.run_module", side_effect=capture_argv) as run_module:
                exit_code = cli.main(["sync-remote", "--help"])

            self.assertEqual(exit_code, 0)
            run_module.assert_called_once_with(
                "orthanc_tools.workflows.sync_remote",
                run_name="__main__",
            )
            self.assertEqual(seen_argv, ["orthanc-tools sync-remote", "--help"])
            self.assertIs(main_module.__spec__, sentinel_spec)
        finally:
            main_module.__spec__ = old_main_spec
