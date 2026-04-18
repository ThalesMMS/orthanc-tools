from __future__ import annotations

import hashlib
import json
import os
import re
import time
import unicodedata
import zipfile
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterable

from .dicom import date_to_orthanc, iso_to_dicom_date, parse_count, pick_tag, safe_text
from .orthanc_api import OrthancApiError, OrthancNetworkError
from .state import atomic_write_json, atomic_write_text, ensure_dir, load_json, maybe_chown, now_iso, utc_now_iso


ZIP_MANIFEST_NAME = "__backup__/manifest.json"
ZIP_REJECTED_PREFIX = "__backup__/rejected/"
BACKUP_MANIFEST_PRODUCER = "backup_remote_to_zip.py"


class ZipValidationError(RuntimeError):
    pass


def normalize_study_day(study_date: str | None, fallback: date) -> str:
    value = (study_date or "").strip()
    if re.fullmatch(r"\d{8}", value):
        return value
    return date_to_orthanc(fallback)


def ascii_slug(value: str, fallback: str = "UNKNOWN") -> str:
    text = unicodedata.normalize("NFKD", value or "")
    text = text.encode("ascii", "ignore").decode("ascii")
    text = text.replace("^", "_")
    text = re.sub(r"[^A-Za-z0-9._-]+", "_", text)
    text = re.sub(r"_+", "_", text)
    text = text.strip("._-")
    return text or fallback


def truncate_with_hash(text: str, limit: int = 180) -> str:
    if len(text) <= limit:
        return text
    digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]
    head = max(16, limit - 13)
    return f"{text[:head]}_{digest}"


def build_patient_base(study: Any, day: date) -> str:
    patient = ascii_slug(getattr(study, "patient_name", "") or "UNKNOWN")
    birth = ascii_slug(getattr(study, "patient_birth_date", "") or "UNKNOWN")
    study_date = ascii_slug(
        normalize_study_day(getattr(study, "study_date", "") or "", day),
        fallback=date_to_orthanc(day),
    )
    return truncate_with_hash(f"{patient}_{birth}_{study_date}")


def _build_series_labels(series_items: list[Any]) -> dict[str, str]:
    """Map Orthanc series IDs to human-readable slugs for ZIP path building."""
    labels: dict[str, str] = {}
    for index, series in enumerate(series_items, start=1):
        series_id = str(series.get("ID") or "").strip()
        if not series_id:
            continue
        description = (
            pick_tag(series, "SeriesDescription")
            or pick_tag(series, "SeriesInstanceUID")
            or f"SERIES_{index:03d}"
        )
        modality = pick_tag(series, "Modality") or "SERIES"
        labels[series_id] = ascii_slug(f"{modality}_{description}", fallback=f"SERIES_{index:03d}")
    return labels


def _build_instance_member_name(
    instance: Any,
    patient_dir: str,
    study_dir: str,
    series_labels: dict[str, str],
    ordinal: int,
    used_paths: set[str],
) -> str:
    """Compute a unique ZIP member path for a DICOM instance."""
    series_dir = series_labels.get(
        instance.parent_series_id,
        ascii_slug(instance.parent_series_id or "SERIES", fallback="SERIES"),
    )
    if instance.instance_number.isdigit():
        base_name = f"IMG{int(instance.instance_number):06d}.dcm"
    elif instance.instance_number:
        base_name = f"IMG_{ascii_slug(instance.instance_number, fallback='UNKNOWN')}.dcm"
    elif instance.sop_instance_uid:
        base_name = f"SOP_{ascii_slug(instance.sop_instance_uid, fallback='UNKNOWN')}.dcm"
    else:
        base_name = f"IMG_{ordinal:06d}.dcm"
    member_name = f"{patient_dir}/{study_dir}/{series_dir}/{base_name}"
    if member_name in used_paths:
        stem = base_name[:-4] if base_name.endswith(".dcm") else base_name
        suffix = ascii_slug(instance.sop_instance_uid or instance.orthanc_id, fallback=str(ordinal))
        member_name = f"{patient_dir}/{study_dir}/{series_dir}/{stem}_{suffix}.dcm"
    used_paths.add(member_name)
    return member_name


def validate_zip_file(path: Path) -> None:
    if not path.exists():
        raise ZipValidationError(f"ZIP file not found: {path}")
    try:
        if path.stat().st_size <= 0:
            raise ZipValidationError(f"ZIP file is empty: {path}")
        if not zipfile.is_zipfile(path):
            raise ZipValidationError(f"Invalid ZIP file: {path}")
        with zipfile.ZipFile(path, "r") as archive:
            names = [name for name in archive.namelist() if not name.endswith("/")]
            if not names:
                raise ZipValidationError(f"ZIP file has no members: {path}")
            bad = archive.testzip()
            if bad is not None:
                raise ZipValidationError(f"ZIP CRC validation failed at member {bad!r} in {path}")
    except (zipfile.BadZipFile, ValueError, OSError) as exc:
        raise ZipValidationError(f"Invalid ZIP file: {path}") from exc


def read_zip_manifest(path: Path) -> dict[str, Any] | None:
    if not path.exists() or not zipfile.is_zipfile(path):
        return None
    with zipfile.ZipFile(path, "r") as archive:
        try:
            with archive.open(ZIP_MANIFEST_NAME) as handle:
                payload = json.load(handle)
        except (KeyError, json.JSONDecodeError, UnicodeDecodeError):
            return None
    return payload if isinstance(payload, dict) else None


def format_size(num_bytes: int) -> str:
    size = float(max(0, int(num_bytes)))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024.0 or unit == "TB":
            if unit == "B":
                return f"{int(size)}{unit}"
            return f"{size:.1f}{unit}"
        size /= 1024.0


def format_duration(seconds: float) -> str:
    total = max(0.0, float(seconds))
    if total < 1:
        return f"{total:.2f}s"
    if total < 60:
        return f"{total:.1f}s"
    minutes, secs = divmod(total, 60.0)
    if minutes < 60:
        return f"{int(minutes)}m{secs:04.1f}s"
    hours, minutes = divmod(minutes, 60.0)
    return f"{int(hours)}h{int(minutes):02d}m{secs:04.1f}s"


class BackupZipMixin:
    def _populate_workflow_study_state(self, day: date, study: Any, entry: dict[str, Any]) -> None:
        del day, study
        entry.setdefault("zip_attempts", 0)
        entry.setdefault("delete_attempts", 0)
        entry.setdefault("backup_complete", False)
        entry.setdefault("accounting_complete", False)
        entry["required"] = True
        entry.setdefault("created_at", utc_now_iso())
        entry["updated_at"] = utc_now_iso()

    def _mark_absent_studies(self, day: date, live_uids: set[str], study_states: dict[str, Any]) -> None:
        del day
        for study_uid, entry in list(study_states.items()):
            if not isinstance(entry, dict):
                study_states[study_uid] = {}
                entry = study_states[study_uid]
            if study_uid not in live_uids:
                entry["required"] = False
                if entry.get("backup_complete") is not True:
                    entry["status"] = "absent"
                    entry["last_error"] = "Not present in the latest remote day inventory"
                entry["updated_at"] = utc_now_iso()

    def _after_bootstrap_day_status(self, day: date, studies: list[Any], status: dict[str, Any]) -> None:
        self.assign_zip_filenames(day, studies, status)

    def _archive_metadata_extra(self, sop_hash: str) -> dict[str, Any]:
        return {"file_token": sop_hash}

    def _study_state_is_complete(self, state: dict[str, Any]) -> bool:
        return state.get("backup_complete") is True

    def _study_state_uses_heuristic(self, state: dict[str, Any]) -> bool:
        return "heuristic" in safe_text(state.get("status")) or safe_text(state.get("manifest_mode")) == "heuristic"

    def assign_zip_filenames(self, day: date, studies: list[Any], status: dict[str, Any]) -> None:
        study_states = status.get("studies", {})
        if not isinstance(study_states, dict):
            raise RuntimeError("Invalid studies map in status")
        used_names: set[str] = set()

        for study in studies:
            entry = study_states.get(study.study_uid, {})
            filename = entry.get("zip_filename")
            if isinstance(filename, str) and filename.strip():
                normalized = filename.strip()
                if normalized in used_names:
                    raise RuntimeError(f"Duplicate ZIP filename assignment in status.json: {normalized}")
                entry["zip_filename"] = normalized
                used_names.add(normalized)

        for study in studies:
            entry = study_states.get(study.study_uid, {})
            if entry.get("zip_filename"):
                continue
            if self.args.name == "uid":
                base = truncate_with_hash(ascii_slug(study.study_uid, fallback="STUDY"), limit=220)
            else:
                base = build_patient_base(study, day)
            candidate = f"{base}.zip"
            if candidate not in used_names:
                entry["zip_filename"] = candidate
                used_names.add(candidate)
                continue
            suffix = 2
            while True:
                candidate = f"{base}_{suffix}.zip"
                if candidate not in used_names:
                    entry["zip_filename"] = candidate
                    used_names.add(candidate)
                    break
                suffix += 1

    def backup_day_dir(self, day: date) -> Path:
        path = self.backup_dir / day.strftime("%Y") / day.strftime("%m") / day.strftime("%d")
        ensure_dir(path, self.owner)
        return path

    def final_zip_path(self, day: date, zip_filename: str) -> Path:
        return self.backup_day_dir(day) / zip_filename

    def zip_part_path(self, final_zip: Path) -> Path:
        return final_zip.with_name(final_zip.name + ".part")

    def maybe_complete_from_existing_zip(self, day: date, study: Any, state: dict[str, Any]) -> bool:
        zip_filename = safe_text(state.get("zip_filename"))
        if not zip_filename:
            return False
        final_zip = self.final_zip_path(day, zip_filename)
        part_zip = self.zip_part_path(final_zip)
        if part_zip.exists() and not final_zip.exists():
            try:
                part_zip.unlink(missing_ok=True)
            except OSError:
                pass
        if not final_zip.exists():
            if state.get("backup_complete") is True:
                state["backup_complete"] = False
                state["status"] = "pending"
            return False

        try:
            validate_zip_file(final_zip)
        except (ZipValidationError, zipfile.BadZipFile, ValueError) as exc:
            state["status"] = "pending"
            state["backup_complete"] = False
            state["last_error"] = f"Existing ZIP invalid: {exc}"
            try:
                final_zip.unlink()
            except OSError:
                pass
            return True

        manifest = read_zip_manifest(final_zip)
        if not self.existing_zip_matches(study, state, manifest):
            state["status"] = "pending"
            state["backup_complete"] = False
            state["last_error"] = "Existing ZIP manifest does not match current study state"
            return False

        state["zip_bytes"] = final_zip.stat().st_size
        state["zip_validated_at"] = utc_now_iso()
        state["zip_manifest_hash"] = safe_text(manifest.get("manifest_sha1", "")) if isinstance(manifest, dict) else ""
        self.delete_local_if_present(day, study, state)
        return True

    def existing_zip_matches(self, study: Any, state: dict[str, Any], manifest: dict[str, Any] | None) -> bool:
        if not isinstance(manifest, dict):
            return False
        if safe_text(manifest.get("study_uid")) != study.study_uid:
            return False
        if manifest.get("backup_complete") is not True:
            return False
        current_rejected = len(state.get("rejected_instances", {})) if isinstance(state.get("rejected_instances"), dict) else 0
        if parse_count(manifest.get("rejected_count")) != current_rejected:
            return False
        current_mode = safe_text(state.get("manifest_mode"))
        recorded_mode = safe_text(manifest.get("accounting_mode"))
        if current_mode and recorded_mode and current_mode != "unknown" and recorded_mode not in ("", current_mode):
            return False
        return True

    def rejected_study_dir(self, day: date, study_uid: str) -> Path:
        study_hash = hashlib.sha1(study_uid.encode("utf-8")).hexdigest()
        path = self.state.day_rejected_dir(day) / study_hash
        ensure_dir(path, self.owner)
        return path

    def remove_rejected_archive(self, day: date, study_uid: str) -> None:
        path = self.rejected_study_dir(day, study_uid)
        if path.exists():
            import shutil

            shutil.rmtree(path)

    def ensure_zip_and_cleanup(self, day: date, study: Any, state: dict[str, Any]) -> None:
        zip_filename = safe_text(state.get("zip_filename"))
        if not zip_filename:
            raise RuntimeError("ZIP filename has not been assigned")
        final_zip = self.final_zip_path(day, zip_filename)
        part_zip = self.zip_part_path(final_zip)
        part_zip.unlink(missing_ok=True)
        study_label = safe_text(getattr(study, "study_uid", ""))

        if final_zip.exists():
            manifest = read_zip_manifest(final_zip)
            if self.existing_zip_matches(study, state, manifest):
                state["zip_bytes"] = final_zip.stat().st_size
                state["zip_validated_at"] = utc_now_iso()
                self.delete_local_if_present(day, study, state)
                return
            final_zip.unlink(missing_ok=True)

        state["zip_attempts"] = int(state.get("zip_attempts", 0)) + 1
        local_study_id = safe_text(state.get("local_study_id"))
        rejected_entries = self.rejected_entries_for_study(state)
        started = time.monotonic()
        self.state.log(
            f"Creating ZIP for study {study_label} "
            f"(mode={self.args.zip_mode}, local-study-present={bool(local_study_id)}, "
            f"rejected={len(rejected_entries)})"
        )

        if self.args.zip_mode == "archive" and local_study_id:
            archive_started = time.monotonic()
            response = self.client.download_study_archive(local_study_id, part_zip)
            maybe_chown(part_zip, self.owner)
            archive_bytes = int(response.get("bytes_written", 0))
            self.state.log(
                f"Downloaded Orthanc archive for study {study_label}: "
                f"{format_size(archive_bytes)} in {format_duration(time.monotonic() - archive_started)}"
            )
            validate_zip_file(part_zip)
            self.append_manifest_and_rejected_to_zip(part_zip, day, study, state, rejected_entries)
        else:
            self.build_combined_zip(part_zip, day, study, state, local_study_id, rejected_entries)

        validate_zip_file(part_zip)
        os.replace(part_zip, final_zip)
        maybe_chown(final_zip, self.owner)
        manifest = read_zip_manifest(final_zip)
        if not self.existing_zip_matches(study, state, manifest):
            raise RuntimeError("ZIP was created but its manifest did not validate against the current study state")
        state["zip_bytes"] = final_zip.stat().st_size
        state["zip_validated_at"] = utc_now_iso()
        state["zip_manifest_hash"] = safe_text(manifest.get("manifest_sha1", "")) if manifest else ""
        self.state.log(
            f"Created ZIP for study {study_label} -> {final_zip} "
            f"({format_size(state['zip_bytes'])}, zip phase {format_duration(time.monotonic() - started)})"
        )
        self.delete_local_if_present(day, study, state)

    def delete_local_if_present(self, day: date, study: Any, state: dict[str, Any]) -> None:
        local = self.client.lookup_local_study(study.study_uid)
        if local is None:
            state["local_study_id"] = None
            state["local_series_count"] = 0
            state["local_instance_count"] = 0
            state["local_deleted_at"] = state.get("local_deleted_at") or utc_now_iso()
            state["backup_complete"] = True
            state["status"] = "complete"
            state["accounting_complete"] = True
            state["last_error"] = ""
            self.remove_rejected_archive(day, study.study_uid)
            return

        local_id = safe_text(local.get("ID"))
        try:
            started = time.monotonic()
            state["delete_attempts"] = int(state.get("delete_attempts", 0)) + 1
            self.client.delete_study(local_id)
            if self.args.settle_seconds > 0:
                time.sleep(self.args.settle_seconds)
            if self.client.lookup_local_study(study.study_uid) is not None:
                raise RuntimeError("study is still present after DELETE")
            state["local_study_id"] = None
            state["local_series_count"] = 0
            state["local_instance_count"] = 0
            state["local_deleted_at"] = utc_now_iso()
            state["backup_complete"] = True
            state["status"] = "complete"
            state["accounting_complete"] = True
            state["last_error"] = ""
            self.remove_rejected_archive(day, study.study_uid)
            stats = self.state.meta.setdefault("stats", {})
            if isinstance(stats, dict):
                stats["local_studies_deleted"] = int(stats.get("local_studies_deleted", 0)) + 1
            self.state.save_meta()
            self.state.log(
                f"Deleted local staging copy of study {study.study_uid} "
                f"in {format_duration(time.monotonic() - started)}"
            )
        except Exception as exc:
            state["backup_complete"] = False
            state["status"] = "zipped"
            state["last_error"] = f"ZIP complete but local study deletion failed: {exc}"

    def rejected_entries_for_study(self, state: dict[str, Any]) -> list[dict[str, Any]]:
        raw = state.get("rejected_instances", {})
        if not isinstance(raw, dict):
            return []
        result: list[dict[str, Any]] = []
        for sop_uid, info in raw.items():
            if isinstance(info, dict):
                entry = dict(info)
                entry.setdefault("sop_uid", sop_uid)
                result.append(entry)
        result.sort(key=lambda item: (safe_text(item.get("series_uid")), safe_text(item.get("sop_uid"))))
        return result

    def build_backup_manifest(
        self,
        day: date,
        study: Any,
        state: dict[str, Any],
        rejected_entries: list[dict[str, Any]],
    ) -> dict[str, Any]:
        manifest = {
            "created_at": utc_now_iso(),
            "script": BACKUP_MANIFEST_PRODUCER,
            "date": day.isoformat(),
            "study_uid": study.study_uid,
            "patient_id": study.patient_id,
            "patient_name": study.patient_name,
            "patient_birth_date": study.patient_birth_date,
            "study_date": study.study_date,
            "study_description": study.description,
            "accession_number": study.accession_number,
            "accounting_mode": safe_text(state.get("manifest_mode")),
            "backup_complete": True,
            "remote_series_count": study.remote_series_count,
            "remote_instance_count": study.remote_instance_count,
            "remote_exact_instance_count": parse_count(state.get("remote_exact_instance_count")),
            "local_instance_count_at_export": parse_count(state.get("local_instance_count")),
            "accounted_count": parse_count(state.get("accounted_count")),
            "missing_count": parse_count(state.get("missing_count")),
            "heuristic_target": parse_count(state.get("heuristic_target")),
            "zip_mode": self.args.zip_mode,
            "zip_name_mode": self.args.name,
            "zip_filename": safe_text(state.get("zip_filename")),
            "rejected_count": len(rejected_entries),
            "rejected_instances": [
                {
                    "sop_uid": safe_text(item.get("sop_uid")),
                    "series_uid": safe_text(item.get("series_uid")),
                    "sop_class_uid": safe_text(item.get("sop_class_uid")),
                    "failure_count": parse_count(item.get("failure_count")),
                    "last_error": safe_text(item.get("last_error")),
                    "zip_member": f"{ZIP_REJECTED_PREFIX}{safe_text(item.get('file_token', 'unknown'))}.dcm",
                }
                for item in rejected_entries
            ],
        }
        manifest["manifest_sha1"] = hashlib.sha1(
            json.dumps(manifest, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()
        return manifest

    def append_manifest_and_rejected_to_zip(
        self,
        zip_path: Path,
        day: date,
        study: Any,
        state: dict[str, Any],
        rejected_entries: list[dict[str, Any]],
    ) -> None:
        manifest = self.build_backup_manifest(day, study, state, rejected_entries)
        with zipfile.ZipFile(zip_path, "a", compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
            for item in rejected_entries:
                stored_file = safe_text(item.get("stored_file"))
                if not stored_file:
                    raise RuntimeError("Rejected instance metadata is missing stored_file")
                source = self.state.root / stored_file
                if not source.exists():
                    raise RuntimeError(f"Rejected raw DICOM file not found: {source}")
                member = f"{ZIP_REJECTED_PREFIX}{safe_text(item.get('file_token', 'unknown'))}.dcm"
                archive.write(source, member, compress_type=zipfile.ZIP_STORED)
            archive.writestr(
                ZIP_MANIFEST_NAME,
                json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
                compress_type=zipfile.ZIP_STORED,
            )

    def build_combined_zip(
        self,
        output_path: Path,
        day: date,
        study: Any,
        state: dict[str, Any],
        local_study_id: str,
        rejected_entries: list[dict[str, Any]],
    ) -> None:
        if not local_study_id and not rejected_entries:
            raise RuntimeError("Study is accounted complete but there is neither a local study nor rejected files to export")

        patient_dir = ascii_slug(study.patient_name or "UNKNOWN_PATIENT", fallback="UNKNOWN_PATIENT")
        study_dir = ascii_slug(study.description or study.study_date or iso_to_dicom_date(day), fallback="STUDY")
        bytes_written = 0

        with zipfile.ZipFile(output_path, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
            used_paths: set[str] = set()
            if local_study_id:
                series_items = self.client.get_study_series_expanded(local_study_id)
                instances = self.client.list_study_instances_for_export(local_study_id)
                if not instances and not rejected_entries:
                    raise RuntimeError(f"Study {study.study_uid} has no local instances to export")
                series_labels = _build_series_labels(series_items)
                for ordinal, instance in enumerate(instances, start=1):
                    member_name = _build_instance_member_name(
                        instance, patient_dir, study_dir, series_labels, ordinal, used_paths
                    )
                    with archive.open(member_name, "w", force_zip64=True) as target:
                        response = self.client.download_instance_file_into_handle(instance.orthanc_id, target)
                        bytes_written += int(response.get("bytes_written", 0))

            for item in rejected_entries:
                stored_file = safe_text(item.get("stored_file"))
                if not stored_file:
                    raise RuntimeError("Rejected instance metadata is missing stored_file")
                source = self.state.root / stored_file
                if not source.exists():
                    raise RuntimeError(f"Rejected raw DICOM file not found: {source}")
                member = f"{ZIP_REJECTED_PREFIX}{safe_text(item.get('file_token', 'unknown'))}.dcm"
                archive.write(source, member, compress_type=zipfile.ZIP_STORED)

            manifest = self.build_backup_manifest(day, study, state, rejected_entries)
            archive.writestr(
                ZIP_MANIFEST_NAME,
                json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
                compress_type=zipfile.ZIP_STORED,
            )

        maybe_chown(output_path, self.owner)
        if output_path.exists() and output_path.stat().st_size <= 0 and bytes_written <= 0 and not rejected_entries:
            raise RuntimeError("Created ZIP is empty")

    def final_day_cleanup(self, day: date, studies: list[Any], status: dict[str, Any]) -> None:
        study_states = status.get("studies", {}) if isinstance(status.get("studies"), dict) else {}
        for study in studies:
            state = study_states.get(study.study_uid, {})
            if not isinstance(state, dict) or state.get("backup_complete") is not True:
                continue
            self.maybe_complete_from_existing_zip(day, study, state)
        status["updated_at"] = utc_now_iso()


class ExportWorkflowMixin:
    def output_day_dir(self, day: date) -> Path:
        return self.backup_dir / day.strftime("%Y") / day.strftime("%m") / day.strftime("%d")

    def refresh_day_inventory(self, day: date, studies_path: Path, status: dict[str, Any]) -> bool:
        studies = self.client.find_studies_for_day(day, page_size=self.args.page_size)
        payload = {
            "date": day.isoformat(),
            "queried_at": now_iso(),
            "count": len(studies),
            "studies": [self.study_to_json(study, day) for study in studies],
        }
        previous = load_json(studies_path, default={}) or {}
        old_uids = {
            item.get("study_uid")
            for item in previous.get("studies", [])
            if isinstance(item, dict) and item.get("study_uid")
        }
        new_uids = {study.study_uid for study in studies}
        atomic_write_json(studies_path, payload, self.owner)
        self.merge_studies_into_status(day, studies, status)
        return new_uids != old_uids

    def merge_studies_into_status(self, day: date, studies: list[Any], status: dict[str, Any]) -> None:
        day_entries = status.setdefault("studies", {})
        if not isinstance(day_entries, dict):
            raise RuntimeError("status.json has an invalid 'studies' structure")

        live_uids = {study.study_uid for study in studies}
        for study_uid, entry in day_entries.items():
            if not isinstance(entry, dict):
                continue
            if study_uid not in live_uids:
                entry["required"] = False
                if entry.get("status") != "completed":
                    entry["status"] = "absent"
                    entry["error"] = "Not currently present in Orthanc during the latest inventory refresh"
                entry["updated_at"] = now_iso()

        for study in studies:
            entry = day_entries.get(study.study_uid)
            if not isinstance(entry, dict):
                entry = {
                    "study_uid": study.study_uid,
                    "orthanc_id": study.orthanc_id,
                    "patient_name": study.patient_name,
                    "patient_birth_date": study.patient_birth_date,
                    "study_date": normalize_study_day(study.study_date, day),
                    "study_description": study.study_description,
                    "accession_number": study.accession_number,
                    "is_stable": study.is_stable,
                    "filename": None,
                    "status": "pending",
                    "attempts": 0,
                    "bytes": 0,
                    "error": "",
                    "required": True,
                    "created_at": now_iso(),
                    "updated_at": now_iso(),
                    "completed_at": None,
                    "last_started_at": None,
                }
                day_entries[study.study_uid] = entry
            else:
                entry["orthanc_id"] = study.orthanc_id
                entry["patient_name"] = study.patient_name
                entry["patient_birth_date"] = study.patient_birth_date
                entry["study_date"] = normalize_study_day(study.study_date, day)
                entry["study_description"] = study.study_description
                entry["accession_number"] = study.accession_number
                entry["is_stable"] = study.is_stable
                entry["required"] = True
                entry["updated_at"] = now_iso()

        self.assign_filenames(day, status)
        status["updated_at"] = now_iso()

    def assign_filenames(self, day: date, status: dict[str, Any]) -> None:
        entries = list(self.iter_day_entries(status))
        used_names: set[str] = set()
        for entry in entries:
            filename = entry.get("filename")
            if isinstance(filename, str) and filename.strip():
                normalized = filename.strip()
                if normalized in used_names:
                    raise RuntimeError(f"Duplicate filename assignment in status.json: {normalized}")
                entry["filename"] = normalized
                used_names.add(normalized)

        for entry in entries:
            if entry.get("filename"):
                continue
            study_uid = str(entry["study_uid"])
            if self.args.name == "uid":
                base = truncate_with_hash(ascii_slug(study_uid, fallback="STUDY"), limit=220)
            else:
                study = SimpleNamespace(
                    patient_name=str(entry.get("patient_name") or ""),
                    patient_birth_date=str(entry.get("patient_birth_date") or ""),
                    study_date=str(entry.get("study_date") or ""),
                )
                base = build_patient_base(study, day)
            candidate = f"{base}.zip"
            if candidate not in used_names:
                entry["filename"] = candidate
                used_names.add(candidate)
                continue
            suffix = 2
            while True:
                candidate = f"{base}_{suffix}.zip"
                if candidate not in used_names:
                    entry["filename"] = candidate
                    used_names.add(candidate)
                    break
                suffix += 1

    def iter_day_entries(self, status: dict[str, Any]) -> Iterable[dict[str, Any]]:
        studies = status.get("studies", {})
        if not isinstance(studies, dict):
            return []
        return sorted(
            studies.values(),
            key=lambda entry: (
                str(entry.get("study_date") or ""),
                str(entry.get("filename") or ""),
                str(entry.get("study_uid") or ""),
            ),
        )

    def sync_existing_completed_files(self, day_dir: Path, status: dict[str, Any]) -> None:
        for entry in self.iter_day_entries(status):
            filename = entry.get("filename")
            if not isinstance(filename, str) or not filename:
                continue
            final_zip = day_dir / filename
            partial_zip = day_dir / f"{filename}.part"
            if partial_zip.exists() and not final_zip.exists():
                partial_zip.unlink(missing_ok=True)
            if final_zip.exists():
                try:
                    validate_zip_file(final_zip)
                except Exception as exc:
                    self.logger.error(f"Existing ZIP is invalid for study {entry['study_uid']}: {final_zip} ({exc})")
                    final_zip.unlink(missing_ok=True)
                    entry["status"] = "pending"
                    entry["error"] = str(exc)
                    entry["updated_at"] = now_iso()
                else:
                    entry["status"] = "completed"
                    entry["bytes"] = final_zip.stat().st_size
                    entry["error"] = ""
                    entry["completed_at"] = entry.get("completed_at") or now_iso()
                    entry["updated_at"] = now_iso()

    def export_one_study(
        self,
        day: date,
        day_dir: Path,
        status: dict[str, Any],
        entry: dict[str, Any],
        status_path: Path,
        progress_path: Path,
    ) -> bool:
        study_uid = str(entry["study_uid"])
        filename = str(entry["filename"])
        final_zip = day_dir / filename
        partial_zip = day_dir / f"{filename}.part"
        partial_zip.unlink(missing_ok=True)

        if final_zip.exists():
            try:
                validate_zip_file(final_zip)
            except (ZipValidationError, zipfile.BadZipFile, ValueError):
                try:
                    final_zip.unlink(missing_ok=True)
                except OSError:
                    pass
            else:
                entry["status"] = "completed"
                entry["bytes"] = final_zip.stat().st_size
                entry["error"] = ""
                entry["completed_at"] = entry.get("completed_at") or now_iso()
                entry["updated_at"] = now_iso()
                self.write_day_status(status_path, progress_path, status)
                return True

        for attempt in range(1, self.args.retries + 1):
            entry["status"] = "downloading"
            entry["attempts"] = int(entry.get("attempts", 0)) + 1
            entry["last_started_at"] = now_iso()
            entry["updated_at"] = now_iso()
            entry["error"] = ""
            self.write_day_status(status_path, progress_path, status)
            self.logger.info(
                f"Exporting study {study_uid} to {final_zip} (attempt {attempt}/{self.args.retries} in this run)"
            )
            try:
                if self.args.zip_mode == "archive":
                    response = self.client.download_study_archive(str(entry["orthanc_id"]), partial_zip)
                else:
                    response = self.build_local_stored_zip(day, entry, partial_zip)
                maybe_chown(partial_zip, self.owner)
                validate_zip_file(partial_zip)
                os.replace(partial_zip, final_zip)
                maybe_chown(final_zip, self.owner)
                entry["status"] = "completed"
                entry["bytes"] = int(response.get("bytes_written", final_zip.stat().st_size))
                entry["error"] = ""
                entry["completed_at"] = now_iso()
                entry["updated_at"] = now_iso()
                self.write_day_status(status_path, progress_path, status)
                self.logger.info(f"Completed study {study_uid} -> {final_zip}")
                return True
            except (
                OrthancApiError,
                OrthancNetworkError,
                OSError,
                ZipValidationError,
                zipfile.BadZipFile,
                ValueError,
            ) as exc:
                try:
                    partial_zip.unlink(missing_ok=True)
                except OSError:
                    pass
                if getattr(exc, "status", None) == 404:
                    refreshed = self.client.find_study_by_uid(study_uid)
                    if refreshed is None:
                        entry["status"] = "error"
                        entry["error"] = (
                            f"Study {study_uid} is no longer present in Orthanc while exporting {day.isoformat()}"
                        )
                        entry["updated_at"] = now_iso()
                        self.write_day_status(status_path, progress_path, status)
                        self.logger.error(entry["error"])
                        return False
                    entry["orthanc_id"] = refreshed.orthanc_id
                entry["status"] = "error"
                entry["error"] = str(exc)
                entry["updated_at"] = now_iso()
                self.write_day_status(status_path, progress_path, status)
                self.logger.error(f"Failed study {study_uid} attempt {attempt}/{self.args.retries}: {exc}")
            except RuntimeError as exc:
                try:
                    partial_zip.unlink(missing_ok=True)
                except OSError:
                    pass
                if "has no instances to export" not in str(exc):
                    raise
                entry["status"] = "error"
                entry["error"] = str(exc)
                entry["updated_at"] = now_iso()
                self.write_day_status(status_path, progress_path, status)
                self.logger.error(f"Failed study {study_uid}: {exc}")
                return False
            except Exception:
                try:
                    partial_zip.unlink(missing_ok=True)
                except OSError:
                    pass
                raise
            if attempt < self.args.retries:
                time.sleep(self.args.retry_delay)
        return False

    def build_local_stored_zip(self, day: date, entry: dict[str, Any], output_path: Path) -> dict[str, Any]:
        study = SimpleNamespace(
            orthanc_id=str(entry["orthanc_id"]),
            study_uid=str(entry["study_uid"]),
            patient_name=str(entry.get("patient_name") or ""),
            patient_birth_date=str(entry.get("patient_birth_date") or ""),
            study_date=str(entry.get("study_date") or ""),
            study_description=str(entry.get("study_description") or ""),
            accession_number=str(entry.get("accession_number") or ""),
            is_stable=entry.get("is_stable") if isinstance(entry.get("is_stable"), bool) else None,
        )
        series_items = self.client.list_study_series(study.orthanc_id)
        instances = self.client.list_study_instances(study.orthanc_id)
        if not instances:
            raise RuntimeError(f"Study {study.study_uid} has no instances to export")

        patient_dir = ascii_slug(study.patient_name or "UNKNOWN_PATIENT", fallback="UNKNOWN_PATIENT")
        study_dir = ascii_slug(study.study_description or normalize_study_day(study.study_date, day), fallback="STUDY")
        series_labels = _build_series_labels(series_items)
        used_paths: set[str] = set()
        bytes_written = 0
        with zipfile.ZipFile(output_path, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
            for ordinal, instance in enumerate(instances, start=1):
                member_name = _build_instance_member_name(
                    instance, patient_dir, study_dir, series_labels, ordinal, used_paths
                )
                with archive.open(member_name, "w", force_zip64=True) as target:
                    response = self.client.download_instance_file_into_handle(instance.orthanc_id, target)
                    bytes_written += int(response.get("bytes_written", 0))
        return {
            "status": 200,
            "content_type": "application/zip",
            "bytes_written": output_path.stat().st_size if output_path.exists() else bytes_written,
            "source_bytes": bytes_written,
            "instance_count": len(instances),
        }

    def write_day_status(self, status_path: Path, progress_path: Path, status: dict[str, Any]) -> None:
        status["updated_at"] = now_iso()
        atomic_write_json(status_path, status, self.owner)
        lines = [
            "status\tfilename\tstudy_uid\torthanc_id\tbytes\tattempts\tstudy_date\tpatient_name\tpatient_birth_date\tupdated_at\terror"
        ]
        for entry in self.iter_day_entries(status):
            lines.append(
                "\t".join(
                    [
                        self.tsv(str(entry.get("status") or "")),
                        self.tsv(str(entry.get("filename") or "")),
                        self.tsv(str(entry.get("study_uid") or "")),
                        self.tsv(str(entry.get("orthanc_id") or "")),
                        self.tsv(str(entry.get("bytes") or 0)),
                        self.tsv(str(entry.get("attempts") or 0)),
                        self.tsv(str(entry.get("study_date") or "")),
                        self.tsv(str(entry.get("patient_name") or "")),
                        self.tsv(str(entry.get("patient_birth_date") or "")),
                        self.tsv(str(entry.get("updated_at") or "")),
                        self.tsv(str(entry.get("error") or "")),
                    ]
                )
            )
        atomic_write_text(progress_path, "\n".join(lines) + "\n", self.owner)

    @staticmethod
    def tsv(value: str) -> str:
        return value.replace("\t", " ").replace("\n", " ").replace("\r", " ")

    @staticmethod
    def study_to_json(study: Any, day: date) -> dict[str, Any]:
        return {
            "orthanc_id": study.orthanc_id,
            "study_uid": study.study_uid,
            "patient_name": study.patient_name,
            "patient_birth_date": study.patient_birth_date,
            "study_date": normalize_study_day(study.study_date, day),
            "study_description": study.study_description,
            "accession_number": study.accession_number,
            "is_stable": study.is_stable,
        }
