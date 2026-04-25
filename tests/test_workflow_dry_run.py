import io
import tempfile
import unittest
from argparse import Namespace
from contextlib import redirect_stdout
from datetime import date
from pathlib import Path
from unittest import mock

from orthanc_tools.workflows import backfill_by_date, backup_remote_to_zip, export_local_to_zip, sync_remote
from orthanc_tools.workflows.primitives import STOP_REQUESTED, OrthancSettings


TEST_CREDENTIAL = "secret"
TEMP_ROOT = Path(tempfile.gettempdir())


class FakeDryRunClient:
    def __init__(self) -> None:
        self.settings = OrthancSettings(
            base_url="http://orthanc.example",
            username="operator",
            password=TEST_CREDENTIAL,
        )
        self.modalities: list[tuple[str, str, str, int]] = []
        self.echoes: list[tuple[str, int]] = []
        self.queries: list[tuple[str, str, dict[str, str]]] = []
        self.deleted_queries: list[str] = []

    def put_modality(self, name: str, aet: str, host: str, port: int) -> None:
        self.modalities.append((name, aet, host, port))

    def list_modalities(self) -> list[str]:
        return ["REMOTE-PLAN"]

    def echo_modality(self, name: str, timeout: int = 10) -> None:
        self.echoes.append((name, timeout))

    def system(self) -> dict[str, str]:
        return {"Name": "Orthanc", "Version": "1.12.9"}

    def create_remote_query(self, modality: str, level: str, query_fields: dict[str, str]) -> str:
        self.queries.append((modality, level, query_fields))
        return query_fields["StudyDate"]

    def get_query_answers(self, query_id: str) -> list[str]:
        return {
            "20240101-20240101": ["answer-1", "answer-2"],
            "20240102-20240102": [],
        }[query_id]

    def get_query_answer_content(self, query_id: str, answer_id: str) -> dict[str, str]:
        return {
            ("20240101-20240101", "answer-1"): {
                "StudyInstanceUID": "1.2.3",
                "PatientID": "P001",
                "PatientName": "Patient One",
                "StudyDate": "20240101",
                "NumberOfStudyRelatedSeries": "2",
                "NumberOfStudyRelatedInstances": "10",
            },
            ("20240101-20240101", "answer-2"): {
                "StudyInstanceUID": "1.2.4",
                "PatientID": "P002",
                "PatientName": "Patient Two",
                "StudyDate": "20240101",
                "NumberOfStudyRelatedSeries": "1",
                "NumberOfStudyRelatedInstances": "3",
            },
        }[(query_id, answer_id)]

    def delete_query(self, query_id: str) -> None:
        self.deleted_queries.append(query_id)

    def import_dicom_file(self, *_args, **_kwargs):
        raise AssertionError("dry-run must not import DICOM files")


class FakeSyncDryRunClient:
    def __init__(self) -> None:
        self.settings = OrthancSettings(
            base_url="http://orthanc.example",
            username="operator",
            password=TEST_CREDENTIAL,
        )
        self.deleted_queries: list[str] = []

    def system(self) -> dict[str, str]:
        return {"Name": "Orthanc", "Version": "1.12.9"}

    def list_modalities(self) -> list[str]:
        return ["REMOTE"]

    def create_remote_study_query(self, modality: str) -> str:
        self.remote_modality = modality
        return "remote-query"

    def get_query_answer_pairs(self, query_id: str):
        if query_id != "remote-query":
            raise AssertionError(f"unexpected query {query_id}")
        return [
            ("answer-1", {"StudyInstanceUID": "1.2.3", "PatientID": "P001", "StudyDate": "20240101"}),
            ("answer-2", {"StudyInstanceUID": "1.2.4", "PatientID": "P002", "StudyDate": "20240102"}),
        ]

    def list_local_study_map(self) -> dict[str, str]:
        return {"1.2.3": "local-1", "9.9.9": "local-extra"}

    def delete_query(self, query_id: str) -> None:
        self.deleted_queries.append(query_id)

    def retrieve_study(self, *_args, **_kwargs):
        raise AssertionError("dry-run must not retrieve studies")

    def delete_study(self, *_args, **_kwargs):
        raise AssertionError("dry-run must not delete studies")


class FakeExportDryRunClient:
    def __init__(self) -> None:
        self.settings = OrthancSettings(
            base_url="http://orthanc.example",
            username="operator",
            password=TEST_CREDENTIAL,
        )
        self.queried_days: list[date] = []

    def system(self) -> dict[str, str]:
        return {"Name": "Orthanc", "Version": "1.12.9"}

    def find_studies_for_day(self, day: date, page_size: int):
        self.queried_days.append(day)
        self.last_page_size = page_size
        if day == date(2024, 1, 1):
            return [object(), object()]
        return []

    def download_study_archive(self, *_args, **_kwargs):
        raise AssertionError("dry-run must not download ZIP archives")


def base_args(**overrides):
    values = {
        "start_date": date(2024, 1, 1),
        "end_date": date(2024, 1, 2),
        "remote_aet": "REMOTE",
        "remote_host": "pacs.example",
        "remote_port": 104,
        "remote_name": "REMOTE-PLAN",
        "echo_timeout": 7,
        "state_dir": str(TEMP_ROOT / "orthanc-state"),
        "backup_dir": TEMP_ROOT / "orthanc-backup",
        "name": "uid",
        "zip_mode": "archive",
        "whole_study_threshold": 32,
        "allow_heuristic_fallback": True,
        "allowance_per_series": 2,
        "dry_run": True,
    }
    values.update(overrides)
    return Namespace(**values)


def sync_args(**overrides):
    values = {
        "remote": "REMOTE",
        "repair_mode": "replace",
        "target_aet": "ORTHANC",
        "retrieve_method": "get",
        "timeout": 60.0,
        "getscu_timeout_seconds": 60.0,
        "settle_seconds": 0.0,
        "max_retries": 2,
        "no_rich": True,
        "yes": False,
        "limit_studies": None,
        "allow_empty_remote": False,
        "dry_run": True,
    }
    values.update(overrides)
    return Namespace(**values)


def export_args(**overrides):
    values = {
        "start_date": date(2024, 1, 1),
        "end_date": date(2024, 1, 2),
        "name": "uid",
        "backup_dir": TEMP_ROOT / "orthanc-export",
        "state_dir": TEMP_ROOT / "orthanc-export" / ".state",
        "config_dir": TEMP_ROOT,
        "orthanc_config": None,
        "credentials_config": None,
        "base_url": "http://orthanc.example",
        "user": "operator",
        "password": TEST_CREDENTIAL,
        "timeout": 120.0,
        "page_size": 200,
        "retries": 3,
        "retry_delay": 5.0,
        "zip_mode": "stored",
        "dry_run": True,
    }
    values.update(overrides)
    return Namespace(**values)


class WorkflowDryRunTests(unittest.TestCase):
    def tearDown(self) -> None:
        STOP_REQUESTED.set(False)

    def test_backfill_dry_run_queries_inventory_without_retrieval_or_state_write(self) -> None:
        client = FakeDryRunClient()
        args = base_args()
        app = backfill_by_date.BackfillApp(args, client, backfill_by_date._DryRunState(Path(args.state_dir)))
        app.state.save_meta = mock.Mock()
        app.state.save_day_status = mock.Mock()

        with mock.patch.object(
            app, "check_remote_modality_for_dry_run", wraps=app.check_remote_modality_for_dry_run
        ) as check_remote_modality, mock.patch.object(
            app, "prepare_remote_modality", wraps=app.prepare_remote_modality
        ) as prepare_remote_modality, mock.patch.object(
            client, "put_modality", wraps=client.put_modality
        ) as put_modality, mock.patch.object(
            app, "query_remote_day_studies", wraps=app.query_remote_day_studies
        ) as query_remote_day_studies, mock.patch.object(
            app, "retrieve_and_import", wraps=app.retrieve_and_import
        ) as retrieve_and_import, mock.patch.object(
            app, "run_getscu_study", wraps=app.run_getscu_study
        ) as run_getscu_study, mock.patch.object(
            app, "run_getscu_missing_instances", wraps=app.run_getscu_missing_instances
        ) as run_getscu_missing_instances:
            stream = io.StringIO()
            with redirect_stdout(stream):
                result = app.dry_run()

        output = stream.getvalue()
        self.assertEqual(result, 0)
        check_remote_modality.assert_called_once_with()
        prepare_remote_modality.assert_not_called()
        put_modality.assert_not_called()
        self.assertEqual(query_remote_day_studies.call_count, 2)
        retrieve_and_import.assert_not_called()
        run_getscu_study.assert_not_called()
        run_getscu_missing_instances.assert_not_called()
        app.state.save_meta.assert_not_called()
        app.state.save_day_status.assert_not_called()
        self.assertIn("Dry-run plan: backfill-by-date", output)
        self.assertIn("2024-01-01: 2 studies", output)
        self.assertIn("2024-01-02: 0 studies", output)
        self.assertIn("Total remote studies: 2", output)
        self.assertIn("No data was retrieved or state written", output)
        self.assertEqual(client.echoes, [("REMOTE-PLAN", 7)])
        self.assertEqual(len(client.queries), 2)
        self.assertEqual(client.deleted_queries, ["20240101-20240101", "20240102-20240102"])

    def test_backfill_dry_run_stops_promptly_during_inventory(self) -> None:
        client = FakeDryRunClient()
        args = base_args()
        app = backfill_by_date.BackfillApp(args, client, backfill_by_date._DryRunState(Path(args.state_dir)))
        STOP_REQUESTED.set(True)

        with mock.patch.object(
            app, "query_remote_day_studies", wraps=app.query_remote_day_studies
        ) as query_remote_day_studies, mock.patch.object(
            client, "put_modality", wraps=client.put_modality
        ) as put_modality:
            stream = io.StringIO()
            with redirect_stdout(stream):
                result = app.dry_run()

        self.assertEqual(result, 130)
        self.assertIn("Stopped by request during dry-run", stream.getvalue())
        query_remote_day_studies.assert_not_called()
        put_modality.assert_not_called()

    def test_backup_dry_run_queries_inventory_without_retrieval_zip_or_delete(self) -> None:
        client = FakeDryRunClient()
        args = base_args(state_dir=TEMP_ROOT / "orthanc-state")
        app = backup_remote_to_zip.BackupZipApp(
            args,
            client,
            backup_remote_to_zip._DryRunState(args.state_dir),
            None,
        )
        app.state.save_meta = mock.Mock()
        app.state.save_day_status = mock.Mock()

        with mock.patch.object(
            app, "prepare_remote_modality", wraps=app.prepare_remote_modality
        ) as prepare_remote_modality, mock.patch.object(
            app, "query_remote_day_studies", wraps=app.query_remote_day_studies
        ) as query_remote_day_studies, mock.patch.object(
            app, "retrieve_and_import", wraps=app.retrieve_and_import
        ) as retrieve_and_import, mock.patch.object(
            app, "run_getscu_study", wraps=app.run_getscu_study
        ) as run_getscu_study, mock.patch.object(
            app, "run_getscu_missing_instances", wraps=app.run_getscu_missing_instances
        ) as run_getscu_missing_instances:
            stream = io.StringIO()
            with redirect_stdout(stream):
                result = app.dry_run()

        output = stream.getvalue()
        self.assertEqual(result, 0)
        prepare_remote_modality.assert_called_once_with()
        self.assertEqual(query_remote_day_studies.call_count, 2)
        retrieve_and_import.assert_not_called()
        run_getscu_study.assert_not_called()
        run_getscu_missing_instances.assert_not_called()
        app.state.save_meta.assert_not_called()
        app.state.save_day_status.assert_not_called()
        self.assertIn("Dry-run plan: backup-remote-to-zip", output)
        self.assertIn(f"Backup directory: {TEMP_ROOT / 'orthanc-backup'}", output)
        self.assertIn("2024-01-01: 2 studies", output)
        self.assertIn("Total remote studies: 2", output)
        self.assertIn("ZIPs written, local studies deleted, or state written", output)
        self.assertEqual(client.echoes, [("REMOTE-PLAN", 7)])
        self.assertEqual(len(client.queries), 2)
        self.assertEqual(client.deleted_queries, ["20240101-20240101", "20240102-20240102"])

    def test_backfill_main_dry_run_does_not_construct_state_manager(self) -> None:
        client = FakeDryRunClient()
        with tempfile.TemporaryDirectory() as tmpdir:
            state_dir = Path(tmpdir) / "state"
            args = base_args(state_dir=str(state_dir))
            with mock.patch.object(backfill_by_date, "parse_args", return_value=args), mock.patch.object(
                backfill_by_date.shutil, "which", return_value="/usr/bin/tool"
            ), mock.patch.object(
                backfill_by_date, "load_orthanc_settings", return_value=client.settings
            ), mock.patch.object(
                backfill_by_date, "OrthancClient", return_value=client
            ), mock.patch.object(
                backfill_by_date, "StateManager", side_effect=AssertionError("state manager should not be used")
            ), redirect_stdout(io.StringIO()):
                result = backfill_by_date.main()

            self.assertEqual(result, 0)
            self.assertFalse(state_dir.exists())

    def test_backup_main_dry_run_does_not_create_directories_or_state_manager(self) -> None:
        client = FakeDryRunClient()
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            args = base_args(state_dir=root / "state", backup_dir=root / "backup")
            with mock.patch.object(backup_remote_to_zip, "parse_args", return_value=args), mock.patch.object(
                backup_remote_to_zip.shutil, "which", return_value="/usr/bin/tool"
            ), mock.patch.object(
                backup_remote_to_zip, "load_orthanc_settings", return_value=client.settings
            ), mock.patch.object(
                backup_remote_to_zip, "OrthancClient", return_value=client
            ), mock.patch.object(
                backup_remote_to_zip, "ensure_dir", side_effect=AssertionError("ensure_dir should not be used")
            ), mock.patch.object(
                backup_remote_to_zip, "StateManager", side_effect=AssertionError("state manager should not be used")
            ), mock.patch.object(
                backup_remote_to_zip, "register_signal_handlers"
            ), redirect_stdout(io.StringIO()):
                result = backup_remote_to_zip.main()

            self.assertEqual(result, 0)
            self.assertFalse(args.state_dir.exists())
            self.assertFalse(args.backup_dir.exists())

    def test_sync_dry_run_compares_remote_and_local_inventory_without_mutation(self) -> None:
        client = FakeSyncDryRunClient()
        args = sync_args()
        app = sync_remote.OrthancMirrorApp(args, client, "REMOTE")

        with mock.patch.object(sync_remote.shutil, "which", return_value="/usr/bin/getscu"), mock.patch.object(
            app, "check_connectivity", wraps=app.check_connectivity
        ) as check_connectivity, mock.patch.object(
            app, "load_remote_inventory", wraps=app.load_remote_inventory
        ) as load_remote_inventory, mock.patch.object(
            client, "create_remote_study_query", wraps=client.create_remote_study_query
        ) as create_remote_study_query, mock.patch.object(
            client, "list_local_study_map", wraps=client.list_local_study_map
        ) as list_local_study_map, mock.patch.object(
            client, "retrieve_study", wraps=client.retrieve_study
        ) as retrieve_study, mock.patch.object(
            client, "delete_study", wraps=client.delete_study
        ) as delete_study:
            stream = io.StringIO()
            with redirect_stdout(stream):
                result = app.dry_run()

        output = stream.getvalue()
        self.assertEqual(result, 0)
        check_connectivity.assert_called_once_with()
        load_remote_inventory.assert_called_once_with()
        create_remote_study_query.assert_called_once_with("REMOTE")
        list_local_study_map.assert_called_once_with()
        retrieve_study.assert_not_called()
        delete_study.assert_not_called()
        self.assertIn("Dry-run plan: sync-remote", output)
        self.assertIn("Remote studies: 2", output)
        self.assertIn("Local studies: 2", output)
        self.assertIn("Missing locally: 1", output)
        self.assertIn("Extra local studies: 1", output)
        self.assertIn("WARNING: A real replace-mode run can delete", output)
        self.assertEqual(client.deleted_queries, ["remote-query"])

    def test_sync_main_dry_run_skips_destructive_confirmation(self) -> None:
        client = FakeSyncDryRunClient()
        args = sync_args(repair_mode="replace", yes=False)

        with mock.patch.object(sync_remote, "parse_args", return_value=args), mock.patch.object(
            sync_remote, "load_orthanc_settings", return_value=client.settings
        ), mock.patch.object(
            sync_remote, "OrthancClient", return_value=client
        ), mock.patch.object(
            sync_remote, "register_signal_handlers"
        ), mock.patch.object(
            sync_remote.shutil, "which", return_value="/usr/bin/getscu"
        ), mock.patch.object(
            sync_remote, "confirm_destructive_mode", side_effect=AssertionError("confirm should not run")
        ), redirect_stdout(io.StringIO()):
            result = sync_remote.main()

        self.assertEqual(result, 0)

    def test_export_local_dry_run_queries_local_inventory_without_state_or_zip_writes(self) -> None:
        client = FakeExportDryRunClient()
        args = export_args()

        with mock.patch.object(client, "system", wraps=client.system) as system, mock.patch.object(
            client, "find_studies_for_day", wraps=client.find_studies_for_day
        ) as find_studies_for_day, mock.patch.object(
            export_local_to_zip.ExportApp,
            "export_one_study",
            side_effect=AssertionError("export_one_study should not run"),
        ) as export_one_study, mock.patch.object(
            export_local_to_zip,
            "ExportRunState",
            side_effect=AssertionError("ExportRunState should not be used"),
        ) as export_run_state:
            stream = io.StringIO()
            with redirect_stdout(stream):
                result = export_local_to_zip.dry_run(args, client)

        output = stream.getvalue()
        self.assertEqual(result, 0)
        system.assert_called_once_with()
        self.assertEqual(find_studies_for_day.call_count, 2)
        export_one_study.assert_not_called()
        export_run_state.assert_not_called()
        self.assertIn("Dry-run plan: export-local-to-zip", output)
        self.assertIn("2024-01-01: 2 studies", output)
        self.assertIn("2024-01-02: 0 studies", output)
        self.assertIn("Total local studies: 2", output)
        self.assertIn("No ZIPs were written and no state was written", output)
        self.assertEqual(client.queried_days, [date(2024, 1, 1), date(2024, 1, 2)])
        self.assertEqual(client.last_page_size, 200)

    def test_export_main_dry_run_does_not_construct_export_app_or_create_state(self) -> None:
        client = FakeExportDryRunClient()
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            args = export_args(state_dir=root / "state", backup_dir=root / "backup")
            with mock.patch.object(export_local_to_zip, "parse_args", return_value=args), mock.patch.object(
                export_local_to_zip, "load_orthanc_settings", return_value=client.settings
            ), mock.patch.object(
                export_local_to_zip, "OrthancClient", return_value=client
            ), mock.patch.object(
                export_local_to_zip, "ExportApp", side_effect=AssertionError("ExportApp should not be used")
            ), redirect_stdout(io.StringIO()):
                result = export_local_to_zip.main()

            self.assertEqual(result, 0)
            self.assertFalse(args.state_dir.exists())
            self.assertFalse(args.backup_dir.exists())


if __name__ == "__main__":
    unittest.main()
