from __future__ import annotations

import unittest
from unittest import mock

from orthanc_tools.workflows.mirror_retrieval import StudyState, _safe_delete_query

from tests.workflow_mirror_retrieval_fakes import ConcreteMirrorWorkflow, _make_args


class SafeDeleteQueryTests(unittest.TestCase):
    def test_logs_delete_failures(self) -> None:
        class FailingClient:
            def delete_query(self, query_id: str) -> None:
                raise RuntimeError("delete failed")

        with self.assertLogs("orthanc_tools.workflows.mirror_retrieval", level="DEBUG") as logs:
            _safe_delete_query(FailingClient(), "query-1")

        self.assertTrue(any("query-1" in message and "delete failed" in message for message in logs.output))


class FetchRemoteManifestTests(unittest.TestCase):
    def test_raises_when_remote_query_id_is_none(self) -> None:
        wf = ConcreteMirrorWorkflow()
        wf.remote_query_id = None
        study = StudyState(answer_id="a1", study_uid="1.2.3")

        with self.assertRaises(RuntimeError) as ctx:
            wf.fetch_remote_manifest(study)

        self.assertIn("Remote query was not created", str(ctx.exception))

    def test_returns_manifest_when_all_series_uids_present(self) -> None:
        wf = ConcreteMirrorWorkflow()
        wf.remote_query_id = "rq-1"
        study = StudyState(answer_id="a1", study_uid="1.2.3")

        wf.client._child_query_map = {
            ("rq-1", "a1", "instances"): "inst-query",
        }
        wf.client._query_answer_pairs["inst-query"] = [
            ("ans-1", {"SeriesInstanceUID": "s1", "SOPInstanceUID": "sop-1"}),
            ("ans-2", {"SeriesInstanceUID": "s1", "SOPInstanceUID": "sop-2"}),
        ]

        manifest = wf.fetch_remote_manifest(study)

        self.assertIn("s1", manifest)
        self.assertEqual(manifest["s1"], {"sop-1", "sop-2"})
        self.assertIn("inst-query", wf.client.deleted_queries)

    def test_raises_when_sop_uid_missing(self) -> None:
        wf = ConcreteMirrorWorkflow()
        wf.remote_query_id = "rq-1"
        study = StudyState(answer_id="a1", study_uid="1.2.3")

        wf.client._child_query_map = {("rq-1", "a1", "instances"): "inst-query"}
        wf.client._query_answer_pairs["inst-query"] = [
            ("ans-1", {"SeriesInstanceUID": "s1"}),
        ]

        with self.assertRaises(RuntimeError) as ctx:
            wf.fetch_remote_manifest(study)

        self.assertIn("SOPInstanceUID", str(ctx.exception))

    def test_falls_back_to_series_walk_when_series_uid_missing(self) -> None:
        wf = ConcreteMirrorWorkflow()
        wf.remote_query_id = "rq-1"
        study = StudyState(answer_id="a1", study_uid="1.2.3")

        wf.client._child_query_map = {
            ("rq-1", "a1", "instances"): "inst-query",
        }
        wf.client._query_answer_pairs["inst-query"] = [
            ("ans-1", {"SOPInstanceUID": "sop-1"}),
        ]

        fallback_manifest = {"series-x": {"sop-99"}}
        with mock.patch.object(wf, "fetch_remote_manifest_via_series", return_value=fallback_manifest) as m:
            result = wf.fetch_remote_manifest(study)

        m.assert_called_once_with(study)
        self.assertEqual(result, fallback_manifest)

    def test_deletes_query_even_on_error(self) -> None:
        wf = ConcreteMirrorWorkflow()
        wf.remote_query_id = "rq-1"
        study = StudyState(answer_id="a1", study_uid="1.2.3")

        wf.client._child_query_map = {("rq-1", "a1", "instances"): "inst-query"}
        wf.client._query_answer_pairs["inst-query"] = [
            ("ans-1", {"SeriesInstanceUID": "s1"}),
        ]

        with self.assertRaises(RuntimeError):
            wf.fetch_remote_manifest(study)

        self.assertIn("inst-query", wf.client.deleted_queries)


class FetchRemoteManifestViaSeriesTests(unittest.TestCase):
    def test_raises_when_remote_query_id_is_none(self) -> None:
        wf = ConcreteMirrorWorkflow()
        wf.remote_query_id = None
        study = StudyState(answer_id="a1", study_uid="1.2.3")

        with self.assertRaises(RuntimeError) as ctx:
            wf.fetch_remote_manifest_via_series(study)

        self.assertIn("Remote query was not created", str(ctx.exception))

    def test_builds_manifest_from_series_walk(self) -> None:
        wf = ConcreteMirrorWorkflow()
        wf.remote_query_id = "rq-1"
        study = StudyState(answer_id="a1", study_uid="1.2.3")

        wf.client._child_query_map = {
            ("rq-1", "a1", "series"): "series-query",
            ("series-query", "s-ans-1", "instances"): "inst-query-1",
        }
        wf.client._query_answer_pairs["series-query"] = [
            ("s-ans-1", {"SeriesInstanceUID": "series-A"}),
        ]
        wf.client._query_answer_pairs["inst-query-1"] = [
            ("i-ans-1", {"SOPInstanceUID": "sop-1"}),
            ("i-ans-2", {"SOPInstanceUID": "sop-2"}),
        ]

        manifest = wf.fetch_remote_manifest_via_series(study)

        self.assertIn("series-A", manifest)
        self.assertEqual(manifest["series-A"], {"sop-1", "sop-2"})
        self.assertIn("series-query", wf.client.deleted_queries)
        self.assertIn("inst-query-1", wf.client.deleted_queries)

    def test_raises_when_series_uid_missing_in_series_response(self) -> None:
        wf = ConcreteMirrorWorkflow()
        wf.remote_query_id = "rq-1"
        study = StudyState(answer_id="a1", study_uid="1.2.3")

        wf.client._child_query_map = {("rq-1", "a1", "series"): "series-query"}
        wf.client._query_answer_pairs["series-query"] = [
            ("s-ans-1", {}),
        ]

        with self.assertRaises(RuntimeError) as ctx:
            wf.fetch_remote_manifest_via_series(study)

        self.assertIn("SeriesInstanceUID", str(ctx.exception))

    def test_raises_when_sop_uid_missing_in_instance_response(self) -> None:
        wf = ConcreteMirrorWorkflow()
        wf.remote_query_id = "rq-1"
        study = StudyState(answer_id="a1", study_uid="1.2.3")

        wf.client._child_query_map = {
            ("rq-1", "a1", "series"): "series-query",
            ("series-query", "s-ans-1", "instances"): "inst-query-1",
        }
        wf.client._query_answer_pairs["series-query"] = [
            ("s-ans-1", {"SeriesInstanceUID": "series-A"}),
        ]
        wf.client._query_answer_pairs["inst-query-1"] = [
            ("i-ans-1", {}),
        ]

        with self.assertRaises(RuntimeError) as ctx:
            wf.fetch_remote_manifest_via_series(study)

        self.assertIn("SOPInstanceUID", str(ctx.exception))

    def test_deletes_all_queries_on_error(self) -> None:
        wf = ConcreteMirrorWorkflow()
        wf.remote_query_id = "rq-1"
        study = StudyState(answer_id="a1", study_uid="1.2.3")

        wf.client._child_query_map = {
            ("rq-1", "a1", "series"): "series-query",
            ("series-query", "s-ans-1", "instances"): "inst-query-1",
        }
        wf.client._query_answer_pairs["series-query"] = [
            ("s-ans-1", {"SeriesInstanceUID": "series-A"}),
        ]
        wf.client._query_answer_pairs["inst-query-1"] = [
            ("i-ans-1", {}),
        ]

        with self.assertRaises(RuntimeError):
            wf.fetch_remote_manifest_via_series(study)

        self.assertIn("inst-query-1", wf.client.deleted_queries)
        self.assertIn("series-query", wf.client.deleted_queries)


class FetchLocalManifestTests(unittest.TestCase):
    def test_returns_empty_dict_when_no_local_id(self) -> None:
        wf = ConcreteMirrorWorkflow()
        study = StudyState(answer_id="a1", study_uid="1.2.3", local_id=None)

        result = wf.fetch_local_manifest(study)

        self.assertEqual(result, {})

    def test_builds_manifest_from_local_series_and_instances(self) -> None:
        wf = ConcreteMirrorWorkflow()
        study = StudyState(answer_id="a1", study_uid="1.2.3", local_id="local-id")
        wf.client._series_expanded = [
            {"ID": "series-local-1", "MainDicomTags": {"SeriesInstanceUID": "series-A"}},
        ]
        wf.client._instances_expanded = [
            {
                "MainDicomTags": {"SOPInstanceUID": "sop-1"},
                "ParentSeries": "series-local-1",
            },
            {
                "MainDicomTags": {"SOPInstanceUID": "sop-2"},
                "ParentSeries": "series-local-1",
            },
        ]

        manifest = wf.fetch_local_manifest(study)

        self.assertIn("series-A", manifest)
        self.assertEqual(manifest["series-A"], {"sop-1", "sop-2"})

    def test_raises_when_instance_cannot_be_mapped_to_series(self) -> None:
        wf = ConcreteMirrorWorkflow()
        study = StudyState(answer_id="a1", study_uid="1.2.3", local_id="local-id")
        wf.client._series_expanded = [
            {"ID": "series-local-1", "MainDicomTags": {"SeriesInstanceUID": "series-A"}},
        ]
        wf.client._instances_expanded = [
            {
                "MainDicomTags": {"SOPInstanceUID": "sop-1"},
                "ParentSeries": "unknown-series",
            },
        ]

        with self.assertRaises(RuntimeError) as ctx:
            wf.fetch_local_manifest(study)

        self.assertIn("SeriesInstanceUID", str(ctx.exception))

    def test_sets_action_before_reading(self) -> None:
        wf = ConcreteMirrorWorkflow()
        study = StudyState(answer_id="a1", study_uid="1.2.3", local_id="local-id")
        wf.client._series_expanded = []
        wf.client._instances_expanded = []

        wf.fetch_local_manifest(study)

        self.assertEqual(study.action, "local manifest read")


class CheckOrRepairExtraLocalStudiesTests(unittest.TestCase):
    def test_logs_when_no_extras(self) -> None:
        wf = ConcreteMirrorWorkflow()
        wf.studies = [StudyState(answer_id="a1", study_uid="1.2.3")]
        wf.client._local_study_map = {"1.2.3": "id-1"}

        with mock.patch("orthanc_tools.workflows.mirror_retrieval.STOP_REQUESTED", False):
            wf.check_or_repair_extra_local_studies()

        self.assertEqual(wf.extra_local_studies, {})
        self.assertTrue(any("No extra" in m for m in wf.dashboard.messages))

    def test_identifies_extra_local_studies(self) -> None:
        wf = ConcreteMirrorWorkflow(args=_make_args(repair_mode="skip"))
        wf.studies = [StudyState(answer_id="a1", study_uid="1.2.3")]
        wf.client._local_study_map = {
            "1.2.3": "id-1",
            "9.9.9": "id-extra",
        }

        with mock.patch("orthanc_tools.workflows.mirror_retrieval.STOP_REQUESTED", False):
            wf.check_or_repair_extra_local_studies()

        self.assertIn("9.9.9", wf.extra_local_studies)
        self.assertNotIn("1.2.3", wf.extra_local_studies)

    def test_does_not_delete_in_non_replace_mode(self) -> None:
        wf = ConcreteMirrorWorkflow(args=_make_args(repair_mode="skip"))
        wf.studies = [StudyState(answer_id="a1", study_uid="1.2.3")]
        wf.client._local_study_map = {"1.2.3": "id-1", "9.9.9": "id-extra"}

        with mock.patch("orthanc_tools.workflows.mirror_retrieval.STOP_REQUESTED", False):
            wf.check_or_repair_extra_local_studies()

        self.assertEqual(wf.client.deleted_studies, [])

    def test_deletes_extras_in_replace_mode(self) -> None:
        wf = ConcreteMirrorWorkflow(args=_make_args(repair_mode="replace"))
        wf.studies = [StudyState(answer_id="a1", study_uid="1.2.3")]
        wf.client._local_study_map = {"1.2.3": "id-1", "9.9.9": "id-extra"}

        with mock.patch("orthanc_tools.workflows.mirror_retrieval.STOP_REQUESTED", False), \
             mock.patch.object(wf, "wait_after_change"):
            wf.check_or_repair_extra_local_studies()

        self.assertIn("id-extra", wf.client.deleted_studies)
        self.assertEqual(wf.extra_local_studies, {})
        self.assertTrue(any("cleanup completed" in m.lower() for m in wf.dashboard.messages))

    def test_stops_deletion_when_stop_requested(self) -> None:
        wf = ConcreteMirrorWorkflow(args=_make_args(repair_mode="replace"))
        wf.studies = []
        wf.client._local_study_map = {"9.9.9": "id-extra-1", "8.8.8": "id-extra-2"}

        with mock.patch("orthanc_tools.workflows.mirror_retrieval.STOP_REQUESTED", True):
            wf.check_or_repair_extra_local_studies()

        self.assertEqual(wf.client.deleted_studies, [])


class WaitAfterChangeTests(unittest.TestCase):
    def test_sleeps_for_settle_seconds(self) -> None:
        wf = ConcreteMirrorWorkflow(args=_make_args(settle_seconds=1))

        sleep_calls = []
        fake_times = [0.0, 0.3, 0.7, 1.1]
        time_iter = iter(fake_times)

        with mock.patch("orthanc_tools.workflows.mirror_retrieval.STOP_REQUESTED", False), \
             mock.patch("orthanc_tools.workflows.mirror_retrieval.time.time", side_effect=time_iter), \
             mock.patch("orthanc_tools.workflows.mirror_retrieval.time.sleep", side_effect=sleep_calls.append):
            wf.wait_after_change()

        self.assertTrue(len(sleep_calls) > 0)
        for call in sleep_calls:
            self.assertEqual(call, 0.5)

    def test_exits_immediately_when_stop_requested(self) -> None:
        wf = ConcreteMirrorWorkflow(args=_make_args(settle_seconds=60))

        sleep_calls = []
        with mock.patch("orthanc_tools.workflows.mirror_retrieval.STOP_REQUESTED", True), \
             mock.patch("orthanc_tools.workflows.mirror_retrieval.time.time", return_value=0.0), \
             mock.patch("orthanc_tools.workflows.mirror_retrieval.time.sleep", side_effect=sleep_calls.append):
            wf.wait_after_change()

        self.assertEqual(len(sleep_calls), 0)
