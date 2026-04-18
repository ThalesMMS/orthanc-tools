from __future__ import annotations

import logging
import shutil
import time
from dataclasses import dataclass
from orthanc_tools.dicom import parse_count, pick_tag, safe_text, short_uid
from orthanc_tools.workflows.primitives import STOP_REQUESTED


LOGGER = logging.getLogger(__name__)


def _safe_delete_query(client: object, query_id: str) -> None:
    try:
        client.delete_query(query_id)  # type: ignore[attr-defined]
    except Exception as exc:
        LOGGER.debug("Failed to delete query %s: %s", query_id, exc, exc_info=True)


ParityManifest = dict[str, set[str]]


@dataclass
class ManifestDiff:
    missing_series: int = 0
    extra_series: int = 0
    missing_instances: int = 0
    extra_instances: int = 0

    @property
    def exact(self) -> bool:
        return (
            self.missing_series == 0
            and self.extra_series == 0
            and self.missing_instances == 0
            and self.extra_instances == 0
        )

    @property
    def needs_replace(self) -> bool:
        return self.extra_series > 0 or self.extra_instances > 0

    def summary(self) -> str:
        return (
            f"series -{self.missing_series}/+{self.extra_series}, "
            f"instances -{self.missing_instances}/+{self.extra_instances}"
        )


@dataclass
class LocalStudySummary:
    orthanc_id: str
    series_count: int | None
    instance_count: int | None


@dataclass
class StudyState:
    answer_id: str
    study_uid: str
    patient_name: str = ""
    patient_id: str = ""
    study_date: str = ""
    description: str = ""
    remote_series_count: int | None = None
    remote_instance_count: int | None = None
    local_id: str | None = None
    local_series_count: int | None = None
    local_instance_count: int | None = None
    summary_status: str = "pending"
    exact_status: str = "pending"
    action: str = "pending"
    retrieve_attempts: int = 0
    error: str | None = None

    def label(self) -> str:
        pieces = [short_uid(self.study_uid, length=28)]
        if self.study_date:
            pieces.append(self.study_date)
        if self.patient_id:
            pieces.append(self.patient_id)
        return " | ".join(pieces)


def _fmt_count(value: int | None) -> str:
    return str(value) if value is not None else "?"


def compare_manifests(remote_manifest: ParityManifest, local_manifest: ParityManifest) -> ManifestDiff:
    diff = ManifestDiff()
    remote_series = set(remote_manifest)
    local_series = set(local_manifest)
    diff.missing_series = len(remote_series - local_series)
    diff.extra_series = len(local_series - remote_series)
    for series_uid in remote_series & local_series:
        remote_instances = remote_manifest[series_uid]
        local_instances = local_manifest[series_uid]
        diff.missing_instances += len(remote_instances - local_instances)
        diff.extra_instances += len(local_instances - remote_instances)
    return diff


class MirrorWorkflowMixin:
    def check_connectivity(self) -> None:
        self.phase = "connecting"
        if self.args.retrieve_method == "get" and shutil.which("getscu") is None:
            raise RuntimeError(
                "getscu is not installed. Install dcmtk or rerun install-orthanc-native.sh."
            )
        system_info = self.client.system()
        orthanc_name = safe_text(system_info.get("Name")) or "Orthanc"
        orthanc_version = safe_text(system_info.get("Version"))
        version_suffix = f" {orthanc_version}" if orthanc_version else ""
        self.dashboard.log(
            f"Connected to {orthanc_name}{version_suffix} at {self.client.settings.base_url}"
        )

    def load_remote_inventory(self) -> None:
        self.phase = "querying remote inventory"
        self.dashboard.log(f"Starting remote study query against modality {self.remote_modality}")
        self.remote_query_id = self.client.create_remote_study_query(self.remote_modality)
        pairs = self.client.get_query_answer_pairs(self.remote_query_id)
        limit = self.args.limit_studies
        seen_uids = {study.study_uid for study in self.studies}
        for answer_id, content in pairs:
            if limit is not None and len(self.studies) >= limit:
                break
            study_uid = pick_tag(content, "StudyInstanceUID")
            if not study_uid:
                self.dashboard.log(
                    f"Skipping remote answer {answer_id} because StudyInstanceUID is missing"
                )
                continue
            if study_uid in seen_uids:
                self.dashboard.log(
                    f"Skipping duplicate remote StudyInstanceUID {short_uid(study_uid, length=28)}"
                )
                continue
            self.studies.append(StudyState(
                answer_id=answer_id,
                study_uid=study_uid,
                patient_name=safe_text(pick_tag(content, "PatientName")),
                patient_id=safe_text(pick_tag(content, "PatientID")),
                study_date=safe_text(pick_tag(content, "StudyDate")),
                description=safe_text(pick_tag(content, "StudyDescription")),
                remote_series_count=parse_count(pick_tag(content, "NumberOfStudyRelatedSeries")),
                remote_instance_count=parse_count(pick_tag(content, "NumberOfStudyRelatedInstances")),
            ))
            seen_uids.add(study_uid)
        if not self.studies and not self.args.allow_empty_remote:
            raise RuntimeError(f"Remote modality {self.remote_modality} returned zero studies.")
        self.dashboard.log(f"Remote query returned {len(self.studies)} studies")

    def get_local_summary(self, study_uid: str) -> LocalStudySummary | None:
        item = self.client.lookup_local_study(study_uid)
        if item is None:
            return None
        orthanc_id = safe_text(item.get("ID"))
        stats = self.client.get_study_statistics(orthanc_id)
        return LocalStudySummary(
            orthanc_id=orthanc_id,
            series_count=parse_count(stats.get("CountSeries")),
            instance_count=parse_count(stats.get("CountInstances")),
        )

    def refresh_local_summary(self, study: StudyState) -> LocalStudySummary | None:
        summary = self.get_local_summary(study.study_uid)
        if summary is None:
            study.local_id = None
            study.local_series_count = None
            study.local_instance_count = None
            return None
        study.local_id = summary.orthanc_id
        study.local_series_count = summary.series_count
        study.local_instance_count = summary.instance_count
        return summary

    def summary_sync(self) -> None:
        self.phase = "checking study counts"
        for index, study in enumerate(self.studies, start=1):
            if STOP_REQUESTED:
                return
            self.current_study = study
            try:
                study.action = f"summary check {index}/{len(self.studies)}"
                local = self.refresh_local_summary(study)
                if local is None:
                    study.summary_status = "missing"
                    self.dashboard.log(f"Study missing locally: {study.label()}")
                    self.retrieve_until_summary_ok(study)
                    continue
                if self.summary_matches(study):
                    study.summary_status = "matched"
                    continue
                study.summary_status = "mismatch"
                self.dashboard.log(
                    f"Count mismatch for {study.label()} "
                    f"(remote {_fmt_count(study.remote_series_count)}/{_fmt_count(study.remote_instance_count)} vs "
                    f"local {_fmt_count(study.local_series_count)}/{_fmt_count(study.local_instance_count)})"
                )
                self.retrieve_until_summary_ok(study)
            except Exception as exc:
                study.summary_status = "failed"
                study.exact_status = "failed"
                study.error = str(exc)
                study.action = "summary failed"
                self.dashboard.log(f"Summary check failed for {study.label()}: {exc}")
        self.current_study = None

    def summary_matches(self, study: StudyState) -> bool:
        if study.local_id is None:
            return False
        if study.remote_series_count is not None and study.local_series_count != study.remote_series_count:
            return False
        if study.remote_instance_count is not None and study.local_instance_count != study.remote_instance_count:
            return False
        return True

    def retrieve_until_summary_ok(self, study: StudyState) -> None:
        while not STOP_REQUESTED:
            if study.retrieve_attempts > self.args.max_retries:
                study.summary_status = "failed"
                study.exact_status = "failed"
                study.error = "Summary mismatch remained after retrieve retries."
                study.action = "summary failed"
                self.dashboard.log(f"Giving up on {study.label()}: {study.error}")
                return
            study.retrieve_attempts += 1
            study.action = "retrieve study"
            self.dashboard.log(f"Retrieving {study.label()} (attempt {study.retrieve_attempts})")
            self.client.retrieve_study(
                self.remote_modality,
                study.study_uid,
                self.args.retrieve_method,
                self.args.target_aet,
            )
            self.wait_after_change()
            self.refresh_local_summary(study)
            if self.summary_matches(study):
                study.summary_status = "matched"
                study.action = "summary matched"
                self.dashboard.log(f"Study counts now match: {study.label()}")
                return
            if (
                study.remote_series_count is not None
                and study.local_series_count is not None
                and study.local_series_count > study.remote_series_count
            ) or (
                study.remote_instance_count is not None
                and study.local_instance_count is not None
                and study.local_instance_count > study.remote_instance_count
            ):
                study.action = "count drift remains"
                self.dashboard.log(
                    f"Local study still has more objects than remote after retrieve: {study.label()}"
                )
                return

    def wait_after_change(self) -> None:
        deadline = time.time() + self.args.settle_seconds
        while time.time() < deadline:
            if STOP_REQUESTED:
                return
            time.sleep(0.5)

    def exact_sync(self) -> None:
        self.phase = "verifying exact series and instances"
        for index, study in enumerate(self.studies, start=1):
            if STOP_REQUESTED or study.exact_status == "failed":
                continue
            self.current_study = study
            try:
                study.action = f"exact verify {index}/{len(self.studies)}"
                self.refresh_local_summary(study)
                if self.ensure_exact_study(study):
                    study.exact_status = "verified"
                    self.dashboard.log(f"Exact match confirmed for {study.label()}")
                elif study.exact_status != "failed":
                    study.exact_status = "failed"
                    study.error = study.error or "Exact verification failed."
                    self.dashboard.log(f"Exact verification failed for {study.label()}")
            except Exception as exc:
                study.exact_status = "failed"
                study.error = str(exc)
                study.action = "exact verify failed"
                self.dashboard.log(f"Exact verification failed for {study.label()}: {exc}")
        self.current_study = None

    def ensure_exact_study(self, study: StudyState) -> bool:
        attempts = 0
        while attempts <= self.args.max_retries and not STOP_REQUESTED:
            attempts += 1
            self.refresh_local_summary(study)
            remote_manifest = self.fetch_remote_manifest(study)
            local_manifest = self.fetch_local_manifest(study)
            diff = compare_manifests(remote_manifest, local_manifest)
            if diff.exact:
                study.action = "exact match"
                return True
            study.action = f"repair exact drift ({diff.summary()})"
            self.dashboard.log(f"Drift detected for {study.label()}: {diff.summary()}")
            if self.args.repair_mode == "replace" and diff.needs_replace and study.local_id:
                self.dashboard.log(f"Deleting drifted local study before refill: {study.label()}")
                self.client.delete_study(study.local_id)
                self.wait_after_change()
                self.refresh_local_summary(study)
            self.dashboard.log(f"Re-retrieving study for exact repair: {study.label()}")
            self.client.retrieve_study(
                self.remote_modality,
                study.study_uid,
                self.args.retrieve_method,
                self.args.target_aet,
            )
            self.wait_after_change()
        study.error = "Exact study manifest did not converge."
        return False

    def fetch_remote_manifest(self, study: StudyState) -> dict[str, set[str]]:
        if self.remote_query_id is None:
            raise RuntimeError("Remote query was not created.")
        study.action = "remote instance query"
        query_id = self.client.create_child_query(
            self.remote_query_id,
            study.answer_id,
            "instances",
            {"SeriesInstanceUID": "", "SOPInstanceUID": ""},
        )
        try:
            pairs = self.client.get_query_answer_pairs(query_id)
            manifest: dict[str, set[str]] = {}
            missing_series = False
            for _answer_id, content in pairs:
                series_uid = pick_tag(content, "SeriesInstanceUID")
                sop_uid = pick_tag(content, "SOPInstanceUID")
                if not sop_uid:
                    raise RuntimeError(f"Remote instance query for {study.study_uid} returned no SOPInstanceUID.")
                if not series_uid:
                    missing_series = True
                    break
                manifest.setdefault(series_uid, set()).add(sop_uid)
            if not missing_series:
                return manifest
            self.dashboard.log(
                f"Remote instance query omitted SeriesInstanceUID for {study.label()}, falling back to series walk"
            )
            return self.fetch_remote_manifest_via_series(study)
        finally:
            _safe_delete_query(self.client, query_id)

    def fetch_remote_manifest_via_series(self, study: StudyState) -> dict[str, set[str]]:
        if self.remote_query_id is None:
            raise RuntimeError("Remote query was not created.")
        series_query_id = self.client.create_child_query(
            self.remote_query_id,
            study.answer_id,
            "series",
            {"SeriesInstanceUID": ""},
        )
        try:
            series_pairs = self.client.get_query_answer_pairs(series_query_id)
            manifest: dict[str, set[str]] = {}
            for series_answer_id, content in series_pairs:
                series_uid = pick_tag(content, "SeriesInstanceUID")
                if not series_uid:
                    raise RuntimeError(f"Remote series query for {study.study_uid} returned no SeriesInstanceUID.")
                instance_query_id = self.client.create_child_query(
                    series_query_id,
                    series_answer_id,
                    "instances",
                    {"SOPInstanceUID": ""},
                )
                try:
                    manifest[series_uid] = set()
                    for _answer_id, instance in self.client.get_query_answer_pairs(instance_query_id):
                        sop_uid = pick_tag(instance, "SOPInstanceUID")
                        if not sop_uid:
                            raise RuntimeError(
                                f"Remote instance query for series {series_uid} returned no SOPInstanceUID."
                            )
                        manifest[series_uid].add(sop_uid)
                finally:
                    _safe_delete_query(self.client, instance_query_id)
            return manifest
        finally:
            _safe_delete_query(self.client, series_query_id)

    def fetch_local_manifest(self, study: StudyState) -> dict[str, set[str]]:
        if study.local_id is None:
            return {}
        study.action = "local manifest read"
        series_items = self.client.get_study_series_expanded(study.local_id)
        instance_items = self.client.get_study_instances_expanded(study.local_id)
        series_uid_by_id: dict[str, str] = {}
        manifest: dict[str, set[str]] = {}
        for series in series_items:
            series_id = safe_text(series.get("ID"))
            series_uid = pick_tag(series, "SeriesInstanceUID")
            if series_id and series_uid:
                series_uid_by_id[series_id] = series_uid
                manifest.setdefault(series_uid, set())
        for instance in instance_items:
            instance_uid = pick_tag(instance, "SOPInstanceUID")
            parent_series = safe_text(instance.get("ParentSeries"))
            series_uid = series_uid_by_id.get(parent_series)
            if not instance_uid or not series_uid:
                raise RuntimeError(
                    f"Local study {study.study_uid} contains an instance that could not be mapped "
                    "to SeriesInstanceUID/SOPInstanceUID."
                )
            manifest.setdefault(series_uid, set()).add(instance_uid)
        return manifest

    def check_or_repair_extra_local_studies(self) -> None:
        self.phase = "checking extra local studies"
        remote_uids = {study.study_uid for study in self.studies}
        local_map = self.client.list_local_study_map()
        self.extra_local_studies = {
            study_uid: study_id
            for study_uid, study_id in local_map.items()
            if study_uid not in remote_uids
        }
        if not self.extra_local_studies:
            self.dashboard.log("No extra local studies were found")
            return
        self.dashboard.log(
            f"Found {len(self.extra_local_studies)} extra local studies not present on the remote node"
        )
        if self.args.repair_mode != "replace":
            return
        for study_uid, study_id in list(self.extra_local_studies.items()):
            if STOP_REQUESTED:
                return
            self.dashboard.log(f"Deleting extra local study {short_uid(study_uid, length=28)}")
            self.client.delete_study(study_id)
            self.wait_after_change()
            self.extra_local_studies.pop(study_uid, None)
        self.dashboard.log("Extra local study cleanup completed")
