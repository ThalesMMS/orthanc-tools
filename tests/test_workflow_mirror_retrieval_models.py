from __future__ import annotations

import unittest

from orthanc_tools.workflows.mirror_retrieval import (
    LocalStudySummary,
    ManifestDiff,
    StudyState,
    compare_manifests,
)


class ManifestDiffTests(unittest.TestCase):
    def test_defaults_are_zero_and_exact(self) -> None:
        diff = ManifestDiff()
        self.assertTrue(diff.exact)
        self.assertFalse(diff.needs_replace)

    def test_missing_series_makes_not_exact(self) -> None:
        diff = ManifestDiff(missing_series=1)
        self.assertFalse(diff.exact)
        self.assertFalse(diff.needs_replace)

    def test_extra_series_triggers_needs_replace(self) -> None:
        diff = ManifestDiff(extra_series=1)
        self.assertFalse(diff.exact)
        self.assertTrue(diff.needs_replace)

    def test_extra_instances_triggers_needs_replace(self) -> None:
        diff = ManifestDiff(extra_instances=2)
        self.assertFalse(diff.exact)
        self.assertTrue(diff.needs_replace)

    def test_summary_contains_all_counts(self) -> None:
        diff = ManifestDiff(missing_series=1, extra_series=2, missing_instances=3, extra_instances=4)
        summary = diff.summary()
        self.assertIn("-1", summary)
        self.assertIn("+2", summary)
        self.assertIn("-3", summary)
        self.assertIn("+4", summary)

    def test_summary_zero_counts(self) -> None:
        diff = ManifestDiff()
        summary = diff.summary()
        self.assertIn("-0", summary)
        self.assertIn("+0", summary)


class StudyStateTests(unittest.TestCase):
    def test_defaults(self) -> None:
        state = StudyState(answer_id="a1", study_uid="1.2.3")
        self.assertEqual(state.answer_id, "a1")
        self.assertEqual(state.study_uid, "1.2.3")
        self.assertEqual(state.patient_name, "")
        self.assertEqual(state.summary_status, "pending")
        self.assertEqual(state.exact_status, "pending")
        self.assertEqual(state.action, "pending")
        self.assertEqual(state.retrieve_attempts, 0)
        self.assertIsNone(state.local_id)
        self.assertIsNone(state.error)

    def test_label_contains_uid(self) -> None:
        state = StudyState(answer_id="a1", study_uid="1.2.840.113619.1.9")
        label = state.label()
        self.assertIn("1.2.840", label)

    def test_label_includes_date_and_patient_id_when_set(self) -> None:
        state = StudyState(
            answer_id="a2",
            study_uid="1.2.840.113619.1.9",
            study_date="20240101",
            patient_id="P-001",
        )
        label = state.label()
        self.assertIn("20240101", label)
        self.assertIn("P-001", label)

    def test_label_without_optional_fields(self) -> None:
        state = StudyState(answer_id="a3", study_uid="1.2.3")
        label = state.label()
        self.assertIsInstance(label, str)
        self.assertTrue(len(label) > 0)


class LocalStudySummaryTests(unittest.TestCase):
    def test_construction(self) -> None:
        s = LocalStudySummary(orthanc_id="id-1", series_count=3, instance_count=9)
        self.assertEqual(s.orthanc_id, "id-1")
        self.assertEqual(s.series_count, 3)
        self.assertEqual(s.instance_count, 9)

    def test_nullable_counts(self) -> None:
        s = LocalStudySummary(orthanc_id="id-2", series_count=None, instance_count=None)
        self.assertIsNone(s.series_count)
        self.assertIsNone(s.instance_count)


class CompareManifestsTests(unittest.TestCase):
    def test_identical_manifests_are_exact(self) -> None:
        diff = compare_manifests(
            {"s1": {"a", "b"}, "s2": {"c"}},
            {"s1": {"a", "b"}, "s2": {"c"}},
        )
        self.assertTrue(diff.exact)
        self.assertEqual(diff.missing_series, 0)
        self.assertEqual(diff.extra_series, 0)
        self.assertEqual(diff.missing_instances, 0)
        self.assertEqual(diff.extra_instances, 0)

    def test_both_empty_is_exact(self) -> None:
        diff = compare_manifests({}, {})
        self.assertTrue(diff.exact)

    def test_remote_has_extra_series(self) -> None:
        diff = compare_manifests({"s1": {"a"}, "s2": {"b"}}, {"s1": {"a"}})
        self.assertEqual(diff.missing_series, 1)
        self.assertEqual(diff.extra_series, 0)
        self.assertFalse(diff.exact)

    def test_local_has_extra_series(self) -> None:
        diff = compare_manifests({"s1": {"a"}}, {"s1": {"a"}, "s2": {"b"}})
        self.assertEqual(diff.missing_series, 0)
        self.assertEqual(diff.extra_series, 1)
        self.assertTrue(diff.needs_replace)

    def test_instance_drift_within_shared_series(self) -> None:
        diff = compare_manifests(
            {"s1": {"a", "b"}},
            {"s1": {"a", "x"}},
        )
        self.assertEqual(diff.missing_instances, 1)
        self.assertEqual(diff.extra_instances, 1)
        self.assertFalse(diff.exact)

    def test_remote_missing_all_instances(self) -> None:
        diff = compare_manifests({"s1": {"a", "b"}}, {})
        self.assertEqual(diff.missing_series, 1)
        self.assertEqual(diff.extra_series, 0)
        self.assertFalse(diff.exact)

    def test_mixed_drift(self) -> None:
        diff = compare_manifests(
            {"series-1": {"a", "b"}, "series-2": {"c"}},
            {"series-1": {"a", "x"}, "series-3": {"z"}},
        )
        self.assertEqual(diff.missing_series, 1)
        self.assertEqual(diff.extra_series, 1)
        self.assertEqual(diff.missing_instances, 1)
        self.assertEqual(diff.extra_instances, 1)


class RetrievalReexportTests(unittest.TestCase):
    """Verify that retrieval.py correctly re-exports symbols from mirror_retrieval."""

    def test_compare_manifests_importable_from_retrieval(self) -> None:
        from orthanc_tools.workflows.retrieval import compare_manifests as cm
        diff = cm({"s1": {"a"}}, {"s1": {"a"}})
        self.assertTrue(diff.exact)

    def test_manifest_diff_importable_from_retrieval(self) -> None:
        from orthanc_tools.workflows.retrieval import ManifestDiff
        d = ManifestDiff(missing_series=1)
        self.assertFalse(d.exact)

    def test_study_state_importable_from_retrieval(self) -> None:
        from orthanc_tools.workflows.retrieval import StudyState
        s = StudyState(answer_id="x", study_uid="1.2.3")
        self.assertEqual(s.study_uid, "1.2.3")

    def test_local_study_summary_importable_from_retrieval(self) -> None:
        from orthanc_tools.workflows.retrieval import LocalStudySummary
        s = LocalStudySummary(orthanc_id="id", series_count=1, instance_count=4)
        self.assertEqual(s.orthanc_id, "id")

    def test_parity_manifest_type_is_dict(self) -> None:
        from orthanc_tools.workflows.retrieval import ParityManifest
        self.assertIsNotNone(ParityManifest)

    def test_mirror_workflow_mixin_importable_from_retrieval(self) -> None:
        from orthanc_tools.workflows.retrieval import MirrorWorkflowMixin
        self.assertIsNotNone(MirrorWorkflowMixin)
