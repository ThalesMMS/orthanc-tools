from __future__ import annotations

import datetime as dt
import hashlib
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from orthanc_tools.dicom import (
    extract_dicom_ids,
    iso_to_dicom_date,
    parse_count,
    pick_tag,
    safe_text,
    short_uid,
    unique_instance_records,
)
from orthanc_tools.state import atomic_write_json, ensure_dir, load_json_file, maybe_chown, utc_now_iso
from orthanc_tools.workflows.client import OrthancClient
from orthanc_tools.workflows.mirror_retrieval import (
    LocalStudySummary,
    ManifestDiff,
    MirrorWorkflowMixin,
    ParityManifest,
    StudyState,
    _safe_delete_query,
    compare_manifests,
)
from orthanc_tools.workflows.primitives import STOP_REQUESTED


ManifestRecord = dict[str, str]
ExactManifest = dict[str, list[ManifestRecord]]


COMMON_MATERIAL_STUDY_STATE_KEYS = (
    "status",
    "manifest_mode",
    "local_study_id",
    "local_series_count",
    "local_instance_count",
    "remote_series_count",
    "remote_instance_count",
    "remote_exact_instance_count",
    "accounted_count",
    "missing_count",
    "heuristic_target",
    "rejected_instances",
    "instance_failures",
    "retrieve_attempts",
    "manifest_error",
)

BACKUP_MATERIAL_STUDY_STATE_EXTRA_KEYS = (
    "accounting_complete",
    "backup_complete",
    "zip_attempts",
    "delete_attempts",
    "zip_filename",
    "zip_bytes",
    "zip_manifest_hash",
    "local_deleted_at",
)


@dataclass
class RemoteStudy:
    study_uid: str
    patient_id: str = ""
    patient_name: str = ""
    patient_birth_date: str = ""
    study_date: str = ""
    description: str = ""
    accession_number: str = ""
    remote_series_count: int | None = None
    remote_instance_count: int | None = None


@dataclass
class RetrievalPlan:
    mode: str
    missing: list[ManifestRecord]


@dataclass
class ImportOutcome:
    retrieved_files: int = 0
    imported_successfully: int = 0
    rejected_accounted: int = 0
    duplicates_or_existing: int = 0
    progress_made: bool = False
    notes: list[str] | None = None

    def __post_init__(self) -> None:
        if self.notes is None:
            self.notes = []


def material_study_state(
    state: dict[str, Any],
    *,
    extra_keys: Iterable[str] = (),
) -> dict[str, Any]:
    keys = tuple(COMMON_MATERIAL_STUDY_STATE_KEYS) + tuple(extra_keys)
    return {key: state.get(key) for key in keys}


def fetch_remote_manifest_exact(
    client: OrthancClient,
    modality: str,
    study_uid: str,
    *,
    study_query_fields: dict[str, str],
    normalize: bool | None = None,
) -> ExactManifest:
    study_query_id: str | None = None
    instance_query_id: str | None = None
    series_query_id: str | None = None
    try:
        query_fields = dict(study_query_fields)
        query_fields["StudyInstanceUID"] = study_uid
        study_query_id = client.create_remote_query(modality, "Study", query_fields, normalize=normalize)
        study_answer_id = pick_single_remote_study_answer(client, study_query_id, study_uid)
        instance_query_id = client.create_child_query(
            study_query_id,
            study_answer_id,
            "instances",
            {
                "SeriesInstanceUID": "",
                "SOPInstanceUID": "",
                "SOPClassUID": "",
            },
        )
        manifest = read_instance_manifest(client, instance_query_id)
        if manifest is not None:
            return manifest

        series_query_id = client.create_child_query(
            study_query_id,
            study_answer_id,
            "series",
            {"SeriesInstanceUID": ""},
        )
        series_manifest: ExactManifest = {}
        for series_answer_id in client.get_query_answers(series_query_id):
            series_content = client.get_query_answer_content(series_query_id, series_answer_id)
            series_uid = pick_tag(series_content, "SeriesInstanceUID")
            if not series_uid:
                raise RuntimeError(f"Series query did not return SeriesInstanceUID for study {study_uid}.")
            child_instance_query_id = client.create_child_query(
                series_query_id,
                series_answer_id,
                "instances",
                {"SOPInstanceUID": "", "SOPClassUID": ""},
            )
            try:
                instances: list[ManifestRecord] = []
                for answer_id in client.get_query_answers(child_instance_query_id):
                    content = client.get_query_answer_content(child_instance_query_id, answer_id)
                    sop_uid = pick_tag(content, "SOPInstanceUID")
                    sop_class_uid = pick_tag(content, "SOPClassUID") or ""
                    if not sop_uid:
                        raise RuntimeError(
                            f"Instance query did not return SOPInstanceUID for series {series_uid}."
                        )
                    instances.append(
                        {
                            "series_uid": series_uid,
                            "sop_uid": sop_uid,
                            "sop_class_uid": sop_class_uid,
                        }
                    )
                series_manifest[series_uid] = unique_instance_records(instances)
            finally:
                _safe_delete_query(client, child_instance_query_id)
        return series_manifest
    finally:
        for query_id in (instance_query_id, series_query_id, study_query_id):
            if query_id:
                _safe_delete_query(client, query_id)


def read_instance_manifest(client: OrthancClient, query_id: str) -> ExactManifest | None:
    manifest: ExactManifest = {}
    missing_series_uid = False
    for answer_id in client.get_query_answers(query_id):
        content = client.get_query_answer_content(query_id, answer_id)
        series_uid = pick_tag(content, "SeriesInstanceUID")
        sop_uid = pick_tag(content, "SOPInstanceUID")
        sop_class_uid = pick_tag(content, "SOPClassUID") or ""
        if not sop_uid:
            raise RuntimeError(f"Remote IMAGE-level answer {answer_id} did not contain SOPInstanceUID.")
        if not series_uid:
            missing_series_uid = True
            break
        manifest.setdefault(series_uid, []).append(
            {
                "series_uid": series_uid,
                "sop_uid": sop_uid,
                "sop_class_uid": sop_class_uid,
            }
        )
    if missing_series_uid:
        return None
    for series_uid, records in list(manifest.items()):
        manifest[series_uid] = unique_instance_records(records)
    return manifest


def pick_single_remote_study_answer(client: OrthancClient, query_id: str, study_uid: str) -> str:
    for answer_id in client.get_query_answers(query_id):
        content = client.get_query_answer_content(query_id, answer_id)
        if pick_tag(content, "StudyInstanceUID") == study_uid:
            return answer_id
    raise RuntimeError(f"Remote query for study {study_uid} returned no exact answer.")


class RemoteStudyWorkflowMixin:
    def prepare_remote_modality(self) -> None:
        self.state.log(
            "Ensuring temporary Orthanc modality "
            f"{self.args.remote_name!r} -> {self.args.remote_aet}@{self.args.remote_host}:{self.args.remote_port}"
        )
        self.client.put_modality(
            self.args.remote_name,
            self.args.remote_aet,
            self.args.remote_host,
            self.args.remote_port,
        )
        self.client.echo_modality(self.args.remote_name, timeout=self.args.echo_timeout)
        system = self.client.system()
        name = safe_text(system.get("Name")) or "Orthanc"
        version = safe_text(system.get("Version")) or "?"
        self.state.log(f"Connected to {name} {version} at {self.client.settings.base_url}")

    def load_or_query_day_studies(self, day: dt.date) -> list[RemoteStudy]:
        cached = None if self.args.refresh_day_cache else self.state.load_day_cache(day)
        if cached is not None:
            self.state.log(f"Using cached remote study list for {day.isoformat()} ({len(cached)} studies)")
            return cached

        result = self.query_remote_day_studies(day)
        self.state.save_day_cache(day, result)
        return result

    def query_remote_day_studies(self, day: dt.date) -> list[RemoteStudy]:
        query_id: str | None = None
        try:
            query_id = self.client.create_remote_query(self.args.remote_name, "Study", self._day_query_fields(day))
            result: list[RemoteStudy] = []
            seen: set[str] = set()
            for answer_id in self.client.get_query_answers(query_id):
                content = self.client.get_query_answer_content(query_id, answer_id)
                study = self._remote_study_from_content(content)
                if study is None or study.study_uid in seen:
                    continue
                seen.add(study.study_uid)
                result.append(study)
            result.sort(key=lambda study: (study.study_date, study.patient_id, study.study_uid))
            self.state.log(f"Queried {len(result)} remote studies for {day.isoformat()}")
            return result
        finally:
            if query_id:
                try:
                    self.client.delete_query(query_id)
                except Exception:
                    pass

    def bootstrap_day_status_from_cache(self, day: dt.date, studies: list[RemoteStudy], status: dict[str, Any]) -> None:
        study_states = status.setdefault("studies", {})
        if not isinstance(study_states, dict):
            study_states = {}
            status["studies"] = study_states

        self._mark_absent_studies(day, {study.study_uid for study in studies}, study_states)
        for study in studies:
            entry = study_states.get(study.study_uid)
            if not isinstance(entry, dict):
                entry = {}
                study_states[study.study_uid] = entry
            entry["study_uid"] = study.study_uid
            entry["patient_id"] = study.patient_id
            entry["patient_name"] = study.patient_name
            entry["patient_birth_date"] = study.patient_birth_date
            entry["study_date"] = study.study_date
            entry["description"] = study.description
            entry["accession_number"] = study.accession_number
            entry["remote_series_count"] = study.remote_series_count
            entry["remote_instance_count"] = study.remote_instance_count
            entry.setdefault("status", "pending")
            entry.setdefault("manifest_mode", "unknown")
            entry.setdefault("rejected_instances", {})
            entry.setdefault("instance_failures", {})
            entry.setdefault("retrieve_attempts", 0)
            self._populate_workflow_study_state(day, study, entry)
        self._after_bootstrap_day_status(day, studies, status)

    def manifest_path(self, day: dt.date, study_uid: str) -> Path:
        filename = hashlib.sha256(study_uid.encode("utf-8")).hexdigest() + ".json"
        return self.state.day_manifest_dir(day) / filename

    def load_or_fetch_remote_manifest(
        self,
        day: dt.date,
        study: RemoteStudy,
        study_state: dict[str, Any],
    ) -> tuple[str, dict[str, list[dict[str, str]]] | None]:
        manifest_path = self.manifest_path(day, study.study_uid)
        if manifest_path.exists():
            payload = self._load_cached_manifest(manifest_path)
            if payload is not None:
                return payload

        try:
            manifest = fetch_remote_manifest_exact(
                self.client,
                self.args.remote_name,
                study.study_uid,
                study_query_fields=self._manifest_query_fields(),
            )
        except Exception as exc:
            self.exact_manifest_failures[study.study_uid] = str(exc)
            study_state["manifest_error"] = str(exc)
            if self.args.allow_heuristic_fallback:
                atomic_write_json(
                    manifest_path,
                    {
                        "study_uid": study.study_uid,
                        "mode": "heuristic",
                        "cached_at": utc_now_iso(),
                        "reason": str(exc),
                    },
                    self._state_owner(),
                )
                self.state.log(
                    f"Exact manifest unavailable for {short_uid(study.study_uid)}; using heuristic fallback: {exc}"
                )
                return "heuristic", None
            raise

        atomic_write_json(
            manifest_path,
            {
                "study_uid": study.study_uid,
                "mode": "exact",
                "cached_at": utc_now_iso(),
                "series": manifest,
            },
            self._state_owner(),
        )
        return "exact", manifest

    def local_manifest(self, local_study_id: str) -> tuple[dict[str, set[str]], set[str]]:
        series_items = self.client.get_study_series_expanded(local_study_id)
        instance_items = self.client.get_study_instances_expanded(local_study_id)
        series_uid_by_id: dict[str, str] = {}
        manifest: dict[str, set[str]] = {}
        for series in series_items:
            series_id = safe_text(series.get("ID"))
            series_uid = pick_tag(series, "SeriesInstanceUID")
            if series_id and series_uid:
                series_uid_by_id[series_id] = series_uid
                manifest.setdefault(series_uid, set())
        local_sops: set[str] = set()
        for instance in instance_items:
            sop_uid = pick_tag(instance, "SOPInstanceUID")
            parent_series = safe_text(instance.get("ParentSeries"))
            series_uid = series_uid_by_id.get(parent_series)
            if sop_uid:
                local_sops.add(sop_uid)
            if sop_uid and series_uid:
                manifest.setdefault(series_uid, set()).add(sop_uid)
        return manifest, local_sops

    def retrieve_and_import(
        self,
        day: dt.date,
        study: RemoteStudy,
        study_state: dict[str, Any],
        plan: RetrievalPlan,
    ) -> ImportOutcome:
        study_state["retrieve_attempts"] = int(study_state.get("retrieve_attempts", 0)) + 1
        outcome = ImportOutcome()
        study_label = short_uid(study.study_uid)
        with tempfile.TemporaryDirectory(prefix=self._temp_dir_prefix()) as temp_dir_str:
            temp_dir = Path(temp_dir_str)
            if plan.mode == "study":
                self.state.log(
                    f"Retrieving whole study {study_label} (attempt {study_state['retrieve_attempts']})"
                )
                self.run_getscu_study(study.study_uid, temp_dir)
            else:
                self.state.log(
                    f"Retrieving {len(plan.missing)} missing instance(s) from study {study_label}"
                )
                outcome.notes.extend(self.run_getscu_missing_instances(study.study_uid, plan.missing, temp_dir))
                if not any(path.is_file() for path in temp_dir.rglob("*")):
                    self.state.log(
                        f"No files were received for missing-instance retrieve of {study_label}; "
                        "falling back to whole-study retrieve once"
                    )
                    self.run_getscu_study(study.study_uid, temp_dir)

            files = sorted(path for path in temp_dir.rglob("*") if path.is_file())
            outcome.retrieved_files = len(files)
            if not files:
                outcome.notes.append("retrieve returned zero files")
                return outcome

            for dicom_file in files:
                ids = extract_dicom_ids(dicom_file)
                sop_uid = ids.get("sop_uid")
                try:
                    response = self.client.import_dicom_file(dicom_file)
                    status_text = safe_text(response.get("Status")) if isinstance(response, dict) else ""
                    if status_text.lower() == "alreadystored":
                        outcome.duplicates_or_existing += 1
                    else:
                        outcome.imported_successfully += 1
                        outcome.progress_made = True
                except Exception as exc:
                    if not sop_uid:
                        outcome.notes.append(
                            f"import failed and SOPInstanceUID could not be extracted from {dicom_file.name}: {exc}"
                        )
                        continue
                    failure_count = int(study_state.setdefault("instance_failures", {}).get(sop_uid, 0)) + 1
                    study_state["instance_failures"][sop_uid] = failure_count
                    archive_info = self.archive_rejected_instance(
                        day,
                        study.study_uid,
                        ids,
                        dicom_file,
                        str(exc),
                        failure_count,
                    )
                    if failure_count >= self.args.reject_after_failures:
                        rejected_map = study_state.setdefault("rejected_instances", {})
                        if sop_uid not in rejected_map:
                            rejected_map[sop_uid] = archive_info
                            outcome.rejected_accounted += 1
                            outcome.progress_made = True
                    outcome.notes.append(f"{short_uid(sop_uid)} rejected by Orthanc: {exc}")

        stats = self.state.meta.setdefault("stats", {})
        if isinstance(stats, dict):
            stats["instances_imported"] = int(stats.get("instances_imported", 0)) + outcome.imported_successfully
        self.state.save_meta()
        return outcome

    def run_getscu_study(self, study_uid: str, output_dir: Path) -> None:
        command = [
            "getscu",
            "-S",
            "-od",
            str(output_dir),
            "-aet",
            self.args.calling_aet,
            "-aec",
            self.args.remote_aet,
            self.args.remote_host,
            str(self.args.remote_port),
            "-k",
            "QueryRetrieveLevel=STUDY",
            "-k",
            f"StudyInstanceUID={study_uid}",
        ]
        self.run_subprocess(command, timeout=self.args.getscu_timeout_seconds)

    def run_getscu_missing_instances(
        self,
        study_uid: str,
        missing_records: list[dict[str, str]],
        output_dir: Path,
    ) -> list[str]:
        errors: list[str] = []
        for item in missing_records:
            if STOP_REQUESTED:
                return errors
            series_uid = item.get("series_uid")
            sop_uid = item.get("sop_uid")
            if not series_uid or not sop_uid:
                continue
            command = [
                "getscu",
                "-S",
                "-od",
                str(output_dir),
                "-aet",
                self.args.calling_aet,
                "-aec",
                self.args.remote_aet,
                self.args.remote_host,
                str(self.args.remote_port),
                "-k",
                "QueryRetrieveLevel=IMAGE",
                "-k",
                f"StudyInstanceUID={study_uid}",
                "-k",
                f"SeriesInstanceUID={series_uid}",
                "-k",
                f"SOPInstanceUID={sop_uid}",
            ]
            try:
                self.run_subprocess(command, timeout=self.args.getscu_timeout_seconds)
            except Exception as exc:
                errors.append(f"retrieve failed for {short_uid(sop_uid)}: {exc}")
                self.state.error(f"Missing-instance retrieve failed for {short_uid(sop_uid)}: {exc}")
        return errors

    def run_subprocess(self, command: list[str], timeout: int) -> None:
        sanitized_command = _sanitized_command(command)
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"Command timed out after {timeout}s: {sanitized_command}") from exc
        if result.returncode != 0:
            details = "\n".join(part for part in (result.stdout.strip(), result.stderr.strip()) if part)
            raise RuntimeError(
                f"Command failed with exit code {result.returncode}: {sanitized_command}\n{details or 'no output'}"
            )

    def archive_rejected_instance(
        self,
        day: dt.date,
        study_uid: str,
        ids: dict[str, str],
        source_path: Path,
        error_text: str,
        failure_count: int,
    ) -> dict[str, Any]:
        study_hash = hashlib.sha256(study_uid.encode("utf-8")).hexdigest()
        sop_uid = ids.get("sop_uid", "unknown-sop")
        sop_hash = hashlib.sha256(sop_uid.encode("utf-8")).hexdigest()
        target_dir = self.state.day_rejected_dir(day) / study_hash
        ensure_dir(target_dir, self._state_owner())
        target_dcm = target_dir / f"{sop_hash}.dcm"
        target_meta = target_dir / f"{sop_hash}.json"
        shutil.copy2(source_path, target_dcm)
        maybe_chown(target_dcm, self._state_owner())
        metadata = {
            "archived_at": utc_now_iso(),
            "study_uid": study_uid,
            "series_uid": ids.get("series_uid", ""),
            "sop_uid": sop_uid,
            "sop_class_uid": ids.get("sop_class_uid", ""),
            "source_file": source_path.name,
            "stored_file": str(target_dcm.relative_to(self.state.root)),
            "failure_count": failure_count,
            "last_error": error_text,
        }
        metadata.update(self._archive_metadata_extra(sop_hash))
        atomic_write_json(target_meta, metadata, self._state_owner())
        return metadata

    def day_is_complete(self, studies: list[RemoteStudy], status: dict[str, Any]) -> bool:
        study_states = status.get("studies", {}) if isinstance(status.get("studies"), dict) else {}
        for study in studies:
            state = study_states.get(study.study_uid, {})
            if not isinstance(state, dict) or not self._study_state_is_complete(state):
                return False
        return True

    def day_completion_mode(self, studies: list[RemoteStudy], status: dict[str, Any]) -> str:
        study_states = status.get("studies", {}) if isinstance(status.get("studies"), dict) else {}
        for study in studies:
            state = study_states.get(study.study_uid, {})
            if isinstance(state, dict) and self._study_state_uses_heuristic(state):
                return "complete-with-heuristic"
        return "complete-exact"

    def _day_query_fields(self, day: dt.date) -> dict[str, str]:
        raise NotImplementedError

    def _manifest_query_fields(self) -> dict[str, str]:
        raise NotImplementedError

    def _remote_study_from_content(self, content: dict[str, Any]) -> RemoteStudy | None:
        study_uid = pick_tag(content, "StudyInstanceUID")
        if not study_uid:
            return None
        return RemoteStudy(
            study_uid=study_uid,
            patient_id=safe_text(pick_tag(content, "PatientID")),
            patient_name=safe_text(pick_tag(content, "PatientName")),
            patient_birth_date=safe_text(pick_tag(content, "PatientBirthDate")),
            study_date=safe_text(pick_tag(content, "StudyDate")),
            description=safe_text(pick_tag(content, "StudyDescription")),
            accession_number=safe_text(pick_tag(content, "AccessionNumber")),
            remote_series_count=parse_count(pick_tag(content, "NumberOfStudyRelatedSeries")),
            remote_instance_count=parse_count(pick_tag(content, "NumberOfStudyRelatedInstances")),
        )

    def _load_cached_manifest(self, manifest_path: Path) -> tuple[str, dict[str, list[dict[str, str]]] | None] | None:
        payload = load_json_file(manifest_path)
        if isinstance(payload, dict) and payload.get("mode") == "exact" and isinstance(payload.get("series"), dict):
            return "exact", payload["series"]
        if isinstance(payload, dict) and payload.get("mode") == "heuristic":
            return "heuristic", None
        return None

    def _mark_absent_studies(
        self,
        day: dt.date,
        live_uids: set[str],
        study_states: dict[str, Any],
    ) -> None:
        del day, live_uids, study_states

    def _populate_workflow_study_state(self, day: dt.date, study: RemoteStudy, entry: dict[str, Any]) -> None:
        del day, study, entry

    def _after_bootstrap_day_status(
        self,
        day: dt.date,
        studies: list[RemoteStudy],
        status: dict[str, Any],
    ) -> None:
        del day, studies, status

    def _archive_metadata_extra(self, sop_hash: str) -> dict[str, Any]:
        del sop_hash
        return {}

    def _study_state_is_complete(self, state: dict[str, Any]) -> bool:
        raise NotImplementedError

    def _study_state_uses_heuristic(self, state: dict[str, Any]) -> bool:
        value = safe_text(state.get("status"))
        return value == "heuristic-complete" or value.startswith("complete-heuristic")

    def _state_owner(self) -> Any:
        return getattr(self.state, "owner", None)

    def _temp_dir_prefix(self) -> str:
        return "orthanc-retrieve-"


def _sanitized_command(command: list[str]) -> str:
    return " ".join(_sanitize_uid_token(token) for token in command)


def _sanitize_uid_token(token: str) -> str:
    for prefix in ("StudyInstanceUID=", "SeriesInstanceUID=", "SOPInstanceUID="):
        if token.startswith(prefix):
            return prefix + short_uid(token[len(prefix) :])
    return token
