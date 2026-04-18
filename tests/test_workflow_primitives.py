import argparse
import importlib
import io
import signal
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

from orthanc_tools.workflows.primitives import (
    STOP_REQUESTED,
    OrthancSettings,
    cli_error,
    extract_resource_id,
    handle_signal,
    load_orthanc_settings,
    register_signal_handlers,
)


class WorkflowPrimitivesTests(unittest.TestCase):
    def tearDown(self) -> None:
        STOP_REQUESTED.set(False)

    def test_orthanc_settings_supports_minimal_and_full_variants(self) -> None:
        minimal = OrthancSettings(base_url="http://localhost:8042", username="admin", password="secret")
        full = OrthancSettings(
            base_url="http://localhost:8043",
            username="alice",
            password="pw",
            dicom_aet="ORTHANC-AE",
            timeout=12.5,
            getscu_timeout=123.0,
            dicom_modalities={"REMOTE": {"AET": "R"}},
        )

        self.assertEqual(minimal.dicom_aet, None)
        self.assertEqual(minimal.timeout, 60.0)
        self.assertEqual(minimal.getscu_timeout, 60.0)
        self.assertEqual(minimal.dicom_modalities, None)
        self.assertEqual(full.dicom_aet, "ORTHANC-AE")
        self.assertEqual(full.timeout, 12.5)
        self.assertEqual(full.getscu_timeout, 123.0)
        self.assertEqual(full.dicom_modalities, {"REMOTE": {"AET": "R"}})

    def test_cli_error_writes_to_stderr_and_exits(self) -> None:
        stream = io.StringIO()
        with redirect_stderr(stream):
            with self.assertRaises(SystemExit) as ctx:
                cli_error("boom")

        self.assertEqual(ctx.exception.code, 1)
        self.assertIn("ERROR: boom", stream.getvalue())

    def test_extract_resource_id_accepts_multiple_payload_shapes(self) -> None:
        self.assertEqual(extract_resource_id({"ID": "abc"}), "abc")
        self.assertEqual(extract_resource_id({"Id": 42}), "42")
        self.assertEqual(extract_resource_id({"Path": "/queries/xyz"}), "xyz")
        self.assertEqual(extract_resource_id({"URL": "https://host/queries/xyz/"}), "xyz")
        self.assertEqual(extract_resource_id("/instances/123"), "123")

    def test_handle_signal_sets_mutable_stop_flag(self) -> None:
        stream = io.StringIO()
        with redirect_stderr(stream):
            handle_signal(signal.SIGINT, None)
        self.assertTrue(STOP_REQUESTED)
        self.assertIn("SIGINT", stream.getvalue())

    def test_register_signal_handlers_includes_hup_when_available(self) -> None:
        with mock.patch("signal.signal") as signal_fn:
            register_signal_handlers(handle_signal)

        registered = [call.args[0] for call in signal_fn.call_args_list]
        self.assertIn(signal.SIGINT, registered)
        self.assertIn(signal.SIGTERM, registered)
        if hasattr(signal, "SIGHUP"):
            self.assertIn(signal.SIGHUP, registered)

    def test_backup_workflow_import_does_not_register_signal_handlers(self) -> None:
        with mock.patch("orthanc_tools.workflows.primitives.signal.signal") as signal_fn:
            backup_module = importlib.import_module("orthanc_tools.workflows.backup_remote_to_zip")
            importlib.reload(backup_module)

        signal_fn.assert_not_called()

    def test_load_orthanc_settings_reads_config_credentials_and_updates_calling_aet(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir)
            (config_dir / "orthanc.json").write_text(
                '{"HttpPort": 8043, "DicomAet": "ORTHANC-AE", "DicomModalities": {"REMOTE": {"AET": "R"}}}',
                encoding="utf-8",
            )
            (config_dir / "credentials.json").write_text(
                '{"RegisteredUsers": {"alice": "secret"}}',
                encoding="utf-8",
            )
            args = argparse.Namespace(
                config_dir=config_dir,
                orthanc_config=None,
                credentials_config=None,
                base_url=None,
                user=None,
                password=None,
                calling_aet=None,
                timeout=12.5,
                getscu_timeout_seconds=1800.0,
            )

            settings = load_orthanc_settings(args)

        self.assertEqual(settings.base_url, "http://127.0.0.1:8043")
        self.assertEqual(settings.username, "alice")
        self.assertEqual(settings.password, "secret")
        self.assertEqual(settings.dicom_aet, "ORTHANC-AE")
        self.assertEqual(settings.timeout, 12.5)
        self.assertEqual(settings.getscu_timeout, 1800.0)
        self.assertEqual(settings.dicom_modalities, {"REMOTE": {"AET": "R"}})
        self.assertEqual(args.calling_aet, "ORTHANC-AE")

    def test_load_orthanc_settings_supports_explicit_base_url_without_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir)
            args = argparse.Namespace(
                config_dir=config_dir,
                orthanc_config=None,
                credentials_config=None,
                base_url="http://localhost:8042",
                user="bob",
                password="pw",
                timeout=90.0,
                getscu_timeout_seconds=120.0,
            )

            settings = load_orthanc_settings(args)

        self.assertEqual(settings.base_url, "http://localhost:8042")
        self.assertEqual(settings.username, "bob")
        self.assertEqual(settings.password, "pw")
        self.assertEqual(settings.dicom_aet, "ORTHANC")
        self.assertEqual(settings.timeout, 90.0)
        self.assertEqual(settings.getscu_timeout, 120.0)
        self.assertEqual(settings.dicom_modalities, {})

    def test_extract_resource_id_accepts_uri_and_id_variants(self) -> None:
        self.assertEqual(extract_resource_id({"Uri": "/queries/abc"}), "abc")
        self.assertEqual(extract_resource_id({"URI": "/studies/xyz/"}), "xyz")
        self.assertEqual(extract_resource_id({"id": "direct-id"}), "direct-id")
        self.assertEqual(extract_resource_id("plain-string"), "plain-string")
        self.assertEqual(extract_resource_id("/path/to/resource"), "resource")

    def test_extract_resource_id_raises_on_unrecognized_payload(self) -> None:
        with self.assertRaises(RuntimeError):
            extract_resource_id(None)
        with self.assertRaises(RuntimeError):
            extract_resource_id({})
        with self.assertRaises(RuntimeError):
            extract_resource_id(42)

    def test_stop_requested_flag_is_mutable_and_bool_evaluates_value(self) -> None:
        from orthanc_tools.workflows.primitives import StopRequestedFlag

        flag = StopRequestedFlag()
        self.assertFalse(flag)
        flag.set(True)
        self.assertTrue(flag)
        flag.set(False)
        self.assertFalse(flag)

    def test_load_orthanc_settings_fails_when_no_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir)
            (config_dir / "orthanc.json").write_text(
                '{"HttpPort": 8042}',
                encoding="utf-8",
            )
            args = argparse.Namespace(
                config_dir=config_dir,
                orthanc_config=None,
                credentials_config=None,
                base_url=None,
                user=None,
                password=None,
                calling_aet=None,
                timeout=60.0,
            )
            stream = io.StringIO()
            with redirect_stderr(stream):
                with self.assertRaises(SystemExit) as ctx:
                    load_orthanc_settings(args)

        self.assertEqual(ctx.exception.code, 1)
        self.assertIn("credentials", stream.getvalue())

    def test_load_orthanc_settings_prefers_cli_credentials_over_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir)
            (config_dir / "orthanc.json").write_text(
                '{"HttpPort": 8042, "DicomAet": "FROM_FILE"}',
                encoding="utf-8",
            )
            (config_dir / "credentials.json").write_text(
                '{"RegisteredUsers": {"file-user": "file-password"}}',
                encoding="utf-8",
            )
            args = argparse.Namespace(
                config_dir=config_dir,
                orthanc_config=None,
                credentials_config=None,
                base_url=None,
                user="cli-user",
                password="cli-password",
                calling_aet=None,
                timeout=60.0,
            )

            settings = load_orthanc_settings(args)

        self.assertEqual(settings.username, "cli-user")
        self.assertEqual(settings.password, "cli-password")

    def test_load_orthanc_settings_fails_without_config_and_no_calling_aet(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir)
            args = argparse.Namespace(
                config_dir=config_dir,
                orthanc_config=None,
                credentials_config=None,
                base_url="http://localhost:8042",
                user="bob",
                password="pw",
                calling_aet=None,
                timeout=60.0,
            )
            stream = io.StringIO()
            with redirect_stderr(stream):
                with self.assertRaises(SystemExit) as ctx:
                    load_orthanc_settings(args)

        self.assertEqual(ctx.exception.code, 1)
        self.assertIn("calling-aet", stream.getvalue())
