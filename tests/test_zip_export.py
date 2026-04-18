import json
import tempfile
import unittest
import zipfile
from argparse import Namespace
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from unittest import mock

from orthanc_tools.orthanc_api import OrthancNetworkError
from orthanc_tools.zip_export import (
    ZIP_MANIFEST_NAME,
    BackupZipMixin,
    ExportWorkflowMixin,
    ZipValidationError,
    ascii_slug,
    build_patient_base,
    format_duration,
    format_size,
    read_zip_manifest,
    truncate_with_hash,
    validate_zip_file,
)


@dataclass
class DummyStudy:
    patient_name: str
    patient_birth_date: str
    study_date: str
    study_uid: str = "1.2.3.4"
    patient_id: str = "P1"
    description: str = "CT"
    accession_number: str = "ACC001"
    remote_series_count: int | None = None
    remote_instance_count: int | None = None


class ZipExportTests(unittest.TestCase):
    def test_ascii_slug_normalizes_text(self) -> None:
        self.assertEqual(ascii_slug("João da Silva"), "Joao_da_Silva")

    def test_truncate_with_hash_keeps_limit(self) -> None:
        truncated = truncate_with_hash("x" * 300, limit=32)
        self.assertLessEqual(len(truncated), 32)

    def test_build_patient_base_uses_patient_fields(self) -> None:
        study = DummyStudy("Maria", "19800101", "20240220")
        self.assertTrue(build_patient_base(study, date(2024, 2, 20)).startswith("Maria_19800101_20240220"))

    def test_validate_zip_file_and_read_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "study.zip"
            manifest = {"study_uid": "1.2.3", "manifest_sha1": "abc"}
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("images/1.dcm", b"dicom")
                archive.writestr(ZIP_MANIFEST_NAME, json.dumps(manifest))
            validate_zip_file(path)
            self.assertEqual(read_zip_manifest(path), manifest)

    def test_validate_zip_file_raises_specific_validation_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "study.zip"
            path.write_bytes(b"not a zip")

            with self.assertRaises(ZipValidationError):
                validate_zip_file(path)

    def test_validate_zip_file_raises_when_file_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "nonexistent.zip"
            with self.assertRaises(ZipValidationError):
                validate_zip_file(path)

    def test_validate_zip_file_raises_on_empty_zip(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "empty.zip"
            path.write_bytes(b"")
            with self.assertRaises(ZipValidationError):
                validate_zip_file(path)

    def test_read_zip_manifest_returns_none_when_no_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "no_manifest.zip"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("data/1.dcm", b"data")
            result = read_zip_manifest(path)
        self.assertIsNone(result)

    def test_read_zip_manifest_returns_none_for_non_zip(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "not_a_zip.zip"
            path.write_bytes(b"not a zip file")
            result = read_zip_manifest(path)
        self.assertIsNone(result)

    def test_read_zip_manifest_returns_none_for_malformed_json(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "bad_manifest.zip"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr(ZIP_MANIFEST_NAME, "{")

            result = read_zip_manifest(path)

        self.assertIsNone(result)

    def test_format_size_renders_human_readable_sizes(self) -> None:
        self.assertEqual(format_size(0), "0B")
        self.assertEqual(format_size(512), "512B")
        self.assertEqual(format_size(1024), "1.0KB")
        self.assertEqual(format_size(1024 * 1024), "1.0MB")
        self.assertEqual(format_size(1024 * 1024 * 1024), "1.0GB")
        self.assertIn("TB", format_size(1024 ** 4))

    def test_format_size_negative_treated_as_zero(self) -> None:
        self.assertEqual(format_size(-100), "0B")

    def test_format_duration_renders_human_readable_times(self) -> None:
        self.assertEqual(format_duration(0.0), "0.00s")
        self.assertEqual(format_duration(0.5), "0.50s")
        self.assertEqual(format_duration(1.0), "1.0s")
        self.assertEqual(format_duration(59.9), "59.9s")
        result_90 = format_duration(90.0)
        self.assertIn("m", result_90)
        result_3600 = format_duration(3600.0)
        self.assertIn("h", result_3600)

    def test_format_duration_negative_treated_as_zero(self) -> None:
        self.assertEqual(format_duration(-5.0), "0.00s")

    def test_ascii_slug_replaces_special_chars_and_strips_edges(self) -> None:
        self.assertEqual(ascii_slug("  test  "), "test")
        self.assertEqual(ascii_slug("hello^world"), "hello_world")
        self.assertEqual(ascii_slug(""), "UNKNOWN")
        self.assertEqual(ascii_slug("___"), "UNKNOWN")

    def test_truncate_with_hash_keeps_short_strings_intact(self) -> None:
        short = "short"
        self.assertEqual(truncate_with_hash(short, limit=32), short)

    def test_truncate_with_hash_adds_digest_suffix(self) -> None:
        long_text = "a" * 300
        result = truncate_with_hash(long_text, limit=50)
        self.assertLessEqual(len(result), 50)
        self.assertIn("_", result)


class FakeState:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.meta: dict = {"stats": {}}
        self._log_messages: list[str] = []

    def day_rejected_dir(self, day: date) -> Path:
        path = self.root / "rejected"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def day_manifest_dir(self, day: date) -> Path:
        path = self.root / "manifests"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def log(self, message: str) -> None:
        self._log_messages.append(message)

    def save_meta(self) -> None:
        pass

    def lookup_local_study(self, study_uid: str) -> dict | None:
        return None


class ConcreteBackupZipMixin(BackupZipMixin):
    """Minimal concrete implementation of BackupZipMixin for testing."""

    def __init__(self, args: Namespace, state: FakeState, client: object, owner: object = None) -> None:
        self.args = args
        self.state = state
        self.client = client
        self.owner = owner
        self.backup_dir = getattr(args, "backup_dir", state.root / "backup")


class FakeExportLogger:
    def __init__(self) -> None:
        self.infos: list[str] = []
        self.errors: list[str] = []

    def info(self, message: str) -> None:
        self.infos.append(message)

    def error(self, message: str) -> None:
        self.errors.append(message)


class NetworkFailingExportClient:
    def __init__(self) -> None:
        self.calls = 0

    def download_study_archive(self, study_id: str, output_path: Path) -> dict:
        del output_path
        self.calls += 1
        raise OrthancNetworkError("GET", f"/studies/{study_id}/archive", "temporary failure")


class ConcreteExportWorkflowMixin(ExportWorkflowMixin):
    def __init__(self, root: Path, client: NetworkFailingExportClient) -> None:
        self.args = Namespace(retries=2, retry_delay=0, zip_mode="archive")
        self.client = client
        self.owner = None
        self.backup_dir = root
        self.logger = FakeExportLogger()


class EmptyStudyExportWorkflowMixin(ExportWorkflowMixin):
    def __init__(self, root: Path) -> None:
        self.args = Namespace(retries=2, retry_delay=0, zip_mode="stored")
        self.client = object()
        self.owner = None
        self.backup_dir = root
        self.logger = FakeExportLogger()
        self.build_calls = 0

    def build_local_stored_zip(self, day: date, entry: dict, output_path: Path) -> dict:
        del day, output_path
        self.build_calls += 1
        raise RuntimeError(f"Study {entry['study_uid']} has no instances to export")


class BackupZipMixinTests(unittest.TestCase):
    def make_mixin(self, tmpdir: str, name: str = "patient", zip_mode: str = "archive") -> ConcreteBackupZipMixin:
        root = Path(tmpdir)
        state = FakeState(root)
        args = Namespace(
            name=name,
            zip_mode=zip_mode,
            backup_dir=root / "backup",
            settle_seconds=0,
        )
        client = mock.MagicMock()
        return ConcreteBackupZipMixin(args, state, client)

    def test_assign_zip_filenames_uses_patient_name_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            mixin = self.make_mixin(tmpdir, name="patient")
            study = DummyStudy("Alice", "19800101", "20240101")
            status: dict = {"studies": {study.study_uid: {}}}

            mixin.assign_zip_filenames(date(2024, 1, 1), [study], status)

            filename = status["studies"][study.study_uid].get("zip_filename")
            self.assertIsNotNone(filename)
            self.assertTrue(filename.endswith(".zip"))
            self.assertIn("Alice", filename)

    def test_assign_zip_filenames_uses_uid_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            mixin = self.make_mixin(tmpdir, name="uid")
            study = DummyStudy("Alice", "19800101", "20240101")
            status: dict = {"studies": {study.study_uid: {}}}

            mixin.assign_zip_filenames(date(2024, 1, 1), [study], status)

            filename = status["studies"][study.study_uid].get("zip_filename")
            self.assertIsNotNone(filename)
            self.assertTrue(filename.endswith(".zip"))

    def test_assign_zip_filenames_resolves_collisions_with_suffix(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            mixin = self.make_mixin(tmpdir, name="patient")
            study_a = DummyStudy("Twins", "19901010", "20240101", study_uid="1.1.1")
            study_b = DummyStudy("Twins", "19901010", "20240101", study_uid="2.2.2")
            status: dict = {"studies": {"1.1.1": {}, "2.2.2": {}}}

            mixin.assign_zip_filenames(date(2024, 1, 1), [study_a, study_b], status)

            fn_a = status["studies"]["1.1.1"]["zip_filename"]
            fn_b = status["studies"]["2.2.2"]["zip_filename"]
            self.assertNotEqual(fn_a, fn_b)

    def test_assign_zip_filenames_preserves_existing_assignment(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            mixin = self.make_mixin(tmpdir)
            study = DummyStudy("Alice", "19800101", "20240101")
            status: dict = {"studies": {study.study_uid: {"zip_filename": "already-assigned.zip"}}}

            mixin.assign_zip_filenames(date(2024, 1, 1), [study], status)

            self.assertEqual(status["studies"][study.study_uid]["zip_filename"], "already-assigned.zip")

    def test_existing_zip_matches_validates_study_uid_and_complete_flag(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            mixin = self.make_mixin(tmpdir)
            study = DummyStudy("Alice", "19800101", "20240101")
            state: dict = {"manifest_mode": "exact", "rejected_instances": {}}

            good_manifest = {
                "study_uid": study.study_uid,
                "backup_complete": True,
                "rejected_count": 0,
                "accounting_mode": "exact",
            }
            self.assertTrue(mixin.existing_zip_matches(study, state, good_manifest))

            bad_uid = {**good_manifest, "study_uid": "9.9.9.9"}
            self.assertFalse(mixin.existing_zip_matches(study, state, bad_uid))

            incomplete = {**good_manifest, "backup_complete": False}
            self.assertFalse(mixin.existing_zip_matches(study, state, incomplete))

            self.assertFalse(mixin.existing_zip_matches(study, state, None))

    def test_existing_zip_matches_checks_rejected_count(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            mixin = self.make_mixin(tmpdir)
            study = DummyStudy("Alice", "19800101", "20240101")
            state: dict = {"manifest_mode": "exact", "rejected_instances": {"sop-1": {}, "sop-2": {}}}

            manifest = {
                "study_uid": study.study_uid,
                "backup_complete": True,
                "rejected_count": 2,
                "accounting_mode": "exact",
            }
            self.assertTrue(mixin.existing_zip_matches(study, state, manifest))

            wrong_count = {**manifest, "rejected_count": 1}
            self.assertFalse(mixin.existing_zip_matches(study, state, wrong_count))

    def test_rejected_entries_for_study_returns_sorted_entries(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            mixin = self.make_mixin(tmpdir)
            state = {
                "rejected_instances": {
                    "sop-b": {"series_uid": "series-1", "sop_uid": "sop-b", "last_error": "err"},
                    "sop-a": {"series_uid": "series-1", "sop_uid": "sop-a", "last_error": "err"},
                }
            }

            entries = mixin.rejected_entries_for_study(state)

        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0]["sop_uid"], "sop-a")
        self.assertEqual(entries[1]["sop_uid"], "sop-b")

    def test_rejected_entries_for_study_returns_empty_on_invalid_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            mixin = self.make_mixin(tmpdir)
            self.assertEqual(mixin.rejected_entries_for_study({}), [])
            self.assertEqual(mixin.rejected_entries_for_study({"rejected_instances": "not-a-dict"}), [])

    def test_mark_absent_studies_sets_absent_status_for_missing_studies(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            mixin = self.make_mixin(tmpdir)
            study_states = {
                "study-existing": {"status": "pending", "backup_complete": False},
                "study-gone": {"status": "pending", "backup_complete": False},
                "study-already-backed-up": {"status": "complete", "backup_complete": True},
            }

            mixin._mark_absent_studies(date(2024, 1, 1), {"study-existing"}, study_states)

        self.assertEqual(study_states["study-gone"]["status"], "absent")
        self.assertFalse(study_states["study-gone"]["required"])
        # Studies that are already backed up should keep their backup_complete status
        self.assertTrue(study_states["study-already-backed-up"]["backup_complete"])
        # study-existing should not be marked absent
        self.assertEqual(study_states["study-existing"]["status"], "pending")

    def test_study_state_is_complete_checks_backup_complete_flag(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            mixin = self.make_mixin(tmpdir)

        self.assertTrue(mixin._study_state_is_complete({"backup_complete": True}))
        self.assertFalse(mixin._study_state_is_complete({"backup_complete": False}))
        self.assertFalse(mixin._study_state_is_complete({}))

    def test_study_state_uses_heuristic_checks_status_and_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            mixin = self.make_mixin(tmpdir)

        self.assertTrue(mixin._study_state_uses_heuristic({"status": "heuristic-complete"}))
        self.assertTrue(mixin._study_state_uses_heuristic({"manifest_mode": "heuristic"}))
        self.assertFalse(mixin._study_state_uses_heuristic({"status": "complete", "manifest_mode": "exact"}))

    def test_build_backup_manifest_includes_sha1_hash(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            mixin = self.make_mixin(tmpdir, zip_mode="archive")
            mixin.args.name = "patient"
            study = DummyStudy("Alice", "19800101", "20240101")
            state = {
                "manifest_mode": "exact",
                "local_instance_count": 5,
                "accounted_count": 5,
                "missing_count": 0,
                "zip_filename": "study.zip",
                "remote_exact_instance_count": 5,
                "heuristic_target": None,
            }

            manifest = mixin.build_backup_manifest(date(2024, 1, 1), study, state, rejected_entries=[])

        self.assertIn("manifest_sha1", manifest)
        self.assertEqual(manifest["script"], "backup_remote_to_zip.py")
        self.assertEqual(manifest["study_uid"], study.study_uid)
        self.assertTrue(manifest["backup_complete"])
        self.assertEqual(manifest["rejected_count"], 0)
        sha1 = manifest["manifest_sha1"]
        self.assertIsInstance(sha1, str)
        self.assertEqual(len(sha1), 40)

    def test_backup_day_dir_creates_expected_directory_structure(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            mixin = self.make_mixin(tmpdir)
            mixin.backup_dir = Path(tmpdir) / "backup"

            day_dir = mixin.backup_day_dir(date(2024, 3, 15))

            self.assertTrue(day_dir.exists())
            self.assertTrue(str(day_dir).endswith("2024/03/15"))

    def test_final_zip_path_and_part_path_use_correct_names(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            mixin = self.make_mixin(tmpdir)
            mixin.backup_dir = Path(tmpdir) / "backup"
            day = date(2024, 1, 1)

            final = mixin.final_zip_path(day, "study.zip")
            part = mixin.zip_part_path(final)

        self.assertTrue(str(final).endswith("study.zip"))
        self.assertTrue(str(part).endswith("study.zip.part"))


class ExportWorkflowMixinTests(unittest.TestCase):
    def test_export_one_study_retries_network_errors(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            client = NetworkFailingExportClient()
            mixin = ConcreteExportWorkflowMixin(root, client)
            entry = {
                "study_uid": "1.2.3",
                "filename": "study.zip",
                "orthanc_id": "orthanc-study",
                "status": "pending",
                "attempts": 0,
                "bytes": 0,
                "error": "",
            }
            status = {"studies": {"1.2.3": entry}}

            ok = mixin.export_one_study(
                date(2024, 1, 1),
                root,
                status,
                entry,
                root / "status.json",
                root / "progress.tsv",
            )

        self.assertFalse(ok)
        self.assertEqual(client.calls, 2)
        self.assertEqual(entry["attempts"], 2)
        self.assertEqual(entry["status"], "error")
        self.assertIn("temporary failure", entry["error"])

    def test_export_one_study_handles_empty_study_error_without_reraising(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            mixin = EmptyStudyExportWorkflowMixin(root)
            entry = {
                "study_uid": "1.2.3",
                "filename": "study.zip",
                "orthanc_id": "orthanc-study",
                "status": "pending",
                "attempts": 0,
                "bytes": 0,
                "error": "",
            }
            status = {"studies": {"1.2.3": entry}}

            ok = mixin.export_one_study(
                date(2024, 1, 1),
                root,
                status,
                entry,
                root / "status.json",
                root / "progress.tsv",
            )

        self.assertFalse(ok)
        self.assertEqual(mixin.build_calls, 1)
        self.assertEqual(entry["status"], "error")
        self.assertIn("has no instances to export", entry["error"])
