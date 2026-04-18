from __future__ import annotations

import unittest
from unittest import mock

from orthanc_tools.workflows.mirror_retrieval import LocalStudySummary, StudyState

from tests.workflow_mirror_retrieval_fakes import ConcreteMirrorWorkflow, _make_args


class SummarySyncTests(unittest.TestCase):
    def test_marks_matched_when_local_counts_agree(self) -> None:
        wf = ConcreteMirrorWorkflow()
        wf.client._local_study = {"ID": "id-1"}
        wf.client._study_stats = {"CountSeries": "2", "CountInstances": "8"}
        study = StudyState(
            answer_id="a1",
            study_uid="1.2.3",
            remote_series_count=2,
            remote_instance_count=8,
        )
        wf.studies = [study]

        with mock.patch("orthanc_tools.workflows.mirror_retrieval.STOP_REQUESTED", False):
            wf.summary_sync()

        self.assertEqual(study.summary_status, "matched")

    def test_marks_missing_and_triggers_retrieve_when_not_local(self) -> None:
        wf = ConcreteMirrorWorkflow(args=_make_args(max_retries=0))
        wf.client._local_study = None
        study = StudyState(answer_id="a1", study_uid="1.2.3")
        wf.studies = [study]

        with mock.patch("orthanc_tools.workflows.mirror_retrieval.STOP_REQUESTED", False), \
             mock.patch.object(wf, "retrieve_until_summary_ok") as retrieve:
            wf.summary_sync()

        retrieve.assert_called_once_with(study)
        self.assertEqual(study.summary_status, "missing")

    def test_marks_mismatch_when_counts_differ(self) -> None:
        call_count = [0]

        def cycling_local_study(uid: str):
            call_count[0] += 1
            if call_count[0] == 1:
                return {"ID": "id-1"}
            return {"ID": "id-1"}

        wf = ConcreteMirrorWorkflow(args=_make_args(max_retries=0))
        wf.client.lookup_local_study = cycling_local_study
        wf.client._study_stats = {"CountSeries": "1", "CountInstances": "3"}
        study = StudyState(
            answer_id="a1",
            study_uid="1.2.3",
            remote_series_count=2,
            remote_instance_count=8,
        )
        wf.studies = [study]

        with mock.patch("orthanc_tools.workflows.mirror_retrieval.STOP_REQUESTED", False), \
             mock.patch.object(wf, "retrieve_until_summary_ok") as retrieve:
            wf.summary_sync()

        retrieve.assert_called_once_with(study)
        self.assertEqual(call_count[0], 1)
        self.assertEqual(study.summary_status, "mismatch")

    def test_exception_in_summary_check_marks_failed(self) -> None:
        wf = ConcreteMirrorWorkflow()
        study = StudyState(answer_id="a1", study_uid="1.2.3")
        wf.studies = [study]

        with mock.patch("orthanc_tools.workflows.mirror_retrieval.STOP_REQUESTED", False), \
             mock.patch.object(wf, "refresh_local_summary", side_effect=RuntimeError("boom")):
            wf.summary_sync()

        self.assertEqual(study.exact_status, "failed")
        self.assertEqual(study.summary_status, "failed")
        self.assertEqual(study.error, "boom")
        self.assertEqual(study.action, "summary failed")


class RetrieveUntilSummaryOkTests(unittest.TestCase):
    def test_retrieves_and_marks_matched_on_success(self) -> None:
        wf = ConcreteMirrorWorkflow(args=_make_args(max_retries=2))
        study = StudyState(
            answer_id="a1",
            study_uid="1.2.3",
            remote_series_count=2,
            remote_instance_count=8,
        )
        wf.studies = [study]

        def make_summary_match(_study):
            _study.local_id = "id-1"
            _study.local_series_count = 2
            _study.local_instance_count = 8
            return LocalStudySummary(orthanc_id="id-1", series_count=2, instance_count=8)

        with mock.patch("orthanc_tools.workflows.mirror_retrieval.STOP_REQUESTED", False), \
             mock.patch.object(wf, "refresh_local_summary", side_effect=make_summary_match), \
             mock.patch.object(wf, "wait_after_change"):
            wf.retrieve_until_summary_ok(study)

        self.assertEqual(study.summary_status, "matched")
        self.assertEqual(len(wf.client.retrieved_studies), 1)

    def test_gives_up_after_max_retries_exceeded(self) -> None:
        wf = ConcreteMirrorWorkflow(args=_make_args(max_retries=1))
        study = StudyState(answer_id="a1", study_uid="1.2.3", remote_series_count=2)
        wf.studies = [study]

        def never_match(_study):
            _study.local_id = "id-1"
            _study.local_series_count = 1
            _study.local_instance_count = 1
            return LocalStudySummary(orthanc_id="id-1", series_count=1, instance_count=1)

        with mock.patch("orthanc_tools.workflows.mirror_retrieval.STOP_REQUESTED", False), \
             mock.patch.object(wf, "refresh_local_summary", side_effect=never_match), \
             mock.patch.object(wf, "wait_after_change"):
            wf.retrieve_until_summary_ok(study)

        self.assertEqual(study.exact_status, "failed")
        self.assertEqual(study.summary_status, "failed")
        self.assertIn("retries", study.error)
        self.assertEqual(study.action, "summary failed")

    def test_stops_when_local_has_more_than_remote(self) -> None:
        wf = ConcreteMirrorWorkflow(args=_make_args(max_retries=3))
        study = StudyState(
            answer_id="a1",
            study_uid="1.2.3",
            remote_series_count=2,
            remote_instance_count=8,
        )

        def more_than_remote(_study):
            _study.local_id = "id-1"
            _study.local_series_count = 3
            _study.local_instance_count = 15
            return LocalStudySummary(orthanc_id="id-1", series_count=3, instance_count=15)

        with mock.patch("orthanc_tools.workflows.mirror_retrieval.STOP_REQUESTED", False), \
             mock.patch.object(wf, "refresh_local_summary", side_effect=more_than_remote), \
             mock.patch.object(wf, "wait_after_change"):
            wf.retrieve_until_summary_ok(study)

        self.assertEqual(study.retrieve_attempts, 1)
        self.assertEqual(study.action, "count drift remains")


class ExactSyncTests(unittest.TestCase):
    def test_marks_verified_on_exact_study(self) -> None:
        wf = ConcreteMirrorWorkflow()
        study = StudyState(answer_id="a1", study_uid="1.2.3")
        wf.studies = [study]

        with mock.patch("orthanc_tools.workflows.mirror_retrieval.STOP_REQUESTED", False), \
             mock.patch.object(wf, "refresh_local_summary"), \
             mock.patch.object(wf, "ensure_exact_study", return_value=True):
            wf.exact_sync()

        self.assertEqual(study.exact_status, "verified")

    def test_marks_failed_when_ensure_exact_returns_false(self) -> None:
        wf = ConcreteMirrorWorkflow()
        study = StudyState(answer_id="a1", study_uid="1.2.3")
        wf.studies = [study]

        with mock.patch("orthanc_tools.workflows.mirror_retrieval.STOP_REQUESTED", False), \
             mock.patch.object(wf, "refresh_local_summary"), \
             mock.patch.object(wf, "ensure_exact_study", return_value=False):
            wf.exact_sync()

        self.assertEqual(study.exact_status, "failed")
        self.assertEqual(study.error, "Exact verification failed.")

    def test_skips_already_failed_studies(self) -> None:
        wf = ConcreteMirrorWorkflow()
        study = StudyState(answer_id="a1", study_uid="1.2.3", exact_status="failed")
        wf.studies = [study]

        ensure_calls = []
        with mock.patch("orthanc_tools.workflows.mirror_retrieval.STOP_REQUESTED", False), \
             mock.patch.object(wf, "ensure_exact_study", side_effect=ensure_calls.append):
            wf.exact_sync()

        self.assertEqual(len(ensure_calls), 0)

    def test_handles_exception_and_marks_failed(self) -> None:
        wf = ConcreteMirrorWorkflow()
        study = StudyState(answer_id="a1", study_uid="1.2.3")
        wf.studies = [study]

        with mock.patch("orthanc_tools.workflows.mirror_retrieval.STOP_REQUESTED", False), \
             mock.patch.object(wf, "refresh_local_summary", side_effect=RuntimeError("err")):
            wf.exact_sync()

        self.assertEqual(study.exact_status, "failed")
        self.assertEqual(study.error, "err")


class EnsureExactStudyTests(unittest.TestCase):
    def test_returns_true_when_manifests_already_match(self) -> None:
        wf = ConcreteMirrorWorkflow(args=_make_args(max_retries=2, repair_mode="skip"))
        wf.remote_query_id = "remote-query-1"
        study = StudyState(answer_id="a1", study_uid="1.2.3", local_id="id-1")

        manifest = {"s1": {"inst-a"}}
        with mock.patch("orthanc_tools.workflows.mirror_retrieval.STOP_REQUESTED", False), \
             mock.patch.object(wf, "refresh_local_summary"), \
             mock.patch.object(wf, "fetch_remote_manifest", return_value=manifest), \
             mock.patch.object(wf, "fetch_local_manifest", return_value=manifest), \
             mock.patch.object(wf, "wait_after_change"):
            result = wf.ensure_exact_study(study)

        self.assertTrue(result)
        self.assertEqual(study.action, "exact match")

    def test_returns_false_when_manifest_never_converges(self) -> None:
        wf = ConcreteMirrorWorkflow(args=_make_args(max_retries=1, repair_mode="skip"))
        wf.remote_query_id = "remote-query-1"
        study = StudyState(answer_id="a1", study_uid="1.2.3", local_id="id-1")

        remote = {"s1": {"inst-a", "inst-b"}}
        local = {"s1": {"inst-a"}}

        with mock.patch("orthanc_tools.workflows.mirror_retrieval.STOP_REQUESTED", False), \
             mock.patch.object(wf, "refresh_local_summary"), \
             mock.patch.object(wf, "fetch_remote_manifest", return_value=remote), \
             mock.patch.object(wf, "fetch_local_manifest", return_value=local), \
             mock.patch.object(wf, "wait_after_change"):
            result = wf.ensure_exact_study(study)

        self.assertFalse(result)
        self.assertIn("converge", study.error)

    def test_deletes_local_study_in_replace_mode_when_drift_needs_replace(self) -> None:
        wf = ConcreteMirrorWorkflow(args=_make_args(max_retries=0, repair_mode="replace"))
        wf.remote_query_id = "remote-query-1"
        study = StudyState(answer_id="a1", study_uid="1.2.3", local_id="id-to-delete")

        remote_manifest = {"s1": {"inst-a"}}
        local_manifest_with_extra = {"s1": {"inst-a", "inst-extra"}}

        manifests = [local_manifest_with_extra]

        with mock.patch("orthanc_tools.workflows.mirror_retrieval.STOP_REQUESTED", False), \
             mock.patch.object(wf, "refresh_local_summary"), \
             mock.patch.object(wf, "fetch_remote_manifest", return_value=remote_manifest), \
             mock.patch.object(wf, "fetch_local_manifest", side_effect=lambda s: manifests[0]), \
             mock.patch.object(wf, "wait_after_change"):
            wf.ensure_exact_study(study)

        self.assertIn("id-to-delete", wf.client.deleted_studies)
