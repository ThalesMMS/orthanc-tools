import io
import tempfile
import unittest
from contextlib import redirect_stderr
from datetime import date
from pathlib import Path

from orthanc_tools.workflows.retrieval import RemoteStudy
from orthanc_tools.workflows.state_manager import ExportRunState, StateManager


class WorkflowStateManagerTests(unittest.TestCase):
    def make_state(self, root: Path, *, backup: bool = False) -> StateManager:
        kwargs = {
            "root": root,
            "start_date": date(2024, 1, 1),
            "end_date": date(2024, 1, 3),
            "remote_name": "REMOTE",
            "remote_aet": "REMOTE-AET",
            "remote_host": "127.0.0.1",
            "remote_port": 4242,
            "orthanc_base_url": "http://127.0.0.1:8042",
            "orthanc_user": "admin",
            "calling_aet": "ORTHANC",
        }
        if backup:
            kwargs.update(
                {
                    "backup_dir": root / "backup",
                    "name_mode": "patient",
                    "zip_mode": "archive",
                }
            )
        return StateManager(**kwargs)

    def test_backfill_state_manager_round_trips_cache_and_day_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state = self.make_state(Path(tmpdir) / "state")
            day = date(2024, 1, 1)
            studies = [
                RemoteStudy(
                    study_uid="1.2.3",
                    patient_id="P1",
                    patient_name="Alice",
                    study_date="20240101",
                    description="CT",
                    remote_series_count=2,
                    remote_instance_count=5,
                )
            ]

            state.save_day_cache(day, studies)
            loaded = state.load_day_cache(day)
            status = state.load_day_status(day)

        self.assertEqual(state.get_next_date(), date(2024, 1, 1))
        self.assertEqual(loaded, studies)
        self.assertEqual(status["date"], "2024-01-01")
        self.assertTrue(state.day_manifest_dir(day).exists())
        self.assertTrue(state.day_rejected_dir(day).exists())

    def test_state_manager_marks_days_done_and_updates_stats(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state = self.make_state(Path(tmpdir) / "state", backup=True)
            day = date(2024, 1, 1)
            studies = [RemoteStudy(study_uid="1.2.3", patient_birth_date="19700101")]
            status = {
                "studies": {
                    "1.2.3": {
                        "status": "complete",
                        "backup_complete": True,
                        "zip_filename": "study.zip",
                        "zip_bytes": 42,
                        "rejected_instances": {"1.2.3.4": {}},
                    }
                }
            }

            state.write_day_progress_tsv(day, studies, status)
            state.mark_day_done(day, studies, status, mode="complete-exact")

            progress = state.day_progress_tsv_path(day).read_text(encoding="utf-8")
            done_payload = state.day_done_path(day).read_text(encoding="utf-8")

        self.assertIn("zip_filename", progress)
        self.assertIn("complete-exact", done_payload)
        self.assertEqual(state.meta["stats"]["days_done"], 1)
        self.assertEqual(state.meta["stats"]["zips_complete"], 1)
        self.assertEqual(state.meta["stats"]["instances_rejected_archived"], 1)

    def test_state_manager_rejects_mismatched_resume_parameters(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "state"
            self.make_state(root)
            stream = io.StringIO()
            with redirect_stderr(stream):
                with self.assertRaises(SystemExit):
                    StateManager(
                        root=root,
                        start_date=date(2024, 1, 1),
                        end_date=date(2024, 1, 3),
                        remote_name="REMOTE",
                        remote_aet="OTHER-AET",
                        remote_host="127.0.0.1",
                        remote_port=4242,
                        orthanc_base_url="http://127.0.0.1:8042",
                        orthanc_user="admin",
                        calling_aet="ORTHANC",
                    )

        self.assertIn("belongs to another run", stream.getvalue())

    def test_export_run_state_persists_current_date_and_validates_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "state"
            state = ExportRunState(
                root,
                None,
                backup_dir=Path(tmpdir) / "backup",
                start_date=date(2024, 1, 1),
                end_date=date(2024, 1, 3),
                name_mode="patient",
                zip_mode="archive",
                orthanc_base_url="http://127.0.0.1:8042",
            )
            state.set_current_date(date(2024, 1, 2))
            reopened = ExportRunState(
                root,
                None,
                backup_dir=Path(tmpdir) / "backup",
                start_date=date(2024, 1, 1),
                end_date=date(2024, 1, 3),
                name_mode="patient",
                zip_mode="archive",
                orthanc_base_url="http://127.0.0.1:8042",
            )

            self.assertEqual(reopened.current_date(), date(2024, 1, 2))

            stream = io.StringIO()
            with redirect_stderr(stream):
                with self.assertRaises(SystemExit):
                    ExportRunState(
                        root,
                        None,
                        backup_dir=Path(tmpdir) / "backup",
                        start_date=date(2024, 1, 1),
                        end_date=date(2024, 1, 3),
                        name_mode="uid",
                        zip_mode="archive",
                        orthanc_base_url="http://127.0.0.1:8042",
                    )

        self.assertIn("different run parameter", stream.getvalue())

    def test_validate_expected_pairs_raises_on_missing_field(self) -> None:
        from orthanc_tools.workflows.state_manager import validate_expected_pairs

        payload = {"remote": {"aet": "AET"}}
        stream = io.StringIO()
        with redirect_stderr(stream):
            with self.assertRaises(SystemExit):
                validate_expected_pairs(
                    payload,
                    {("remote", "host"): "127.0.0.1"},
                    root="/some/path",
                )

        self.assertIn("missing", stream.getvalue())

    def test_validate_expected_pairs_raises_on_value_mismatch(self) -> None:
        from orthanc_tools.workflows.state_manager import validate_expected_pairs

        payload = {"remote": {"aet": "WRONG-AET"}}
        stream = io.StringIO()
        with redirect_stderr(stream):
            with self.assertRaises(SystemExit):
                validate_expected_pairs(
                    payload,
                    {("remote", "aet"): "EXPECTED-AET"},
                    root="/some/path",
                )

        self.assertIn("WRONG-AET", stream.getvalue())
        self.assertIn("EXPECTED-AET", stream.getvalue())

    def test_validate_expected_pairs_custom_messages_are_used(self) -> None:
        from orthanc_tools.workflows.state_manager import validate_expected_pairs

        payload = {"key": "bad"}
        stream = io.StringIO()
        with redirect_stderr(stream):
            with self.assertRaises(SystemExit):
                validate_expected_pairs(
                    payload,
                    {("key",): "good"},
                    root="/path",
                    mismatch_message=lambda parts, actual, expected: f"CUSTOM: {actual} vs {expected}",
                )

        self.assertIn("CUSTOM: bad vs good", stream.getvalue())

    def test_backfill_state_manager_marks_complete_via_status_field(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state = self.make_state(Path(tmpdir) / "state")
            day = date(2024, 1, 1)
            studies = [
                RemoteStudy(study_uid="1.2.3"),
                RemoteStudy(study_uid="4.5.6"),
            ]
            status = {
                "studies": {
                    "1.2.3": {"status": "complete", "rejected_instances": {}},
                    "4.5.6": {"status": "heuristic-complete", "rejected_instances": {"a": {}, "b": {}}},
                }
            }

            state.mark_day_done(day, studies, status, mode="complete-with-heuristic")

        self.assertEqual(state.meta["stats"]["days_done"], 1)
        self.assertEqual(state.meta["stats"]["studies_complete"], 2)
        self.assertEqual(state.meta["stats"]["instances_rejected_archived"], 2)
        self.assertNotIn("zips_complete", state.meta["stats"])

    def test_backfill_state_manager_writes_backfill_progress_tsv_format(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state = self.make_state(Path(tmpdir) / "state")
            day = date(2024, 1, 1)
            studies = [
                RemoteStudy(
                    study_uid="1.2.3",
                    patient_id="P1",
                    patient_name="Alice",
                    study_date="20240101",
                    description="CT",
                    remote_series_count=2,
                    remote_instance_count=5,
                )
            ]
            status = {"studies": {"1.2.3": {"status": "pending", "manifest_mode": "exact"}}}

            state.write_day_progress_tsv(day, studies, status)
            content = state.day_progress_tsv_path(day).read_text(encoding="utf-8")

        self.assertNotIn("description", content)
        self.assertNotIn("zip_filename", content)
        self.assertIn("1.2.3", content)
        self.assertNotIn("patient_id", content)
        self.assertNotIn("patient_name", content)
        self.assertNotIn("Alice", content)

    def test_state_manager_set_next_date_persists_to_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state = self.make_state(Path(tmpdir) / "state")

            state.set_next_date(date(2024, 1, 5))

            self.assertEqual(state.meta["next_date"], "2024-01-05")
            date_file = (Path(tmpdir) / "state" / "current-date.txt").read_text(encoding="utf-8").strip()
            self.assertEqual(date_file, "2024-01-05")

    def test_state_manager_prune_day_manifests_removes_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state = self.make_state(Path(tmpdir) / "state")
            day = date(2024, 1, 1)
            manifest_dir = state.day_manifest_dir(day)
            (manifest_dir / "fake.json").write_text("{}", encoding="utf-8")
            self.assertTrue(manifest_dir.exists())

            state.prune_day_manifests(day)

        self.assertFalse(manifest_dir.exists())

    def test_state_manager_requires_all_backup_args_together(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            with self.assertRaises(ValueError):
                StateManager(
                    root=Path(tmpdir) / "state",
                    start_date=date(2024, 1, 1),
                    end_date=date(2024, 1, 3),
                    remote_name="REMOTE",
                    remote_aet="REMOTE-AET",
                    remote_host="127.0.0.1",
                    remote_port=4242,
                    orthanc_base_url="http://127.0.0.1:8042",
                    orthanc_user="admin",
                    calling_aet="ORTHANC",
                    backup_dir=Path(tmpdir) / "backup",
                    name_mode="patient",
                    # zip_mode intentionally omitted → partial backup args
                )

    def test_export_run_state_mark_day_completed_advances_date(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "state"
            state = ExportRunState(
                root,
                None,
                backup_dir=Path(tmpdir) / "backup",
                start_date=date(2024, 1, 1),
                end_date=date(2024, 1, 5),
                name_mode="patient",
                zip_mode="archive",
                orthanc_base_url="http://127.0.0.1:8042",
            )

            state.mark_day_completed(date(2024, 1, 1), next_day=date(2024, 1, 2))

            self.assertEqual(state.state["last_completed_day"], "2024-01-01")
            self.assertEqual(state.state["current_date"], "2024-01-02")
            date_file = (root / "current-date.txt").read_text(encoding="utf-8").strip()
            self.assertEqual(date_file, "2024-01-02")

    def test_export_run_state_mark_completed_sets_status_and_advances_past_end(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "state"
            state = ExportRunState(
                root,
                None,
                backup_dir=Path(tmpdir) / "backup",
                start_date=date(2024, 1, 1),
                end_date=date(2024, 1, 3),
                name_mode="patient",
                zip_mode="archive",
                orthanc_base_url="http://127.0.0.1:8042",
            )

            state.mark_completed()

            self.assertEqual(state.state["status"], "completed")
            self.assertEqual(state.state["current_date"], "2024-01-04")
            date_file = (root / "current-date.txt").read_text(encoding="utf-8").strip()
            self.assertEqual(date_file, "2024-01-04")

    def test_export_run_state_restores_current_date_txt_if_missing_on_resume(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "state"
            state = ExportRunState(
                root,
                None,
                backup_dir=Path(tmpdir) / "backup",
                start_date=date(2024, 2, 1),
                end_date=date(2024, 2, 5),
                name_mode="patient",
                zip_mode="archive",
                orthanc_base_url="http://127.0.0.1:8042",
            )
            state.set_current_date(date(2024, 2, 3))
            (root / "current-date.txt").unlink()

            reopened = ExportRunState(
                root,
                None,
                backup_dir=Path(tmpdir) / "backup",
                start_date=date(2024, 2, 1),
                end_date=date(2024, 2, 5),
                name_mode="patient",
                zip_mode="archive",
                orthanc_base_url="http://127.0.0.1:8042",
            )

            self.assertTrue((root / "current-date.txt").exists())
