from __future__ import annotations

import unittest
from unittest import mock

from orthanc_tools.workflows.mirror_retrieval import StudyState

from tests.workflow_mirror_retrieval_fakes import ConcreteMirrorWorkflow, _make_args


class CheckConnectivityTests(unittest.TestCase):
    def test_sets_phase_and_logs_orthanc_info(self) -> None:
        wf = ConcreteMirrorWorkflow()
        wf.client._system_info = {"Name": "MyOrthanc", "Version": "1.9.0"}

        with mock.patch("orthanc_tools.workflows.mirror_retrieval.shutil.which", return_value="/usr/bin/getscu"):
            wf.check_connectivity()

        self.assertEqual(wf.phase, "connecting")
        self.assertTrue(any("MyOrthanc" in m for m in wf.dashboard.messages))
        self.assertTrue(any("1.9.0" in m for m in wf.dashboard.messages))

    def test_logs_base_url(self) -> None:
        wf = ConcreteMirrorWorkflow()
        wf.client._system_info = {"Name": "Orthanc", "Version": "1.0"}
        wf.client.settings.base_url = "http://host:8042"

        with mock.patch("orthanc_tools.workflows.mirror_retrieval.shutil.which", return_value="/usr/bin/getscu"):
            wf.check_connectivity()

        self.assertTrue(any("http://host:8042" in m for m in wf.dashboard.messages))

    def test_raises_when_getscu_not_found_and_retrieve_method_is_get(self) -> None:
        wf = ConcreteMirrorWorkflow(args=_make_args(retrieve_method="get"))

        with mock.patch("orthanc_tools.workflows.mirror_retrieval.shutil.which", return_value=None):
            with self.assertRaises(RuntimeError) as ctx:
                wf.check_connectivity()

        self.assertIn("getscu", str(ctx.exception))

    def test_no_error_when_getscu_not_found_but_method_is_not_get(self) -> None:
        wf = ConcreteMirrorWorkflow(args=_make_args(retrieve_method="move"))
        wf.client._system_info = {"Name": "Orthanc", "Version": None}

        with mock.patch("orthanc_tools.workflows.mirror_retrieval.shutil.which", return_value=None):
            wf.check_connectivity()

    def test_orthanc_name_falls_back_to_orthanc_when_missing(self) -> None:
        wf = ConcreteMirrorWorkflow()
        wf.client._system_info = {}

        with mock.patch("orthanc_tools.workflows.mirror_retrieval.shutil.which", return_value="/usr/bin/getscu"):
            wf.check_connectivity()

        self.assertTrue(any("Orthanc" in m for m in wf.dashboard.messages))


class LoadRemoteInventoryTests(unittest.TestCase):
    def _make_study_content(
        self,
        study_uid: str = "1.2.3",
        patient_id: str = "P001",
        study_date: str = "20240101",
    ) -> dict:
        return {
            "StudyInstanceUID": study_uid,
            "PatientID": patient_id,
            "PatientName": "Doe^John",
            "StudyDate": study_date,
            "StudyDescription": "CT Chest",
            "NumberOfStudyRelatedSeries": "2",
            "NumberOfStudyRelatedInstances": "10",
        }

    def test_populates_studies_from_remote_query(self) -> None:
        wf = ConcreteMirrorWorkflow()
        wf.client._query_answer_pairs["remote-query-1"] = [
            ("ans-1", self._make_study_content("1.2.3")),
        ]

        wf.load_remote_inventory()

        self.assertEqual(len(wf.studies), 1)
        study = wf.studies[0]
        self.assertEqual(study.study_uid, "1.2.3")
        self.assertEqual(study.patient_id, "P001")
        self.assertEqual(study.remote_series_count, 2)
        self.assertEqual(study.remote_instance_count, 10)
        self.assertEqual(wf.remote_query_id, "remote-query-1")

    def test_skips_answers_with_no_study_uid(self) -> None:
        wf = ConcreteMirrorWorkflow()
        wf.client._query_answer_pairs["remote-query-1"] = [
            ("ans-bad", {"PatientID": "P002"}),
            ("ans-good", self._make_study_content("1.2.3")),
        ]

        wf.load_remote_inventory()

        self.assertEqual(len(wf.studies), 1)
        self.assertEqual(wf.studies[0].study_uid, "1.2.3")
        self.assertTrue(any("missing" in m.lower() for m in wf.dashboard.messages))

    def test_skips_duplicate_study_uids(self) -> None:
        wf = ConcreteMirrorWorkflow()
        wf.client._query_answer_pairs["remote-query-1"] = [
            ("ans-1", self._make_study_content("1.2.3")),
            ("ans-2", self._make_study_content("1.2.3")),
        ]

        wf.load_remote_inventory()

        self.assertEqual(len(wf.studies), 1)
        self.assertTrue(any("duplicate" in m.lower() or "skipping" in m.lower() for m in wf.dashboard.messages))

    def test_raises_on_empty_remote_when_flag_not_set(self) -> None:
        wf = ConcreteMirrorWorkflow(args=_make_args(allow_empty_remote=False))
        wf.client._query_answer_pairs["remote-query-1"] = []

        with self.assertRaises(RuntimeError) as ctx:
            wf.load_remote_inventory()

        self.assertIn("zero studies", str(ctx.exception))

    def test_does_not_raise_on_empty_remote_when_flag_set(self) -> None:
        wf = ConcreteMirrorWorkflow(args=_make_args(allow_empty_remote=True))
        wf.client._query_answer_pairs["remote-query-1"] = []

        wf.load_remote_inventory()

        self.assertEqual(len(wf.studies), 0)

    def test_respects_limit_studies(self) -> None:
        wf = ConcreteMirrorWorkflow(args=_make_args(limit_studies=1))
        wf.client._query_answer_pairs["remote-query-1"] = [
            ("ans-1", self._make_study_content("1.2.3")),
            ("ans-2", self._make_study_content("1.2.4")),
        ]

        wf.load_remote_inventory()

        self.assertEqual(len(wf.studies), 1)

    def test_limit_studies_counts_only_valid_unique_studies(self) -> None:
        wf = ConcreteMirrorWorkflow(args=_make_args(limit_studies=2))
        wf.client._query_answer_pairs["remote-query-1"] = [
            ("ans-missing", {"PatientID": "P002"}),
            ("ans-1", self._make_study_content("1.2.3")),
            ("ans-duplicate", self._make_study_content("1.2.3")),
            ("ans-2", self._make_study_content("1.2.4")),
            ("ans-3", self._make_study_content("1.2.5")),
        ]

        wf.load_remote_inventory()

        self.assertEqual([study.study_uid for study in wf.studies], ["1.2.3", "1.2.4"])

    def test_logs_study_count(self) -> None:
        wf = ConcreteMirrorWorkflow()
        wf.client._query_answer_pairs["remote-query-1"] = [
            ("ans-1", self._make_study_content("1.2.3")),
        ]

        wf.load_remote_inventory()

        self.assertIn("Remote query returned 1 studies", wf.dashboard.messages)


class GetLocalSummaryTests(unittest.TestCase):
    def test_returns_none_when_study_not_found_locally(self) -> None:
        wf = ConcreteMirrorWorkflow()
        wf.client._local_study = None

        result = wf.get_local_summary("1.2.3")

        self.assertIsNone(result)

    def test_returns_summary_with_counts_when_found(self) -> None:
        wf = ConcreteMirrorWorkflow()
        wf.client._local_study = {"ID": "orthanc-id-1"}
        wf.client._study_stats = {"CountSeries": "3", "CountInstances": "12"}

        result = wf.get_local_summary("1.2.3")

        self.assertIsNotNone(result)
        self.assertEqual(result.orthanc_id, "orthanc-id-1")
        self.assertEqual(result.series_count, 3)
        self.assertEqual(result.instance_count, 12)

    def test_refresh_propagates_none_when_missing(self) -> None:
        wf = ConcreteMirrorWorkflow()
        wf.client._local_study = None
        study = StudyState(answer_id="a1", study_uid="1.2.3", local_id="old-id")

        result = wf.refresh_local_summary(study)

        self.assertIsNone(result)
        self.assertIsNone(study.local_id)
        self.assertIsNone(study.local_series_count)
        self.assertIsNone(study.local_instance_count)

    def test_refresh_updates_study_with_local_counts(self) -> None:
        wf = ConcreteMirrorWorkflow()
        wf.client._local_study = {"ID": "orthanc-id-99"}
        wf.client._study_stats = {"CountSeries": "5", "CountInstances": "20"}
        study = StudyState(answer_id="a1", study_uid="1.2.3")

        result = wf.refresh_local_summary(study)

        self.assertIsNotNone(result)
        self.assertEqual(study.local_id, "orthanc-id-99")
        self.assertEqual(study.local_series_count, 5)
        self.assertEqual(study.local_instance_count, 20)


class SummaryMatchesTests(unittest.TestCase):
    def test_returns_false_when_no_local_id(self) -> None:
        wf = ConcreteMirrorWorkflow()
        study = StudyState(answer_id="a1", study_uid="1.2.3", local_id=None)
        self.assertFalse(wf.summary_matches(study))

    def test_returns_true_when_counts_match(self) -> None:
        wf = ConcreteMirrorWorkflow()
        study = StudyState(
            answer_id="a1",
            study_uid="1.2.3",
            local_id="id-1",
            remote_series_count=3,
            local_series_count=3,
            remote_instance_count=10,
            local_instance_count=10,
        )
        self.assertTrue(wf.summary_matches(study))

    def test_returns_false_when_series_count_differs(self) -> None:
        wf = ConcreteMirrorWorkflow()
        study = StudyState(
            answer_id="a1",
            study_uid="1.2.3",
            local_id="id-1",
            remote_series_count=3,
            local_series_count=2,
        )
        self.assertFalse(wf.summary_matches(study))

    def test_returns_false_when_instance_count_differs(self) -> None:
        wf = ConcreteMirrorWorkflow()
        study = StudyState(
            answer_id="a1",
            study_uid="1.2.3",
            local_id="id-1",
            remote_series_count=3,
            local_series_count=3,
            remote_instance_count=10,
            local_instance_count=9,
        )
        self.assertFalse(wf.summary_matches(study))

    def test_returns_true_when_remote_counts_are_none(self) -> None:
        wf = ConcreteMirrorWorkflow()
        study = StudyState(
            answer_id="a1",
            study_uid="1.2.3",
            local_id="id-1",
            remote_series_count=None,
            local_series_count=5,
            remote_instance_count=None,
            local_instance_count=20,
        )
        self.assertTrue(wf.summary_matches(study))
