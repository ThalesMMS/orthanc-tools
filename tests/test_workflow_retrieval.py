import subprocess
import tempfile
import unittest
from argparse import Namespace
from datetime import date
from pathlib import Path
from unittest import mock

from orthanc_tools.workflows.retrieval import (
    BACKUP_MATERIAL_STUDY_STATE_EXTRA_KEYS,
    ImportOutcome,
    RemoteStudy,
    RetrievalPlan,
    RemoteStudyWorkflowMixin,
    compare_manifests,
    fetch_remote_manifest_exact,
    material_study_state,
    read_instance_manifest,
)


class FakeManifestClient:
    def __init__(self) -> None:
        self.deleted_queries: list[str] = []

    def create_remote_query(self, modality: str, level: str, query_fields: dict[str, str], normalize=None) -> str:
        self.last_remote_query = (modality, level, query_fields, normalize)
        return "study-query"

    def create_child_query(self, query_id: str, answer_id: str, child_level: str, query_fields: dict[str, str]) -> str:
        return {
            ("study-query", "study-answer", "instances"): "instance-query",
            ("study-query", "study-answer", "series"): "series-query",
            ("series-query", "series-answer", "instances"): "series-instance-query",
        }[(query_id, answer_id, child_level)]

    def get_query_answers(self, query_id: str) -> list[str]:
        return {
            "study-query": ["study-answer"],
            "instance-query": ["instance-answer"],
            "series-query": ["series-answer"],
            "series-instance-query": ["series-instance-answer"],
            "missing-series-query": ["bad-answer"],
        }[query_id]

    def get_query_answer_content(self, query_id: str, answer_id: str) -> dict[str, str]:
        return {
            ("study-query", "study-answer"): {"StudyInstanceUID": "1.2.3"},
            ("instance-query", "instance-answer"): {"SOPInstanceUID": "1.2.3.4"},
            ("series-query", "series-answer"): {"SeriesInstanceUID": "series-1"},
            ("series-instance-query", "series-instance-answer"): {
                "SOPInstanceUID": "1.2.3.4",
                "SOPClassUID": "1.2.840",
            },
            ("missing-series-query", "bad-answer"): {"SOPInstanceUID": "1.2.3.4"},
        }[(query_id, answer_id)]

    def delete_query(self, query_id: str) -> None:
        self.deleted_queries.append(query_id)


class FakeManifestState:
    owner = None

    def __init__(self, root: Path) -> None:
        self.root = root

    def day_manifest_dir(self, day) -> Path:
        path = self.root / day.isoformat() / "remote-manifests"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def log(self, message: str) -> None:
        self.last_log = message


class ConcreteRemoteStudyWorkflow(RemoteStudyWorkflowMixin):
    def __init__(self, root: Path) -> None:
        self.args = Namespace(remote_name="REMOTE", allow_heuristic_fallback=True)
        self.client = object()
        self.state = FakeManifestState(root)
        self.exact_manifest_failures: dict[str, str] = {}

    def _manifest_query_fields(self) -> dict[str, str]:
        return {"PatientID": ""}


class WorkflowRetrievalTests(unittest.TestCase):
    def test_retrieval_dataclasses_construct_expected_defaults(self) -> None:
        plan = RetrievalPlan(mode="study", missing=[{"sop_uid": "1.2.3"}])
        outcome = ImportOutcome()
        study = RemoteStudy(study_uid="1.2.840", patient_birth_date="19700101")

        self.assertEqual(plan.mode, "study")
        self.assertEqual(plan.missing, [{"sop_uid": "1.2.3"}])
        self.assertEqual(outcome.notes, [])
        self.assertEqual(study.patient_birth_date, "19700101")
        self.assertEqual(study.remote_series_count, None)

    def test_material_study_state_supports_extra_keys(self) -> None:
        state = {
            "status": "pending",
            "manifest_mode": "exact",
            "zip_filename": "study.zip",
            "zip_bytes": 123,
            "ignored": "value",
        }

        snapshot = material_study_state(state, extra_keys=BACKUP_MATERIAL_STUDY_STATE_EXTRA_KEYS)

        self.assertEqual(snapshot["status"], "pending")
        self.assertEqual(snapshot["zip_filename"], "study.zip")
        self.assertNotIn("ignored", snapshot)

    def test_compare_manifests_counts_series_and_instance_drift(self) -> None:
        diff = compare_manifests(
            {"series-1": {"a", "b"}, "series-2": {"c"}},
            {"series-1": {"a", "x"}, "series-3": {"z"}},
        )

        self.assertEqual(diff.missing_series, 1)
        self.assertEqual(diff.extra_series, 1)
        self.assertEqual(diff.missing_instances, 1)
        self.assertEqual(diff.extra_instances, 1)
        self.assertFalse(diff.exact)
        self.assertTrue(diff.needs_replace)

    def test_read_instance_manifest_returns_none_when_series_uid_missing(self) -> None:
        client = FakeManifestClient()

        manifest = read_instance_manifest(client, "missing-series-query")

        self.assertIsNone(manifest)

    def test_fetch_remote_manifest_exact_falls_back_to_series_walk(self) -> None:
        client = FakeManifestClient()

        manifest = fetch_remote_manifest_exact(
            client,
            "REMOTE",
            "1.2.3",
            study_query_fields={
                "PatientID": "",
                "PatientName": "",
                "StudyDate": "",
                "NumberOfStudyRelatedSeries": "",
                "NumberOfStudyRelatedInstances": "",
            },
        )

        self.assertEqual(
            manifest,
            {
                "series-1": [
                    {
                        "series_uid": "series-1",
                        "sop_uid": "1.2.3.4",
                        "sop_class_uid": "1.2.840",
                    }
                ]
            },
        )
        self.assertEqual(
            client.last_remote_query,
            (
                "REMOTE",
                "Study",
                {
                    "PatientID": "",
                    "PatientName": "",
                    "StudyDate": "",
                    "NumberOfStudyRelatedSeries": "",
                    "NumberOfStudyRelatedInstances": "",
                    "StudyInstanceUID": "1.2.3",
                },
                None,
            ),
        )
        self.assertIn("study-query", client.deleted_queries)
        self.assertIn("instance-query", client.deleted_queries)
        self.assertIn("series-query", client.deleted_queries)
        self.assertIn("series-instance-query", client.deleted_queries)

    def test_exact_manifest_cache_write_errors_are_not_treated_as_remote_failures(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workflow = ConcreteRemoteStudyWorkflow(Path(tmpdir))
            study = RemoteStudy(study_uid="1.2.3")
            state: dict = {}
            with mock.patch(
                "orthanc_tools.workflows.retrieval.fetch_remote_manifest_exact",
                return_value={"series-1": [{"series_uid": "series-1", "sop_uid": "sop-1"}]},
            ), mock.patch(
                "orthanc_tools.workflows.retrieval.atomic_write_json",
                side_effect=OSError("disk full"),
            ):
                with self.assertRaises(OSError):
                    workflow.load_or_fetch_remote_manifest(date(2024, 1, 1), study, state)

        self.assertEqual(workflow.exact_manifest_failures, {})
        self.assertNotIn("manifest_error", state)

    def test_manifest_diff_properties_report_drift_correctly(self) -> None:
        from orthanc_tools.workflows.retrieval import ManifestDiff

        exact = ManifestDiff()
        self.assertTrue(exact.exact)
        self.assertFalse(exact.needs_replace)

        missing_only = ManifestDiff(missing_series=1, missing_instances=3)
        self.assertFalse(missing_only.exact)
        self.assertFalse(missing_only.needs_replace)

        extra = ManifestDiff(extra_series=1, extra_instances=2)
        self.assertFalse(extra.exact)
        self.assertTrue(extra.needs_replace)

    def test_manifest_diff_summary_includes_all_counts(self) -> None:
        from orthanc_tools.workflows.retrieval import ManifestDiff

        diff = ManifestDiff(missing_series=1, extra_series=2, missing_instances=3, extra_instances=4)
        summary = diff.summary()

        self.assertIn("-1", summary)
        self.assertIn("+2", summary)
        self.assertIn("-3", summary)
        self.assertIn("+4", summary)

    def test_study_state_label_includes_uid_and_optional_fields(self) -> None:
        from orthanc_tools.workflows.retrieval import StudyState

        state = StudyState(answer_id="a1", study_uid="1.2.840.113619.1.9")
        label = state.label()
        self.assertIn("1.2.840", label)

        full = StudyState(
            answer_id="a2",
            study_uid="1.2.840.113619.1.9",
            study_date="20240101",
            patient_id="P-001",
        )
        label_full = full.label()
        self.assertIn("20240101", label_full)
        self.assertIn("P-001", label_full)

    def test_local_study_summary_holds_counts(self) -> None:
        from orthanc_tools.workflows.retrieval import LocalStudySummary

        summary = LocalStudySummary(orthanc_id="study-1", series_count=3, instance_count=12)
        self.assertEqual(summary.orthanc_id, "study-1")
        self.assertEqual(summary.series_count, 3)
        self.assertEqual(summary.instance_count, 12)

        empty = LocalStudySummary(orthanc_id="x", series_count=None, instance_count=None)
        self.assertIsNone(empty.series_count)

    def test_material_study_state_without_extra_keys_returns_only_common_keys(self) -> None:
        state = {
            "status": "complete",
            "manifest_mode": "exact",
            "local_instance_count": 5,
            "zip_filename": "study.zip",
            "ignored_key": "should_not_appear",
        }

        snapshot = material_study_state(state)

        self.assertEqual(snapshot["status"], "complete")
        self.assertEqual(snapshot["local_instance_count"], 5)
        self.assertNotIn("zip_filename", snapshot)
        self.assertNotIn("ignored_key", snapshot)

    def test_compare_manifests_exact_when_identical(self) -> None:
        diff = compare_manifests(
            {"s1": {"a", "b"}, "s2": {"c"}},
            {"s1": {"a", "b"}, "s2": {"c"}},
        )

        self.assertTrue(diff.exact)
        self.assertEqual(diff.missing_series, 0)
        self.assertEqual(diff.missing_instances, 0)

    def test_compare_manifests_handles_empty_manifests(self) -> None:
        diff_both_empty = compare_manifests({}, {})
        self.assertTrue(diff_both_empty.exact)

        diff_all_missing = compare_manifests({"s1": {"a"}}, {})
        self.assertEqual(diff_all_missing.missing_series, 1)
        self.assertFalse(diff_all_missing.exact)

    def test_pick_single_remote_study_answer_returns_matching_answer(self) -> None:
        from orthanc_tools.workflows.retrieval import pick_single_remote_study_answer

        client = FakeManifestClient()
        answer_id = pick_single_remote_study_answer(client, "study-query", "1.2.3")

        self.assertEqual(answer_id, "study-answer")

    def test_pick_single_remote_study_answer_raises_when_no_match(self) -> None:
        from orthanc_tools.workflows.retrieval import pick_single_remote_study_answer

        client = FakeManifestClient()
        with self.assertRaises(RuntimeError) as ctx:
            pick_single_remote_study_answer(client, "study-query", "9.9.9.9")

        self.assertIn("no exact answer", str(ctx.exception))

    def test_read_instance_manifest_accumulates_instances_by_series(self) -> None:
        class DirectClient:
            def get_query_answers(self, query_id: str) -> list[str]:
                return ["a1", "a2"]

            def get_query_answer_content(self, query_id: str, answer_id: str) -> dict:
                return {
                    "a1": {"SeriesInstanceUID": "series-A", "SOPInstanceUID": "sop-1", "SOPClassUID": "class-1"},
                    "a2": {"SeriesInstanceUID": "series-A", "SOPInstanceUID": "sop-2"},
                }[answer_id]

        manifest = read_instance_manifest(DirectClient(), "any-query")

        self.assertIsNotNone(manifest)
        self.assertIn("series-A", manifest)
        sop_uids = [r["sop_uid"] for r in manifest["series-A"]]
        self.assertIn("sop-1", sop_uids)
        self.assertIn("sop-2", sop_uids)

    def test_run_subprocess_redacts_uid_tokens_on_timeout(self) -> None:
        long_uid = "1.2.840.113619.2.55.3.604688433.781.1599123456.467"
        with tempfile.TemporaryDirectory() as tmpdir:
            workflow = ConcreteRemoteStudyWorkflow(Path(tmpdir))
            with mock.patch(
                "orthanc_tools.workflows.retrieval.subprocess.run",
                side_effect=subprocess.TimeoutExpired("getscu", 3),
            ):
                with self.assertRaises(RuntimeError) as ctx:
                    workflow.run_subprocess(["getscu", "-k", f"StudyInstanceUID={long_uid}"], timeout=3)

        message = str(ctx.exception)
        self.assertNotIn(long_uid, message)
        self.assertIn("StudyInstanceUID=1.2.840", message)
        self.assertIn("...", message)

    def test_run_subprocess_redacts_uid_tokens_on_failure(self) -> None:
        long_uid = "1.2.840.113619.2.55.3.604688433.781.1599123456.468"
        with tempfile.TemporaryDirectory() as tmpdir:
            workflow = ConcreteRemoteStudyWorkflow(Path(tmpdir))
            with mock.patch(
                "orthanc_tools.workflows.retrieval.subprocess.run",
                return_value=mock.Mock(returncode=1, stdout="out", stderr="err"),
            ):
                with self.assertRaises(RuntimeError) as ctx:
                    workflow.run_subprocess(["getscu", "-k", f"SOPInstanceUID={long_uid}"], timeout=3)

        message = str(ctx.exception)
        self.assertNotIn(long_uid, message)
        self.assertIn("SOPInstanceUID=1.2.840", message)
        self.assertIn("out\nerr", message)
