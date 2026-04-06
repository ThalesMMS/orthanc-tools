#!/usr/bin/env python3
"""Sequential remote-to-ZIP backup by study date using Orthanc as a staging store.

This script combines the behavior of:
- day-by-day remote PACS backfill into local Orthanc, with exact verification
  whenever IMAGE-level manifests are available and heuristic fallback otherwise;
- day-by-day export of local Orthanc studies into one ZIP per study.

Operational model
-----------------
1. Query the remote PACS for one StudyDate.
2. For each remote study of that day, make local Orthanc contain a complete
   backed-up copy of the study:
   - exact Study/Series/SOP parity when supported by the remote PACS;
   - otherwise a count heuristic, with repeated Orthanc import failures being
     archived as rejected-but-backed-up raw DICOM files.
3. Create a final ZIP for the study under backup/YYYY/MM/DD/.
   - The ZIP always contains a manifest file under "__backup__/manifest.json".
   - If some objects were retrieved from the remote PACS but rejected by
     Orthanc, the ZIP also contains those raw DICOM files under
     "__backup__/rejected/".
4. Delete the local DICOM study from Orthanc as soon as the ZIP is valid.
5. Advance to the next day only when every study of the day has a valid ZIP and
   no longer occupies local Orthanc storage.

Resumability
------------
The script persists machine-readable state, human-readable TSV progress, logs,
remote day caches, remote manifests, and temporary rejected raw DICOM files on
disk. If interrupted, it resumes from the last unfinished day and will not go
back to the first day. Because completed studies are deleted immediately after
their ZIP is validated, local Orthanc usually only contains the still-pending
studies of the current day.

Dependencies
------------
- Python 3 standard library only
- Local Orthanc reachable over REST
- dcmtk installed locally: getscu and dcmdump
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Iterable
from urllib import parse

from orthanc_tools.config import build_base_url, first_registered_user, read_json_file as load_json_file, resolve_config_paths
from orthanc_tools.dicom import (
    compute_default_end_date,
    extract_dicom_ids,
    iso_to_dicom_date,
    nullable_int,
    parse_count,
    parse_iso_date,
    pick_tag,
    safe_text,
    sanitize_tsv,
    short_uid,
    unique_instance_records,
)
from orthanc_tools.orthanc_api import OrthancApiError, OrthancRestClient
from orthanc_tools.state import (
    Ownership,
    append_text,
    atomic_write_json,
    atomic_write_text,
    default_owner,
    ensure_dir,
    local_now_human,
    maybe_chown,
    preferred_home,
    utc_now_iso,
)
from orthanc_tools.zip_export import (
    ZIP_MANIFEST_NAME,
    ZIP_REJECTED_PREFIX,
    ascii_slug,
    build_patient_base,
    read_zip_manifest,
    truncate_with_hash,
    validate_zip_file,
)


STOP_REQUESTED = False


def _handle_signal(signum: int, _frame: Any) -> None:
    global STOP_REQUESTED
    STOP_REQUESTED = True
    name = signal.Signals(signum).name
    print(f"\nSignal received: {name}. Finishing the current operation.", file=sys.stderr)


signal.signal(signal.SIGINT, _handle_signal)
signal.signal(signal.SIGTERM, _handle_signal)


def cli_error(message: str) -> None:
    print(f"ERROR: {message}", file=sys.stderr)
    raise SystemExit(1)


@dataclass
class OrthancSettings:
    base_url: str
    username: str
    password: str
    dicom_aet: str


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
    mode: str  # "study" or "instances"
    missing: list[dict[str, str]]


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


@dataclass(frozen=True)
class ExportInstanceInfo:
    orthanc_id: str
    parent_series_id: str
    sop_instance_uid: str
    instance_number: str
    index_in_series: int | None
    file_size: int | None


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


def extract_resource_id(response: Any) -> str:
    if isinstance(response, dict):
        for key in ("ID", "Path", "Uri", "URL"):
            value = response.get(key)
            if isinstance(value, str) and value:
                if "/" in value:
                    return value.rsplit("/", 1)[-1]
                return value
    if isinstance(response, str) and response:
        if "/" in response:
            return response.rstrip("/").rsplit("/", 1)[-1]
        return response
    raise RuntimeError(f"Could not extract resource ID from response: {response!r}")


class OrthancClient(OrthancRestClient):
    def __init__(self, settings: OrthancSettings, timeout: float = 60.0):
        self.settings = settings
        super().__init__(settings.base_url, settings.username, settings.password, timeout=timeout)

    def download(self, path: str, output_path: Path) -> dict[str, Any]:
        return self.request("GET", path, accept="application/octet-stream", stream_to=output_path)

    def download_into_handle(self, path: str, handle: Any) -> dict[str, Any]:
        return self.request("GET", path, accept="application/octet-stream", stream_handle=handle)

    def system(self) -> dict[str, Any]:
        response = self.get("/system")
        if not isinstance(response, dict):
            raise RuntimeError("Unexpected /system response from Orthanc.")
        return response

    def put_modality(self, name: str, aet: str, host: str, port: int) -> Any:
        payload = {
            "AET": aet,
            "Host": host,
            "Port": int(port),
            "Manufacturer": "Generic",
            "AllowEcho": True,
            "AllowFind": True,
            "AllowGet": True,
            "AllowMove": True,
            "AllowStore": False,
        }
        return self.put(f"/modalities/{parse.quote(name, safe='')}", payload)

    def delete_modality(self, name: str) -> Any:
        return self.delete(f"/modalities/{parse.quote(name, safe='')}")

    def echo_modality(self, name: str, timeout: int = 10) -> Any:
        return self.post(f"/modalities/{parse.quote(name, safe='')}/echo", {"Timeout": int(timeout)})

    def create_remote_query(self, modality: str, level: str, query_fields: dict[str, str]) -> str:
        response = self.post(
            f"/modalities/{parse.quote(modality, safe='')}/query",
            {"Level": level, "Query": query_fields},
        )
        return extract_resource_id(response)

    def create_child_query(self, query_id: str, answer_id: str, child_level: str, query_fields: dict[str, str]) -> str:
        response = self.post(
            f"/queries/{parse.quote(query_id, safe='')}/answers/{parse.quote(str(answer_id), safe='')}/query-{child_level}",
            {"Query": query_fields},
        )
        return extract_resource_id(response)

    def get_query_answers(self, query_id: str) -> list[str]:
        response = self.get(f"/queries/{parse.quote(query_id, safe='')}/answers")
        if not isinstance(response, list):
            raise RuntimeError(f"Unexpected /queries/{query_id}/answers response.")
        return [str(item) for item in response]

    def get_query_answer_content(self, query_id: str, answer_id: str) -> dict[str, Any]:
        response = self.get(f"/queries/{parse.quote(query_id, safe='')}/answers/{parse.quote(str(answer_id), safe='')}/content")
        if not isinstance(response, dict):
            raise RuntimeError(f"Unexpected answer content for query {query_id}, answer {answer_id}.")
        return response

    def delete_query(self, query_id: str) -> Any:
        return self.delete(f"/queries/{parse.quote(query_id, safe='')}")

    def lookup_local_study(self, study_uid: str) -> dict[str, Any] | None:
        payload = {
            "Level": "Study",
            "Expand": True,
            "Query": {"StudyInstanceUID": study_uid},
        }
        response = self.post("/tools/find", payload)
        if not isinstance(response, list):
            raise RuntimeError("Unexpected /tools/find response.")
        if not response:
            return None
        if len(response) > 1:
            raise RuntimeError(f"Multiple local studies found for StudyInstanceUID {study_uid}.")
        item = response[0]
        if not isinstance(item, dict):
            raise RuntimeError("Expanded /tools/find response item is not an object.")
        return item

    def get_study_statistics(self, study_id: str) -> dict[str, Any]:
        response = self.get(f"/studies/{parse.quote(study_id, safe='')}/statistics")
        if not isinstance(response, dict):
            raise RuntimeError(f"Unexpected statistics response for study {study_id}.")
        return response

    def get_study_series_expanded(self, study_id: str) -> list[dict[str, Any]]:
        response = self.get(f"/studies/{parse.quote(study_id, safe='')}/series?expand")
        if not isinstance(response, list) or not all(isinstance(item, dict) for item in response):
            raise RuntimeError(f"Unexpected /studies/{study_id}/series?expand response.")
        return response  # type: ignore[return-value]

    def get_study_instances_expanded(self, study_id: str) -> list[dict[str, Any]]:
        response = self.get(f"/studies/{parse.quote(study_id, safe='')}/instances?expand")
        if not isinstance(response, list) or not all(isinstance(item, dict) for item in response):
            raise RuntimeError(f"Unexpected /studies/{study_id}/instances?expand response.")
        return response  # type: ignore[return-value]

    def list_study_instances_for_export(self, study_id: str) -> list[ExportInstanceInfo]:
        response = self.get(f"/studies/{parse.quote(study_id, safe='')}/instances")
        if not isinstance(response, list):
            raise RuntimeError(f"/studies/{study_id}/instances did not return a list")
        result: list[ExportInstanceInfo] = []
        for item in response:
            if not isinstance(item, dict):
                raise RuntimeError(f"/studies/{study_id}/instances returned a non-object item")
            instance_id = safe_text(item.get("ID")).strip()
            if not instance_id:
                raise RuntimeError(f"/studies/{study_id}/instances returned an item without ID")
            result.append(
                ExportInstanceInfo(
                    orthanc_id=instance_id,
                    parent_series_id=safe_text(item.get("ParentSeries")).strip(),
                    sop_instance_uid=(pick_tag(item, "SOPInstanceUID") or "").strip(),
                    instance_number=(pick_tag(item, "InstanceNumber") or "").strip(),
                    index_in_series=item.get("IndexInSeries") if isinstance(item.get("IndexInSeries"), int) else None,
                    file_size=item.get("FileSize") if isinstance(item.get("FileSize"), int) else None,
                )
            )
        result.sort(
            key=lambda item: (
                item.parent_series_id,
                item.index_in_series if item.index_in_series is not None else 10**9,
                item.instance_number,
                item.orthanc_id,
            )
        )
        return result

    def import_dicom_file(self, path: Path) -> Any:
        return self.post("/instances", path.read_bytes(), content_type="application/dicom")

    def delete_study(self, study_id: str) -> Any:
        return self.delete(f"/studies/{parse.quote(study_id, safe='')}")

    def download_study_archive(self, study_id: str, output_path: Path) -> dict[str, Any]:
        return self.download(f"/studies/{parse.quote(study_id, safe='')}/archive", output_path)

    def download_instance_file_into_handle(self, instance_id: str, handle: Any) -> dict[str, Any]:
        return self.download_into_handle(f"/instances/{parse.quote(instance_id, safe='')}/file", handle)


class StateManager:
    def __init__(
        self,
        root: Path,
        owner: Ownership | None,
        start_date: dt.date,
        end_date: dt.date,
        remote_name: str,
        remote_aet: str,
        remote_host: str,
        remote_port: int,
        orthanc_base_url: str,
        orthanc_user: str,
        calling_aet: str,
        backup_dir: Path,
        name_mode: str,
        zip_mode: str,
    ):
        self.root = root
        self.owner = owner
        self.logs_dir = self.root / "logs"
        self.days_dir = self.root / "days"
        self.meta_path = self.root / "state.json"
        self.current_date_path = self.root / "current-date.txt"
        self.summary_path = self.root / "summary.tsv"
        ensure_dir(self.root, owner)
        ensure_dir(self.logs_dir, owner)
        ensure_dir(self.days_dir, owner)
        self.meta = self._load_or_init_meta(
            start_date=start_date,
            end_date=end_date,
            remote_name=remote_name,
            remote_aet=remote_aet,
            remote_host=remote_host,
            remote_port=remote_port,
            orthanc_base_url=orthanc_base_url,
            orthanc_user=orthanc_user,
            calling_aet=calling_aet,
            backup_dir=backup_dir,
            name_mode=name_mode,
            zip_mode=zip_mode,
        )

    def _load_or_init_meta(
        self,
        *,
        start_date: dt.date,
        end_date: dt.date,
        remote_name: str,
        remote_aet: str,
        remote_host: str,
        remote_port: int,
        orthanc_base_url: str,
        orthanc_user: str,
        calling_aet: str,
        backup_dir: Path,
        name_mode: str,
        zip_mode: str,
    ) -> dict[str, Any]:
        now = utc_now_iso()
        template = {
            "created_at": now,
            "updated_at": now,
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "next_date": start_date.isoformat(),
            "remote": {
                "name": remote_name,
                "aet": remote_aet,
                "host": remote_host,
                "port": int(remote_port),
            },
            "orthanc": {
                "base_url": orthanc_base_url,
                "user": orthanc_user,
                "calling_aet": calling_aet,
            },
            "backup": {
                "dir": str(backup_dir),
                "name_mode": name_mode,
                "zip_mode": zip_mode,
            },
            "stats": {
                "days_done": 0,
                "studies_complete": 0,
                "zips_complete": 0,
                "local_studies_deleted": 0,
                "instances_imported": 0,
                "instances_rejected_archived": 0,
            },
        }
        if not self.meta_path.exists():
            atomic_write_json(self.meta_path, template, self.owner)
            atomic_write_text(self.current_date_path, start_date.isoformat() + "\n", self.owner)
            if not self.summary_path.exists():
                atomic_write_text(
                    self.summary_path,
                    "date\tstatus\tremote_studies\tcomplete_studies\tpending_studies\trejected_instances\tupdated_at\n",
                    self.owner,
                )
            return template

        meta = load_json_file(self.meta_path)
        if not isinstance(meta, dict):
            cli_error(f"Invalid state file: {self.meta_path}")

        expected_pairs = {
            ("start_date",): start_date.isoformat(),
            ("remote", "aet"): remote_aet,
            ("remote", "host"): remote_host,
            ("remote", "port"): int(remote_port),
            ("orthanc", "base_url"): orthanc_base_url,
            ("orthanc", "calling_aet"): calling_aet,
            ("backup", "dir"): str(backup_dir),
            ("backup", "name_mode"): name_mode,
            ("backup", "zip_mode"): zip_mode,
        }
        for path_parts, expected_value in expected_pairs.items():
            current = meta
            for key in path_parts:
                if not isinstance(current, dict) or key not in current:
                    cli_error(
                        f"State directory {self.root} is missing {'.'.join(path_parts)}. "
                        "Use a clean --state-dir or fix the state file."
                    )
                current = current[key]
            if current != expected_value:
                cli_error(
                    f"State directory {self.root} belongs to another run. "
                    f"Field {'.'.join(path_parts)}={current!r}, expected {expected_value!r}."
                )
        meta["end_date"] = end_date.isoformat()
        meta["updated_at"] = now
        atomic_write_json(self.meta_path, meta, self.owner)
        if not self.summary_path.exists():
            atomic_write_text(
                self.summary_path,
                "date\tstatus\tremote_studies\tcomplete_studies\tpending_studies\trejected_instances\tupdated_at\n",
                self.owner,
            )
        atomic_write_text(
            self.current_date_path,
            str(meta.get("next_date", start_date.isoformat())) + "\n",
            self.owner,
        )
        return meta

    def save_meta(self) -> None:
        self.meta["updated_at"] = utc_now_iso()
        atomic_write_json(self.meta_path, self.meta, self.owner)
        atomic_write_text(self.current_date_path, str(self.meta.get("next_date", "")) + "\n", self.owner)

    def get_next_date(self) -> dt.date:
        return parse_iso_date(str(self.meta.get("next_date", self.meta["start_date"])))

    def set_next_date(self, value: dt.date) -> None:
        self.meta["next_date"] = value.isoformat()
        self.save_meta()

    def day_dir(self, day: dt.date) -> Path:
        path = self.days_dir / day.isoformat()
        ensure_dir(path, self.owner)
        return path

    def day_cache_path(self, day: dt.date) -> Path:
        return self.day_dir(day) / "remote-studies.json"

    def day_status_path(self, day: dt.date) -> Path:
        return self.day_dir(day) / "status.json"

    def day_progress_tsv_path(self, day: dt.date) -> Path:
        return self.day_dir(day) / "progress.tsv"

    def day_done_path(self, day: dt.date) -> Path:
        return self.day_dir(day) / "DONE"

    def day_manifest_dir(self, day: dt.date) -> Path:
        path = self.day_dir(day) / "remote-manifests"
        ensure_dir(path, self.owner)
        return path

    def day_rejected_dir(self, day: dt.date) -> Path:
        path = self.day_dir(day) / "rejected"
        ensure_dir(path, self.owner)
        return path

    def load_day_cache(self, day: dt.date) -> list[RemoteStudy] | None:
        path = self.day_cache_path(day)
        if not path.exists():
            return None
        payload = load_json_file(path)
        if not isinstance(payload, dict):
            return None
        studies_data = payload.get("studies")
        if not isinstance(studies_data, list):
            return None
        result: list[RemoteStudy] = []
        for item in studies_data:
            if not isinstance(item, dict):
                continue
            study_uid = safe_text(item.get("study_uid")).strip()
            if not study_uid:
                continue
            result.append(
                RemoteStudy(
                    study_uid=study_uid,
                    patient_id=safe_text(item.get("patient_id")),
                    patient_name=safe_text(item.get("patient_name")),
                    patient_birth_date=safe_text(item.get("patient_birth_date")),
                    study_date=safe_text(item.get("study_date")),
                    description=safe_text(item.get("description")),
                    accession_number=safe_text(item.get("accession_number")),
                    remote_series_count=parse_count(item.get("remote_series_count")),
                    remote_instance_count=parse_count(item.get("remote_instance_count")),
                )
            )
        return result

    def save_day_cache(self, day: dt.date, studies: list[RemoteStudy]) -> None:
        payload = {
            "date": day.isoformat(),
            "cached_at": utc_now_iso(),
            "studies": [asdict(study) for study in studies],
        }
        atomic_write_json(self.day_cache_path(day), payload, self.owner)

    def load_day_status(self, day: dt.date) -> dict[str, Any]:
        path = self.day_status_path(day)
        if not path.exists():
            return {
                "date": day.isoformat(),
                "last_pass": 0,
                "stalled_passes": 0,
                "studies": {},
                "last_updated_at": utc_now_iso(),
            }
        payload = load_json_file(path)
        if not isinstance(payload, dict):
            cli_error(f"Invalid day status file: {path}")
        payload.setdefault("date", day.isoformat())
        payload.setdefault("last_pass", 0)
        payload.setdefault("stalled_passes", 0)
        payload.setdefault("studies", {})
        payload.setdefault("last_updated_at", utc_now_iso())
        if not isinstance(payload["studies"], dict):
            cli_error(f"Invalid studies map in {path}")
        return payload

    def save_day_status(self, day: dt.date, status: dict[str, Any]) -> None:
        status["last_updated_at"] = utc_now_iso()
        atomic_write_json(self.day_status_path(day), status, self.owner)

    def write_day_progress_tsv(self, day: dt.date, studies: list[RemoteStudy], status: dict[str, Any]) -> None:
        lines = [
            "study_uid\tpatient_id\tpatient_name\tstudy_date\tremote_series\tremote_instances\tmanifest_mode\taccounted\tmissing\tlocal_instances\trejected_instances\tzip_filename\tzip_bytes\tbackup_complete\tstatus\tlast_error\tlast_checked_at"
        ]
        study_states = status.get("studies", {})
        for study in studies:
            s = study_states.get(study.study_uid, {}) if isinstance(study_states, dict) else {}
            rejected_count = len(s.get("rejected_instances", {})) if isinstance(s.get("rejected_instances"), dict) else 0
            lines.append(
                "\t".join(
                    [
                        sanitize_tsv(study.study_uid),
                        sanitize_tsv(study.patient_id),
                        sanitize_tsv(study.patient_name),
                        sanitize_tsv(study.study_date),
                        str(nullable_int(study.remote_series_count)),
                        str(nullable_int(study.remote_instance_count)),
                        sanitize_tsv(safe_text(s.get("manifest_mode", "unknown"))),
                        str(nullable_int(parse_count(s.get("accounted_count")))),
                        str(nullable_int(parse_count(s.get("missing_count")))),
                        str(nullable_int(parse_count(s.get("local_instance_count")))),
                        str(rejected_count),
                        sanitize_tsv(safe_text(s.get("zip_filename", ""))),
                        str(nullable_int(parse_count(s.get("zip_bytes")))),
                        sanitize_tsv(safe_text(s.get("backup_complete", False))),
                        sanitize_tsv(safe_text(s.get("status", "pending"))),
                        sanitize_tsv(safe_text(s.get("last_error", ""))),
                        sanitize_tsv(safe_text(s.get("last_checked_at", ""))),
                    ]
                )
            )
        atomic_write_text(self.day_progress_tsv_path(day), "\n".join(lines) + "\n", self.owner)

    def mark_day_done(self, day: dt.date, studies: list[RemoteStudy], status: dict[str, Any], mode: str) -> None:
        complete_count = 0
        rejected_total = 0
        for s in status.get("studies", {}).values():
            if isinstance(s, dict):
                if s.get("backup_complete") is True:
                    complete_count += 1
                if isinstance(s.get("rejected_instances"), dict):
                    rejected_total += len(s.get("rejected_instances", {}))
        content = {
            "date": day.isoformat(),
            "completed_at": utc_now_iso(),
            "mode": mode,
            "remote_studies": len(studies),
            "complete_studies": complete_count,
            "rejected_instances": rejected_total,
        }
        atomic_write_json(self.day_done_path(day), content, self.owner)
        append_text(
            self.summary_path,
            "\t".join(
                [
                    day.isoformat(),
                    mode,
                    str(len(studies)),
                    str(complete_count),
                    str(max(0, len(studies) - complete_count)),
                    str(rejected_total),
                    utc_now_iso(),
                ]
            )
            + "\n",
            self.owner,
        )
        stats = self.meta.setdefault("stats", {})
        if not isinstance(stats, dict):
            stats = {}
            self.meta["stats"] = stats
        stats["days_done"] = int(stats.get("days_done", 0)) + 1
        stats["studies_complete"] = int(stats.get("studies_complete", 0)) + complete_count
        stats["zips_complete"] = int(stats.get("zips_complete", 0)) + complete_count
        stats["instances_rejected_archived"] = int(stats.get("instances_rejected_archived", 0)) + rejected_total
        self.save_meta()

    def prune_day_manifests(self, day: dt.date) -> None:
        manifest_dir = self.day_manifest_dir(day)
        if manifest_dir.exists():
            shutil.rmtree(manifest_dir)

    def log(self, message: str) -> None:
        line = f"{local_now_human()} {message}\n"
        append_text(self.logs_dir / "run.log", line, self.owner)
        print(line, end="")

    def error(self, message: str) -> None:
        line = f"{local_now_human()} {message}\n"
        append_text(self.logs_dir / "errors.log", line, self.owner)
        append_text(self.logs_dir / "run.log", line, self.owner)
        print(line, end="", file=sys.stderr)


def load_orthanc_settings(args: argparse.Namespace) -> OrthancSettings:
    orthanc_config_path, credentials_config_path = resolve_config_paths(
        args.config_dir,
        args.orthanc_config,
        args.credentials_config,
    )

    config: dict[str, Any] = {}
    if orthanc_config_path.exists():
        payload = load_json_file(orthanc_config_path)
        if isinstance(payload, dict):
            config = payload
    elif not args.base_url or not args.calling_aet:
        cli_error(
            f"Orthanc config file not found: {orthanc_config_path}. "
            "Provide --base-url and --calling-aet explicitly, or make orthanc.json readable."
        )

    username = args.user
    password = args.password
    if (username is None or password is None) and credentials_config_path.exists():
        credentials = load_json_file(credentials_config_path)
        if isinstance(credentials, dict):
            user_from_cfg, password_from_cfg = first_registered_user(credentials)
            username = username or user_from_cfg
            password = password or password_from_cfg
    if username is None or password is None:
        cli_error("Orthanc credentials not provided. Use --user/--password or make credentials.json readable.")

    http_port = parse_count(config.get("HttpPort")) or 8042
    dicom_aet = safe_text(config.get("DicomAet")) or "ORTHANC"
    base_url = args.base_url or build_base_url(config, default_port=http_port)
    calling_aet = args.calling_aet or dicom_aet
    args.calling_aet = calling_aet

    return OrthancSettings(
        base_url=base_url,
        username=username,
        password=password,
        dicom_aet=calling_aet,
    )


def material_study_state(state: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "status",
        "manifest_mode",
        "accounting_complete",
        "backup_complete",
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
        "zip_attempts",
        "delete_attempts",
        "zip_filename",
        "zip_bytes",
        "zip_manifest_hash",
        "local_deleted_at",
        "manifest_error",
    )
    return {key: state.get(key) for key in keys}


class BackupZipApp:
    def __init__(self, args: argparse.Namespace, client: OrthancClient, state: StateManager, owner: Ownership | None):
        self.args = args
        self.client = client
        self.state = state
        self.owner = owner
        self.backup_dir = args.backup_dir
        self.exact_manifest_failures: dict[str, str] = {}

    def run(self) -> int:
        self.prepare_remote_modality()
        start = self.state.get_next_date()
        end = parse_iso_date(self.state.meta["end_date"])
        if start > end:
            self.state.log("Nothing to do: next_date is already beyond end_date.")
            return 0

        current = start
        while current <= end and not STOP_REQUESTED:
            if self.state.day_done_path(current).exists() and not self.args.recheck_completed_days:
                self.state.log(f"Skipping completed day {current.isoformat()} (DONE marker found).")
                self.state.set_next_date(current + dt.timedelta(days=1))
                current += dt.timedelta(days=1)
                continue
            ok = self.process_day(current)
            if not ok:
                return 2
            current = self.state.get_next_date()
        return 130 if STOP_REQUESTED else 0

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

    def process_day(self, day: dt.date) -> bool:
        self.state.log(f"Starting day {day.isoformat()}")
        studies = self.load_or_query_day_studies(day)
        status = self.state.load_day_status(day)
        self.bootstrap_day_status_from_cache(day, studies, status)
        self.state.save_day_status(day, status)
        self.state.write_day_progress_tsv(day, studies, status)

        if not studies:
            self.state.log(f"Remote day {day.isoformat()} returned zero studies.")
            self.state.mark_day_done(day, studies, status, mode="complete-empty-day")
            if not self.args.keep_day_manifests:
                self.state.prune_day_manifests(day)
            self.state.set_next_date(day + dt.timedelta(days=1))
            return True

        stalled_passes = int(status.get("stalled_passes", 0))
        last_pass = int(status.get("last_pass", 0))
        for pass_number in range(last_pass + 1, self.args.max_passes_per_day + 1):
            if STOP_REQUESTED:
                return False
            self.state.log(f"Day {day.isoformat()} pass {pass_number}/{self.args.max_passes_per_day}")
            progress_this_pass = False
            for study in studies:
                if STOP_REQUESTED:
                    break
                changed = self.process_study(day, study, status)
                progress_this_pass = progress_this_pass or changed
                self.state.save_day_status(day, status)
                self.state.write_day_progress_tsv(day, studies, status)

            status["last_pass"] = pass_number
            if self.day_is_complete(studies, status):
                self.final_day_cleanup(day, studies, status)
                if not self.day_is_complete(studies, status):
                    progress_this_pass = True
                    self.state.save_day_status(day, status)
                    self.state.write_day_progress_tsv(day, studies, status)
                else:
                    mode = self.day_completion_mode(studies, status)
                    self.state.log(f"Day {day.isoformat()} completed in mode {mode}")
                    self.state.save_day_status(day, status)
                    self.state.write_day_progress_tsv(day, studies, status)
                    self.state.mark_day_done(day, studies, status, mode=mode)
                    if not self.args.keep_day_manifests:
                        self.state.prune_day_manifests(day)
                    self.state.set_next_date(day + dt.timedelta(days=1))
                    return True

            if progress_this_pass:
                stalled_passes = 0
                self.state.log(f"Day {day.isoformat()} made progress on pass {pass_number}")
            else:
                stalled_passes += 1
                self.state.log(
                    f"Day {day.isoformat()} made no progress on pass {pass_number} "
                    f"(stalled {stalled_passes}/{self.args.max_stalled_passes})"
                )
            status["stalled_passes"] = stalled_passes
            self.state.save_day_status(day, status)
            self.state.write_day_progress_tsv(day, studies, status)

            if stalled_passes >= self.args.max_stalled_passes:
                self.state.error(
                    f"Stopping on {day.isoformat()} after {stalled_passes} stalled passes. "
                    "Inspect progress.tsv and logs/errors.log, then rerun."
                )
                return False

            if self.args.settle_seconds > 0 and not STOP_REQUESTED:
                time.sleep(self.args.settle_seconds)

        self.state.error(
            f"Stopping on {day.isoformat()} after reaching max passes per day ({self.args.max_passes_per_day})."
        )
        return False

    def load_or_query_day_studies(self, day: dt.date) -> list[RemoteStudy]:
        cached = None if self.args.refresh_day_cache else self.state.load_day_cache(day)
        if cached is not None:
            self.state.log(f"Using cached remote study list for {day.isoformat()} ({len(cached)} studies)")
            return cached

        query_id: str | None = None
        try:
            query_fields = {
                "StudyDate": iso_to_dicom_date(day) + "-" + iso_to_dicom_date(day),
                "StudyInstanceUID": "",
                "PatientID": "",
                "PatientName": "",
                "PatientBirthDate": "",
                "StudyDescription": "",
                "AccessionNumber": "",
                "NumberOfStudyRelatedSeries": "",
                "NumberOfStudyRelatedInstances": "",
            }
            query_id = self.client.create_remote_query(self.args.remote_name, "Study", query_fields)
            answer_ids = self.client.get_query_answers(query_id)
            result: list[RemoteStudy] = []
            seen: set[str] = set()
            for answer_id in answer_ids:
                content = self.client.get_query_answer_content(query_id, answer_id)
                study_uid = pick_tag(content, "StudyInstanceUID")
                if not study_uid or study_uid in seen:
                    continue
                seen.add(study_uid)
                result.append(
                    RemoteStudy(
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
                )
            result.sort(key=lambda s: (s.study_date, s.patient_id, s.study_uid))
            self.state.save_day_cache(day, result)
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

        live_uids = {study.study_uid for study in studies}
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

        for study in studies:
            entry = study_states.setdefault(study.study_uid, {})
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
            entry.setdefault("zip_attempts", 0)
            entry.setdefault("delete_attempts", 0)
            entry.setdefault("backup_complete", False)
            entry.setdefault("accounting_complete", False)
            entry["required"] = True
            entry.setdefault("created_at", utc_now_iso())
            entry["updated_at"] = utc_now_iso()

        self.assign_zip_filenames(day, studies, status)

    def assign_zip_filenames(self, day: dt.date, studies: list[RemoteStudy], status: dict[str, Any]) -> None:
        study_states = status.get("studies", {})
        if not isinstance(study_states, dict):
            raise RuntimeError("Invalid studies map in status")
        used_names: set[str] = set()

        # Preserve existing assignments when possible.
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

    def backup_day_dir(self, day: dt.date) -> Path:
        path = self.backup_dir / day.strftime("%Y") / day.strftime("%m") / day.strftime("%d")
        ensure_dir(path, self.owner)
        return path

    def final_zip_path(self, day: dt.date, zip_filename: str) -> Path:
        return self.backup_day_dir(day) / zip_filename

    def zip_part_path(self, final_zip: Path) -> Path:
        return final_zip.with_name(final_zip.name + ".part")

    def process_study(self, day: dt.date, study: RemoteStudy, day_status: dict[str, Any]) -> bool:
        study_states = day_status.setdefault("studies", {})
        if not isinstance(study_states, dict):
            raise RuntimeError("Invalid studies state")
        s = study_states.setdefault(study.study_uid, {})
        if not isinstance(s, dict):
            s = {}
            study_states[study.study_uid] = s

        previous_snapshot = json.dumps(material_study_state(s), sort_keys=True, default=str)
        s["last_checked_at"] = utc_now_iso()
        s.setdefault("rejected_instances", {})
        if not isinstance(s["rejected_instances"], dict):
            s["rejected_instances"] = {}
        s.setdefault("instance_failures", {})
        if not isinstance(s["instance_failures"], dict):
            s["instance_failures"] = {}
        s["last_error"] = ""

        if self.maybe_complete_from_existing_zip(day, study, s):
            current_snapshot = json.dumps(material_study_state(s), sort_keys=True, default=str)
            return previous_snapshot != current_snapshot

        try:
            local = self.client.lookup_local_study(study.study_uid)
            if local is None:
                s["local_study_id"] = None
                s["local_series_count"] = 0
                s["local_instance_count"] = 0
            else:
                local_study_id = safe_text(local.get("ID"))
                s["local_study_id"] = local_study_id
                stats = self.client.get_study_statistics(local_study_id)
                s["local_series_count"] = parse_count(stats.get("CountSeries")) or 0
                s["local_instance_count"] = parse_count(stats.get("CountInstances")) or 0

            manifest_mode, remote_manifest = self.load_or_fetch_remote_manifest(day, study, s)
            s["manifest_mode"] = manifest_mode
            if manifest_mode == "exact" and remote_manifest is not None:
                self.reconcile_exact_study(day, study, s, remote_manifest)
            else:
                self.reconcile_heuristic_study(day, study, s)

            if s.get("accounting_complete") is True:
                self.ensure_zip_and_cleanup(day, study, s)
        except Exception as exc:
            s["status"] = "error"
            s["last_error"] = str(exc)
            self.state.error(f"Study {short_uid(study.study_uid)} failed: {exc}")

        current_snapshot = json.dumps(material_study_state(s), sort_keys=True, default=str)
        return previous_snapshot != current_snapshot

    def maybe_complete_from_existing_zip(self, day: dt.date, study: RemoteStudy, s: dict[str, Any]) -> bool:
        zip_filename = safe_text(s.get("zip_filename"))
        if not zip_filename:
            return False
        final_zip = self.final_zip_path(day, zip_filename)
        part_zip = self.zip_part_path(final_zip)
        if part_zip.exists() and not final_zip.exists():
            part_zip.unlink(missing_ok=True)

        if not final_zip.exists():
            if s.get("backup_complete") is True:
                s["backup_complete"] = False
                s["status"] = "pending"
            return False

        try:
            validate_zip_file(final_zip)
        except Exception as exc:
            s["status"] = "pending"
            s["backup_complete"] = False
            s["last_error"] = f"Existing ZIP invalid: {exc}"
            try:
                final_zip.unlink()
            except Exception:
                pass
            return True

        manifest = read_zip_manifest(final_zip)
        if not self.existing_zip_matches(study, s, manifest):
            s["status"] = "pending"
            s["backup_complete"] = False
            s["last_error"] = "Existing ZIP manifest does not match current study state"
            return False

        s["zip_bytes"] = final_zip.stat().st_size
        s["zip_validated_at"] = utc_now_iso()
        s["zip_manifest_hash"] = safe_text(manifest.get("manifest_sha1", "")) if isinstance(manifest, dict) else ""

        self.delete_local_if_present(day, study, s)
        return True

    def existing_zip_matches(self, study: RemoteStudy, s: dict[str, Any], manifest: dict[str, Any] | None) -> bool:
        if not isinstance(manifest, dict):
            return False
        if safe_text(manifest.get("study_uid")) != study.study_uid:
            return False
        if manifest.get("backup_complete") is not True:
            return False
        current_rejected = len(s.get("rejected_instances", {})) if isinstance(s.get("rejected_instances"), dict) else 0
        if parse_count(manifest.get("rejected_count")) != current_rejected:
            return False
        current_mode = safe_text(s.get("manifest_mode"))
        recorded_mode = safe_text(manifest.get("accounting_mode"))
        if current_mode and recorded_mode and current_mode != "unknown" and recorded_mode not in ("", current_mode):
            return False
        return True

    def load_or_fetch_remote_manifest(
        self,
        day: dt.date,
        study: RemoteStudy,
        study_state: dict[str, Any],
    ) -> tuple[str, dict[str, list[dict[str, str]]] | None]:
        manifest_path = self.manifest_path(day, study.study_uid)
        if manifest_path.exists():
            payload = load_json_file(manifest_path)
            if isinstance(payload, dict) and payload.get("mode") == "exact" and isinstance(payload.get("series"), dict):
                return "exact", payload["series"]
            if isinstance(payload, dict) and payload.get("mode") == "heuristic":
                return "heuristic", None

        try:
            manifest = self.fetch_remote_manifest_exact(study.study_uid)
            atomic_write_json(
                manifest_path,
                {
                    "study_uid": study.study_uid,
                    "mode": "exact",
                    "cached_at": utc_now_iso(),
                    "series": manifest,
                },
                self.owner,
            )
            return "exact", manifest
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
                    self.owner,
                )
                self.state.log(
                    f"Exact manifest unavailable for {short_uid(study.study_uid)}; using heuristic fallback: {exc}"
                )
                return "heuristic", None
            raise

    def manifest_path(self, day: dt.date, study_uid: str) -> Path:
        filename = hashlib.sha1(study_uid.encode("utf-8")).hexdigest() + ".json"
        return self.state.day_manifest_dir(day) / filename

    def rejected_study_dir(self, day: dt.date, study_uid: str) -> Path:
        study_hash = hashlib.sha1(study_uid.encode("utf-8")).hexdigest()
        path = self.state.day_rejected_dir(day) / study_hash
        ensure_dir(path, self.owner)
        return path

    def remove_rejected_archive(self, day: dt.date, study_uid: str) -> None:
        study_hash = hashlib.sha1(study_uid.encode("utf-8")).hexdigest()
        path = self.state.day_rejected_dir(day) / study_hash
        if path.exists():
            shutil.rmtree(path)

    def fetch_remote_manifest_exact(self, study_uid: str) -> dict[str, list[dict[str, str]]]:
        study_query_id: str | None = None
        instance_query_id: str | None = None
        series_query_id: str | None = None
        try:
            study_query_id = self.client.create_remote_query(
                self.args.remote_name,
                "Study",
                {
                    "StudyInstanceUID": study_uid,
                    "PatientID": "",
                    "PatientName": "",
                    "PatientBirthDate": "",
                    "StudyDate": "",
                    "NumberOfStudyRelatedSeries": "",
                    "NumberOfStudyRelatedInstances": "",
                },
            )
            study_answer_id = self.pick_single_remote_study_answer(study_query_id, study_uid)
            instance_query_id = self.client.create_child_query(
                study_query_id,
                study_answer_id,
                "instances",
                {
                    "SeriesInstanceUID": "",
                    "SOPInstanceUID": "",
                    "SOPClassUID": "",
                },
            )
            manifest = self.read_instance_manifest(instance_query_id)
            if manifest is not None:
                return manifest

            series_query_id = self.client.create_child_query(
                study_query_id,
                study_answer_id,
                "series",
                {"SeriesInstanceUID": ""},
            )
            series_manifest: dict[str, list[dict[str, str]]] = {}
            for series_answer_id in self.client.get_query_answers(series_query_id):
                series_content = self.client.get_query_answer_content(series_query_id, series_answer_id)
                series_uid = pick_tag(series_content, "SeriesInstanceUID")
                if not series_uid:
                    raise RuntimeError(f"Series query did not return SeriesInstanceUID for study {study_uid}.")
                child_instance_query_id = self.client.create_child_query(
                    series_query_id,
                    series_answer_id,
                    "instances",
                    {
                        "SOPInstanceUID": "",
                        "SOPClassUID": "",
                    },
                )
                try:
                    instances = []
                    for answer_id in self.client.get_query_answers(child_instance_query_id):
                        content = self.client.get_query_answer_content(child_instance_query_id, answer_id)
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
                    try:
                        self.client.delete_query(child_instance_query_id)
                    except Exception:
                        pass
            return series_manifest
        finally:
            for query_id in (instance_query_id, series_query_id, study_query_id):
                if query_id:
                    try:
                        self.client.delete_query(query_id)
                    except Exception:
                        pass

    def read_instance_manifest(self, query_id: str) -> dict[str, list[dict[str, str]]] | None:
        manifest: dict[str, list[dict[str, str]]] = {}
        missing_series_uid = False
        for answer_id in self.client.get_query_answers(query_id):
            content = self.client.get_query_answer_content(query_id, answer_id)
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

    def pick_single_remote_study_answer(self, query_id: str, study_uid: str) -> str:
        answer_ids = self.client.get_query_answers(query_id)
        for answer_id in answer_ids:
            content = self.client.get_query_answer_content(query_id, answer_id)
            if pick_tag(content, "StudyInstanceUID") == study_uid:
                return answer_id
        raise RuntimeError(f"Remote query for study {study_uid} returned no exact answer.")

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

    def reconcile_exact_study(
        self,
        day: dt.date,
        study: RemoteStudy,
        study_state: dict[str, Any],
        remote_manifest: dict[str, list[dict[str, str]]],
    ) -> None:
        local_study_id = safe_text(study_state.get("local_study_id"))
        if local_study_id:
            _, local_sops = self.local_manifest(local_study_id)
        else:
            local_sops = set()

        rejected_sops = set(study_state.get("rejected_instances", {}).keys()) if isinstance(study_state.get("rejected_instances"), dict) else set()

        remote_records: list[dict[str, str]] = []
        for records in remote_manifest.values():
            remote_records.extend(records)
        remote_records = unique_instance_records(remote_records)
        remote_sops = {item["sop_uid"] for item in remote_records if item.get("sop_uid")}
        missing_records = [
            item for item in remote_records
            if item.get("sop_uid") and item["sop_uid"] not in local_sops and item["sop_uid"] not in rejected_sops
        ]

        study_state["remote_exact_instance_count"] = len(remote_sops)
        study_state["accounted_count"] = len(remote_sops) - len(missing_records)
        study_state["missing_count"] = len(missing_records)
        study_state["local_instance_count"] = len(local_sops)
        study_state["accounting_complete"] = False

        if not missing_records:
            study_state["status"] = "accounted-exact"
            study_state["accounting_complete"] = True
            study_state["last_error"] = ""
            return

        plan = RetrievalPlan(
            mode="study" if (not local_study_id or len(missing_records) >= self.args.whole_study_threshold) else "instances",
            missing=missing_records,
        )
        outcome = self.retrieve_and_import(day, study, study_state, plan)

        local = self.client.lookup_local_study(study.study_uid)
        if local is None:
            local_sops_after: set[str] = set()
            study_state["local_study_id"] = None
            study_state["local_series_count"] = 0
            study_state["local_instance_count"] = 0
        else:
            local_study_id = safe_text(local.get("ID"))
            study_state["local_study_id"] = local_study_id
            stats = self.client.get_study_statistics(local_study_id)
            study_state["local_series_count"] = parse_count(stats.get("CountSeries")) or 0
            _, local_sops_after = self.local_manifest(local_study_id)
            study_state["local_instance_count"] = len(local_sops_after)

        rejected_sops_after = set(study_state.get("rejected_instances", {}).keys()) if isinstance(study_state.get("rejected_instances"), dict) else set()
        remaining = [
            item for item in remote_records
            if item["sop_uid"] not in local_sops_after and item["sop_uid"] not in rejected_sops_after
        ]
        study_state["accounted_count"] = len(remote_sops) - len(remaining)
        study_state["missing_count"] = len(remaining)

        if not remaining:
            study_state["status"] = "accounted-exact"
            study_state["accounting_complete"] = True
            study_state["last_error"] = ""
        else:
            study_state["status"] = "pending"
            study_state["accounting_complete"] = False
            if outcome.notes:
                study_state["last_error"] = "; ".join(outcome.notes)

    def reconcile_heuristic_study(self, day: dt.date, study: RemoteStudy, study_state: dict[str, Any]) -> None:
        local_study_id = safe_text(study_state.get("local_study_id"))
        if local_study_id:
            stats = self.client.get_study_statistics(local_study_id)
            local_instances = parse_count(stats.get("CountInstances")) or 0
            local_series = parse_count(stats.get("CountSeries")) or 0
            study_state["local_instance_count"] = local_instances
            study_state["local_series_count"] = local_series
        else:
            local_instances = 0
            local_series = 0
            study_state["local_instance_count"] = 0
            study_state["local_series_count"] = 0

        rejected_instances = study_state.get("rejected_instances", {}) if isinstance(study_state.get("rejected_instances"), dict) else {}
        rejected_count = len(rejected_instances)
        if study.remote_instance_count is None or study.remote_series_count is None:
            raise RuntimeError(
                "Exact manifest is unavailable and the remote PACS did not return "
                "NumberOfStudyRelatedInstances/NumberOfStudyRelatedSeries for heuristic verification."
            )
        remote_total = study.remote_instance_count
        remote_series = study.remote_series_count
        exact_target = remote_total
        heuristic_target = max(0, remote_total - self.args.allowance_per_series * remote_series)
        accounted = local_instances + rejected_count
        missing_exact = max(0, exact_target - accounted)
        study_state["accounted_count"] = accounted
        study_state["missing_count"] = missing_exact
        study_state["heuristic_target"] = heuristic_target
        study_state["accounting_complete"] = False

        if remote_total > 0 and accounted >= exact_target:
            study_state["status"] = "accounted-heuristic-exactcount"
            study_state["accounting_complete"] = True
            study_state["last_error"] = ""
            return
        if remote_total > 0 and accounted >= heuristic_target:
            study_state["status"] = "accounted-heuristic"
            study_state["accounting_complete"] = True
            study_state["last_error"] = self.exact_manifest_failures.get(study.study_uid, "")
            return

        plan = RetrievalPlan(mode="study", missing=[])
        outcome = self.retrieve_and_import(day, study, study_state, plan)

        local = self.client.lookup_local_study(study.study_uid)
        if local is None:
            local_instances = 0
            local_series = 0
            study_state["local_study_id"] = None
        else:
            study_state["local_study_id"] = safe_text(local.get("ID"))
            stats = self.client.get_study_statistics(study_state["local_study_id"])
            local_instances = parse_count(stats.get("CountInstances")) or 0
            local_series = parse_count(stats.get("CountSeries")) or 0
        study_state["local_instance_count"] = local_instances
        study_state["local_series_count"] = local_series
        rejected_count = len(study_state.get("rejected_instances", {})) if isinstance(study_state.get("rejected_instances"), dict) else 0
        accounted = local_instances + rejected_count
        missing_exact = max(0, exact_target - accounted)
        study_state["accounted_count"] = accounted
        study_state["missing_count"] = missing_exact

        if remote_total > 0 and accounted >= exact_target:
            study_state["status"] = "accounted-heuristic-exactcount"
            study_state["accounting_complete"] = True
            study_state["last_error"] = ""
        elif remote_total > 0 and accounted >= heuristic_target:
            study_state["status"] = "accounted-heuristic"
            study_state["accounting_complete"] = True
            study_state["last_error"] = self.exact_manifest_failures.get(study.study_uid, "")
        else:
            study_state["status"] = "pending"
            study_state["accounting_complete"] = False
            if outcome.notes:
                study_state["last_error"] = "; ".join(outcome.notes)

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
        overall_started = time.monotonic()
        with tempfile.TemporaryDirectory(prefix="orthanc-backup-") as temp_dir_str:
            temp_dir = Path(temp_dir_str)
            retrieve_started = time.monotonic()
            if plan.mode == "study":
                self.state.log(
                    f"Retrieving whole study {study_label} "
                    f"(attempt {study_state['retrieve_attempts']})"
                )
                self.run_getscu_study(study.study_uid, temp_dir)
            else:
                self.state.log(
                    f"Retrieving {len(plan.missing)} missing instance(s) from study {study_label}"
                )
                instance_errors = self.run_getscu_missing_instances(study.study_uid, plan.missing, temp_dir)
                outcome.notes.extend(instance_errors)
                files_after_instances = sorted(path for path in temp_dir.rglob("*") if path.is_file())
                if not files_after_instances:
                    self.state.log(
                        f"No files were received for missing-instance retrieve of {study_label}; "
                        "falling back to whole-study retrieve once"
                    )
                    self.run_getscu_study(study.study_uid, temp_dir)

            files = sorted(path for path in temp_dir.rglob("*") if path.is_file())
            retrieve_elapsed = time.monotonic() - retrieve_started
            outcome.retrieved_files = len(files)
            retrieved_bytes = sum(path.stat().st_size for path in files)
            self.state.log(
                f"Retrieve finished for study {study_label}: "
                f"{outcome.retrieved_files} file(s), {format_size(retrieved_bytes)} "
                f"in {format_duration(retrieve_elapsed)}"
            )
            if not files:
                outcome.notes.append("retrieve returned zero files")
                return outcome

            import_started = time.monotonic()
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
                        outcome.notes.append(f"import failed and SOPInstanceUID could not be extracted from {dicom_file.name}: {exc}")
                        continue
                    failure_count = int(study_state.setdefault("instance_failures", {}).get(sop_uid, 0)) + 1
                    study_state["instance_failures"][sop_uid] = failure_count
                    archive_info = self.archive_rejected_instance(day, study.study_uid, ids, dicom_file, str(exc), failure_count)
                    if failure_count >= self.args.reject_after_failures:
                        rejected_map = study_state.setdefault("rejected_instances", {})
                        if sop_uid not in rejected_map:
                            rejected_map[sop_uid] = archive_info
                            outcome.rejected_accounted += 1
                            outcome.progress_made = True
                    outcome.notes.append(f"{short_uid(sop_uid)} rejected by Orthanc: {exc}")
            import_elapsed = time.monotonic() - import_started
            self.state.log(
                f"Import finished for study {study_label}: "
                f"new={outcome.imported_successfully}, already-stored={outcome.duplicates_or_existing}, "
                f"rejected-accounted={outcome.rejected_accounted}, notes={len(outcome.notes)} "
                f"in {format_duration(import_elapsed)} "
                f"(total retrieve+import {format_duration(time.monotonic() - overall_started)})"
            )
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

    def run_getscu_missing_instances(self, study_uid: str, missing_records: list[dict[str, str]], output_dir: Path) -> list[str]:
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
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"Command timed out after {timeout}s: {' '.join(command)}") from exc
        if result.returncode != 0:
            details = "\n".join(part for part in (result.stdout.strip(), result.stderr.strip()) if part)
            raise RuntimeError(
                f"Command failed with exit code {result.returncode}: {' '.join(command)}\n{details or 'no output'}"
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
        target_dir = self.rejected_study_dir(day, study_uid)
        sop_uid = ids.get("sop_uid", "unknown-sop")
        sop_hash = hashlib.sha1(sop_uid.encode("utf-8")).hexdigest()
        target_dcm = target_dir / f"{sop_hash}.dcm"
        target_meta = target_dir / f"{sop_hash}.json"
        shutil.copy2(source_path, target_dcm)
        maybe_chown(target_dcm, self.owner)
        metadata = {
            "archived_at": utc_now_iso(),
            "study_uid": study_uid,
            "series_uid": ids.get("series_uid", ""),
            "sop_uid": sop_uid,
            "sop_class_uid": ids.get("sop_class_uid", ""),
            "source_file": source_path.name,
            "stored_file": str(target_dcm.relative_to(self.state.root)),
            "file_token": sop_hash,
            "failure_count": failure_count,
            "last_error": error_text,
        }
        atomic_write_json(target_meta, metadata, self.owner)
        return metadata

    def ensure_zip_and_cleanup(self, day: dt.date, study: RemoteStudy, study_state: dict[str, Any]) -> None:
        zip_filename = safe_text(study_state.get("zip_filename"))
        if not zip_filename:
            raise RuntimeError("ZIP filename has not been assigned")
        final_zip = self.final_zip_path(day, zip_filename)
        part_zip = self.zip_part_path(final_zip)
        part_zip.unlink(missing_ok=True)
        study_label = short_uid(study.study_uid)

        if final_zip.exists():
            manifest = read_zip_manifest(final_zip)
            if self.existing_zip_matches(study, study_state, manifest):
                study_state["zip_bytes"] = final_zip.stat().st_size
                study_state["zip_validated_at"] = utc_now_iso()
                self.delete_local_if_present(day, study, study_state)
                return
            final_zip.unlink(missing_ok=True)

        study_state["zip_attempts"] = int(study_state.get("zip_attempts", 0)) + 1
        local_study_id = safe_text(study_state.get("local_study_id"))
        rejected_entries = self.rejected_entries_for_study(study_state)
        zip_started = time.monotonic()
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
            validate_started = time.monotonic()
            validate_zip_file(part_zip)
            self.state.log(
                f"Validated raw archive ZIP for study {study_label} "
                f"in {format_duration(time.monotonic() - validate_started)}"
            )
            append_started = time.monotonic()
            self.append_manifest_and_rejected_to_zip(part_zip, day, study, study_state, rejected_entries)
            self.state.log(
                f"Appended manifest/rejected payload for study {study_label} "
                f"in {format_duration(time.monotonic() - append_started)}"
            )
        else:
            self.build_combined_zip(part_zip, day, study, study_state, local_study_id, rejected_entries)

        final_validate_started = time.monotonic()
        validate_zip_file(part_zip)
        self.state.log(
            f"Validated final ZIP payload for study {study_label} "
            f"in {format_duration(time.monotonic() - final_validate_started)}"
        )
        os.replace(part_zip, final_zip)
        maybe_chown(final_zip, self.owner)
        manifest = read_zip_manifest(final_zip)
        if not self.existing_zip_matches(study, study_state, manifest):
            raise RuntimeError("ZIP was created but its manifest did not validate against the current study state")
        study_state["zip_bytes"] = final_zip.stat().st_size
        study_state["zip_validated_at"] = utc_now_iso()
        study_state["zip_manifest_hash"] = safe_text(manifest.get("manifest_sha1", "")) if manifest else ""
        self.state.log(
            f"Created ZIP for study {study_label} -> {final_zip} "
            f"({format_size(study_state['zip_bytes'])}, zip phase {format_duration(time.monotonic() - zip_started)})"
        )
        self.delete_local_if_present(day, study, study_state)

    def delete_local_if_present(self, day: dt.date, study: RemoteStudy, study_state: dict[str, Any]) -> None:
        local = self.client.lookup_local_study(study.study_uid)
        if local is None:
            study_state["local_study_id"] = None
            study_state["local_series_count"] = 0
            study_state["local_instance_count"] = 0
            study_state["local_deleted_at"] = study_state.get("local_deleted_at") or utc_now_iso()
            study_state["backup_complete"] = True
            study_state["status"] = "complete"
            study_state["accounting_complete"] = True
            study_state["last_error"] = ""
            self.remove_rejected_archive(day, study.study_uid)
            return

        local_id = safe_text(local.get("ID"))
        try:
            delete_started = time.monotonic()
            study_state["delete_attempts"] = int(study_state.get("delete_attempts", 0)) + 1
            self.client.delete_study(local_id)
            if self.args.settle_seconds > 0:
                time.sleep(self.args.settle_seconds)
            local_after = self.client.lookup_local_study(study.study_uid)
            if local_after is not None:
                raise RuntimeError("study is still present after DELETE")
            study_state["local_study_id"] = None
            study_state["local_series_count"] = 0
            study_state["local_instance_count"] = 0
            study_state["local_deleted_at"] = utc_now_iso()
            study_state["backup_complete"] = True
            study_state["status"] = "complete"
            study_state["accounting_complete"] = True
            study_state["last_error"] = ""
            self.remove_rejected_archive(day, study.study_uid)
            stats = self.state.meta.setdefault("stats", {})
            if isinstance(stats, dict):
                stats["local_studies_deleted"] = int(stats.get("local_studies_deleted", 0)) + 1
            self.state.save_meta()
            self.state.log(
                f"Deleted local staging copy of study {short_uid(study.study_uid)} "
                f"in {format_duration(time.monotonic() - delete_started)}"
            )
        except Exception as exc:
            study_state["backup_complete"] = False
            study_state["status"] = "zipped"
            study_state["last_error"] = f"ZIP complete but local study deletion failed: {exc}"

    def rejected_entries_for_study(self, study_state: dict[str, Any]) -> list[dict[str, Any]]:
        raw = study_state.get("rejected_instances", {})
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
        day: dt.date,
        study: RemoteStudy,
        study_state: dict[str, Any],
        rejected_entries: list[dict[str, Any]],
    ) -> dict[str, Any]:
        manifest = {
            "created_at": utc_now_iso(),
            "script": "orthanc-backfill-export-by-date.py",
            "date": day.isoformat(),
            "study_uid": study.study_uid,
            "patient_id": study.patient_id,
            "patient_name": study.patient_name,
            "patient_birth_date": study.patient_birth_date,
            "study_date": study.study_date,
            "study_description": study.description,
            "accession_number": study.accession_number,
            "accounting_mode": safe_text(study_state.get("manifest_mode")),
            "backup_complete": True,
            "remote_series_count": study.remote_series_count,
            "remote_instance_count": study.remote_instance_count,
            "remote_exact_instance_count": parse_count(study_state.get("remote_exact_instance_count")),
            "local_instance_count_at_export": parse_count(study_state.get("local_instance_count")),
            "accounted_count": parse_count(study_state.get("accounted_count")),
            "missing_count": parse_count(study_state.get("missing_count")),
            "heuristic_target": parse_count(study_state.get("heuristic_target")),
            "zip_mode": self.args.zip_mode,
            "zip_name_mode": self.args.name,
            "zip_filename": safe_text(study_state.get("zip_filename")),
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
        day: dt.date,
        study: RemoteStudy,
        study_state: dict[str, Any],
        rejected_entries: list[dict[str, Any]],
    ) -> None:
        manifest = self.build_backup_manifest(day, study, study_state, rejected_entries)
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
        day: dt.date,
        study: RemoteStudy,
        study_state: dict[str, Any],
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
                series_labels: dict[str, str] = {}
                for index, series in enumerate(series_items, start=1):
                    series_id = safe_text(series.get("ID")).strip()
                    if not series_id:
                        continue
                    description = (
                        pick_tag(series, "SeriesDescription")
                        or pick_tag(series, "SeriesInstanceUID")
                        or f"SERIES_{index:03d}"
                    )
                    modality = pick_tag(series, "Modality") or "SERIES"
                    series_labels[series_id] = ascii_slug(f"{modality}_{description}", fallback=f"SERIES_{index:03d}")

                for ordinal, instance in enumerate(instances, start=1):
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

            manifest = self.build_backup_manifest(day, study, study_state, rejected_entries)
            archive.writestr(
                ZIP_MANIFEST_NAME,
                json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
                compress_type=zipfile.ZIP_STORED,
            )

        maybe_chown(output_path, self.owner)
        if output_path.exists() and output_path.stat().st_size <= 0 and bytes_written <= 0 and not rejected_entries:
            raise RuntimeError("Created ZIP is empty")

    def day_is_complete(self, studies: list[RemoteStudy], status: dict[str, Any]) -> bool:
        study_states = status.get("studies", {}) if isinstance(status.get("studies"), dict) else {}
        for study in studies:
            state = study_states.get(study.study_uid, {})
            if not isinstance(state, dict):
                return False
            if state.get("backup_complete") is not True:
                return False
        return True

    def day_completion_mode(self, studies: list[RemoteStudy], status: dict[str, Any]) -> str:
        study_states = status.get("studies", {}) if isinstance(status.get("studies"), dict) else {}
        used_heuristic = False
        for study in studies:
            state = study_states.get(study.study_uid, {})
            if isinstance(state, dict):
                value = safe_text(state.get("status"))
                if "heuristic" in value or safe_text(state.get("manifest_mode")) == "heuristic":
                    used_heuristic = True
                    break
        return "complete-with-heuristic" if used_heuristic else "complete-exact"

    def final_day_cleanup(self, day: dt.date, studies: list[RemoteStudy], status: dict[str, Any]) -> None:
        study_states = status.get("studies", {}) if isinstance(status.get("studies"), dict) else {}
        for study in studies:
            state = study_states.get(study.study_uid, {})
            if not isinstance(state, dict):
                continue
            if state.get("backup_complete") is not True:
                continue
            self.maybe_complete_from_existing_zip(day, study, state)
        status["updated_at"] = utc_now_iso()


def parse_args() -> argparse.Namespace:
    home = preferred_home()
    default_backup_dir = home / "backup"
    default_state_dir = default_backup_dir / ".orthanc-remote-zip-backup-state"

    parser = argparse.ArgumentParser(
        description="Resumable day-by-day remote PACS backup into ZIP files, using Orthanc only as a staging store.",
    )
    parser.add_argument("--start-date", required=True, type=parse_iso_date, help="First date to process (YYYY-MM-DD).")
    parser.add_argument(
        "--end-date",
        type=parse_iso_date,
        default=None,
        help="Last date to process (YYYY-MM-DD). Defaults to yesterday in the local timezone.",
    )
    parser.add_argument("--remote-aet", required=True, help="Remote PACS AE Title.")
    parser.add_argument("--remote-host", required=True, help="Remote PACS host/IP.")
    parser.add_argument("--remote-port", required=True, type=int, help="Remote PACS TCP port.")
    parser.add_argument(
        "--remote-name",
        default="BACKUP-REMOTE",
        help="Temporary Orthanc modality name to use for the remote PACS. Default: BACKUP-REMOTE",
    )

    parser.add_argument("--base-url", help="Orthanc REST base URL. Default: read from /etc/orthanc/orthanc.json")
    parser.add_argument("--user", help="Orthanc REST username. Default: read from credentials.json")
    parser.add_argument("--password", help="Orthanc REST password. Default: read from credentials.json")
    parser.add_argument("--config-dir", default="/etc/orthanc", help="Orthanc config dir. Default: /etc/orthanc")
    parser.add_argument("--orthanc-config", help="Explicit path to orthanc.json")
    parser.add_argument("--credentials-config", help="Explicit path to credentials.json")
    parser.add_argument(
        "--calling-aet",
        help="Local calling AE Title for getscu. Default: DicomAet from local Orthanc config.",
    )

    parser.add_argument(
        "--backup-dir",
        type=lambda s: Path(s).expanduser().resolve(),
        default=default_backup_dir,
        help=f"Root directory for final ZIP files. Default: {default_backup_dir}",
    )
    parser.add_argument(
        "--state-dir",
        type=lambda s: Path(s).expanduser().resolve(),
        default=default_state_dir,
        help=f"Directory for resumable state, logs, manifests, and temporary rejected raw DICOM files. Default: {default_state_dir}",
    )
    parser.add_argument(
        "--name",
        choices=("uid", "patientName"),
        default="uid",
        help=(
            "ZIP naming mode: 'uid' -> StudyInstanceUID.zip, "
            "'patientName' -> PatientName_BirthDate_StudyDate[_N].zip"
        ),
    )
    parser.add_argument(
        "--zip-mode",
        choices=("archive", "stored"),
        default="archive",
        help=(
            "ZIP creation mode. 'archive' downloads /studies/{id}/archive then appends manifest/rejected files. "
            "'stored' builds the ZIP locally by streaming each instance with ZIP_STORED. Default: archive"
        ),
    )

    parser.add_argument(
        "--whole-study-threshold",
        type=int,
        default=32,
        help="If a study is missing at least this many instances, retrieve the whole study instead of per-instance. Default: 32",
    )
    parser.add_argument(
        "--reject-after-failures",
        type=int,
        default=2,
        help="How many repeated Orthanc import failures are required before an instance is counted as rejected-but-backed-up on disk. Default: 2",
    )
    parser.add_argument(
        "--allow-heuristic-fallback",
        action="store_true",
        default=True,
        help="Allow fallback to count-based completion if exact IMAGE-level manifests are unavailable. Default: enabled",
    )
    parser.add_argument(
        "--no-heuristic-fallback",
        action="store_false",
        dest="allow_heuristic_fallback",
        help="Disable heuristic fallback; fail instead if exact manifests are unavailable.",
    )
    parser.add_argument(
        "--allowance-per-series",
        type=int,
        default=2,
        help="Heuristic fallback allowance: remote_instances - allowance_per_series * remote_series. Default: 2",
    )
    parser.add_argument(
        "--max-passes-per-day",
        type=int,
        default=20,
        help="Maximum verification/retrieve/export passes per day before stopping. Default: 20",
    )
    parser.add_argument(
        "--max-stalled-passes",
        type=int,
        default=3,
        help="Stop after this many passes with no progress. Default: 3",
    )
    parser.add_argument(
        "--settle-seconds",
        type=float,
        default=2.0,
        help="Sleep between state-changing operations and day passes. Default: 2 seconds",
    )
    parser.add_argument(
        "--getscu-timeout-seconds",
        type=int,
        default=1800,
        help="Timeout for each getscu call. Default: 1800 seconds",
    )
    parser.add_argument(
        "--echo-timeout",
        type=int,
        default=10,
        help="Timeout for Orthanc C-ECHO to the remote modality. Default: 10 seconds",
    )
    parser.add_argument(
        "--refresh-day-cache",
        action="store_true",
        help="Ignore cached remote study lists and query the current day again.",
    )
    parser.add_argument(
        "--recheck-completed-days",
        action="store_true",
        help="Ignore DONE markers and re-run completed days.",
    )
    parser.add_argument(
        "--keep-day-manifests",
        action="store_true",
        help="Keep cached remote manifests even after a day is complete.",
    )

    args = parser.parse_args()
    if args.end_date is None:
        args.end_date = compute_default_end_date()
    if args.start_date > args.end_date:
        cli_error(f"start-date {args.start_date.isoformat()} is after end-date {args.end_date.isoformat()}.")
    if args.remote_port < 1 or args.remote_port > 65535:
        cli_error("--remote-port must be between 1 and 65535.")
    if args.reject_after_failures < 1:
        cli_error("--reject-after-failures must be >= 1.")
    if args.allowance_per_series < 0:
        cli_error("--allowance-per-series must be >= 0.")
    if args.max_passes_per_day < 1 or args.max_stalled_passes < 1:
        cli_error("--max-passes-per-day and --max-stalled-passes must be >= 1.")
    return args


def main() -> int:
    args = parse_args()
    for cmd in ("getscu", "dcmdump"):
        if shutil.which(cmd) is None:
            cli_error(f"Required command not found: {cmd}")

    owner = default_owner()
    ensure_dir(args.backup_dir, owner)
    ensure_dir(args.state_dir, owner)

    settings = load_orthanc_settings(args)
    client = OrthancClient(settings, timeout=60.0)
    state = StateManager(
        root=args.state_dir,
        owner=owner,
        start_date=args.start_date,
        end_date=args.end_date,
        remote_name=args.remote_name,
        remote_aet=args.remote_aet,
        remote_host=args.remote_host,
        remote_port=args.remote_port,
        orthanc_base_url=settings.base_url,
        orthanc_user=settings.username,
        calling_aet=args.calling_aet,
        backup_dir=args.backup_dir,
        name_mode=args.name,
        zip_mode=args.zip_mode,
    )
    app = BackupZipApp(args, client, state, owner)
    return app.run()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except OrthancApiError as exc:
        print(f"Orthanc API error: {exc}", file=sys.stderr)
        raise SystemExit(1)
    except KeyboardInterrupt:
        raise SystemExit(130)
