from __future__ import annotations

import datetime as dt
import shutil
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable, Mapping

from orthanc_tools.dicom import nullable_int, parse_count, parse_iso_date, safe_text, sanitize_tsv
from orthanc_tools.state import (
    Ownership,
    append_text,
    atomic_write_json,
    atomic_write_text,
    ensure_dir,
    load_json,
    load_json_file,
    local_now_human,
    now_iso,
    utc_now_iso,
)
from orthanc_tools.workflows.primitives import cli_error
from orthanc_tools.workflows.retrieval import RemoteStudy


PathParts = tuple[str, ...]
ExpectedPairs = Mapping[PathParts, Any]
MissingMessageBuilder = Callable[[PathParts], str]
MismatchMessageBuilder = Callable[[PathParts, Any, Any], str]


SUMMARY_HEADER = (
    "date\tstatus\tremote_studies\tcomplete_studies\tpending_studies\t"
    "rejected_instances\tupdated_at\n"
)
BACKFILL_PROGRESS_HEADER = (
    "study_uid\tstudy_date\tremote_series\tremote_instances\tstatus\tmanifest_mode\tlocal_instances\trejected_instances\t"
    "accounted\tmissing\tlast_error\tlast_checked_at"
)
BACKUP_PROGRESS_HEADER = (
    "study_uid\tstudy_date\tremote_series\tremote_instances\tmanifest_mode\taccounted\tmissing\tlocal_instances\t"
    "rejected_instances\tzip_filename\tzip_bytes\tbackup_complete\tstatus\tlast_error\tlast_checked_at"
)


def validate_expected_pairs(
    payload: dict[str, Any],
    expected_pairs: ExpectedPairs,
    *,
    root: Path | str,
    missing_message: MissingMessageBuilder | None = None,
    mismatch_message: MismatchMessageBuilder | None = None,
) -> None:
    root_text = str(root)

    def default_missing(path_parts: PathParts) -> str:
        return (
            f"State directory {root_text} is missing {'.'.join(path_parts)}. "
            "Use a clean --state-dir or fix the state file."
        )

    def default_mismatch(path_parts: PathParts, current: Any, expected: Any) -> str:
        return (
            f"State directory {root_text} belongs to another run. "
            f"Field {'.'.join(path_parts)}={current!r}, expected {expected!r}."
        )

    on_missing = missing_message or default_missing
    on_mismatch = mismatch_message or default_mismatch

    for path_parts, expected_value in expected_pairs.items():
        current: Any = payload
        for key in path_parts:
            if not isinstance(current, dict) or key not in current:
                cli_error(on_missing(path_parts))
            current = current[key]
        if current != expected_value:
            cli_error(on_mismatch(path_parts, current, expected_value))


class StateManager:
    def __init__(
        self,
        root: Path,
        start_date: dt.date,
        end_date: dt.date,
        remote_name: str,
        remote_aet: str,
        remote_host: str,
        remote_port: int,
        orthanc_base_url: str,
        orthanc_user: str,
        calling_aet: str,
        owner: Ownership | None = None,
        backup_dir: Path | None = None,
        name_mode: str | None = None,
        zip_mode: str | None = None,
    ):
        backup_args = (backup_dir, name_mode, zip_mode)
        if any(value is not None for value in backup_args) and not all(value is not None for value in backup_args):
            raise ValueError("backup_dir, name_mode, and zip_mode must be provided together")

        self.root = Path(root)
        self.owner = owner
        self.start_date = start_date
        self.end_date = end_date
        self.remote_name = remote_name
        self.remote_aet = remote_aet
        self.remote_host = remote_host
        self.remote_port = int(remote_port)
        self.orthanc_base_url = orthanc_base_url
        self.orthanc_user = orthanc_user
        self.calling_aet = calling_aet
        self.backup_dir = backup_dir
        self.name_mode = name_mode
        self.zip_mode = zip_mode
        self.is_backup = backup_dir is not None

        self.logs_dir = self.root / "logs"
        self.days_dir = self.root / "days"
        self.meta_path = self.root / "state.json"
        self.current_date_path = self.root / "current-date.txt"
        self.summary_path = self.root / "summary.tsv"

        ensure_dir(self.root, self.owner)
        ensure_dir(self.logs_dir, self.owner)
        ensure_dir(self.days_dir, self.owner)
        self.meta = self._load_or_init_meta()

    def _load_or_init_meta(self) -> dict[str, Any]:
        now = utc_now_iso()
        template: dict[str, Any] = {
            "created_at": now,
            "updated_at": now,
            "start_date": self.start_date.isoformat(),
            "end_date": self.end_date.isoformat(),
            "next_date": self.start_date.isoformat(),
            "remote": {
                "name": self.remote_name,
                "aet": self.remote_aet,
                "host": self.remote_host,
                "port": self.remote_port,
            },
            "orthanc": {
                "base_url": self.orthanc_base_url,
                "user": self.orthanc_user,
                "calling_aet": self.calling_aet,
            },
            "stats": self._initial_stats(),
        }
        if self.is_backup:
            template["backup"] = {
                "dir": str(self.backup_dir),
                "name_mode": self.name_mode,
                "zip_mode": self.zip_mode,
            }

        if not self.meta_path.exists():
            atomic_write_json(self.meta_path, template, self.owner)
            atomic_write_text(self.current_date_path, self.start_date.isoformat() + "\n", self.owner)
            if not self.summary_path.exists():
                atomic_write_text(self.summary_path, SUMMARY_HEADER, self.owner)
            return template

        meta = load_json_file(self.meta_path)
        if not isinstance(meta, dict):
            cli_error(f"Invalid state file: {self.meta_path}")

        validate_expected_pairs(meta, self._expected_pairs(), root=self.root)
        meta["end_date"] = self.end_date.isoformat()
        meta["updated_at"] = now
        atomic_write_json(self.meta_path, meta, self.owner)
        if not self.summary_path.exists():
            atomic_write_text(self.summary_path, SUMMARY_HEADER, self.owner)
        atomic_write_text(
            self.current_date_path,
            str(meta.get("next_date", self.start_date.isoformat())) + "\n",
            self.owner,
        )
        return meta

    def _initial_stats(self) -> dict[str, int]:
        stats = {
            "days_done": 0,
            "studies_complete": 0,
            "instances_imported": 0,
            "instances_rejected_archived": 0,
        }
        if self.is_backup:
            stats["zips_complete"] = 0
            stats["local_studies_deleted"] = 0
        return stats

    def _expected_pairs(self) -> dict[PathParts, Any]:
        pairs: dict[PathParts, Any] = {
            ("start_date",): self.start_date.isoformat(),
            ("remote", "aet"): self.remote_aet,
            ("remote", "host"): self.remote_host,
            ("remote", "port"): self.remote_port,
            ("orthanc", "base_url"): self.orthanc_base_url,
            ("orthanc", "calling_aet"): self.calling_aet,
        }
        if self.is_backup:
            pairs[("backup", "dir")] = str(self.backup_dir)
            pairs[("backup", "name_mode")] = self.name_mode
            pairs[("backup", "zip_mode")] = self.zip_mode
        return pairs

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
        study_states = status.get("studies", {})
        if self.is_backup:
            lines = [BACKUP_PROGRESS_HEADER]
        else:
            lines = [BACKFILL_PROGRESS_HEADER]
        for study in studies:
            s = study_states.get(study.study_uid, {}) if isinstance(study_states, dict) else {}
            rejected_count = len(s.get("rejected_instances", {})) if isinstance(s.get("rejected_instances"), dict) else 0
            if self.is_backup:
                fields = [
                    sanitize_tsv(study.study_uid),
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
            else:
                fields = [
                    sanitize_tsv(study.study_uid),
                    sanitize_tsv(study.study_date),
                    str(nullable_int(study.remote_series_count)),
                    str(nullable_int(study.remote_instance_count)),
                    sanitize_tsv(safe_text(s.get("status", "pending"))),
                    sanitize_tsv(safe_text(s.get("manifest_mode", "unknown"))),
                    str(nullable_int(parse_count(s.get("local_instance_count")))),
                    str(rejected_count),
                    str(nullable_int(parse_count(s.get("accounted_count")))),
                    str(nullable_int(parse_count(s.get("missing_count")))),
                    sanitize_tsv(safe_text(s.get("last_error", ""))),
                    sanitize_tsv(safe_text(s.get("last_checked_at", ""))),
                ]
            lines.append("\t".join(fields))
        atomic_write_text(self.day_progress_tsv_path(day), "\n".join(lines) + "\n", self.owner)

    def mark_day_done(self, day: dt.date, studies: list[RemoteStudy], status: dict[str, Any], mode: str) -> None:
        complete_count = 0
        rejected_total = 0
        for entry in status.get("studies", {}).values():
            if not isinstance(entry, dict):
                continue
            if self.is_backup:
                if entry.get("backup_complete") is True:
                    complete_count += 1
            else:
                state = entry.get("status")
                if str(state).startswith("complete") or state == "heuristic-complete":
                    complete_count += 1
            if isinstance(entry.get("rejected_instances"), dict):
                rejected_total += len(entry.get("rejected_instances", {}))

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
        if self.is_backup:
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
        if self.is_backup:
            append_text(self.logs_dir / "run.log", line, self.owner)
        print(line, end="", file=sys.stderr)


class ExportRunState:
    def __init__(
        self,
        state_dir: Path,
        owner: Ownership | None,
        *,
        backup_dir: Path,
        start_date: dt.date,
        end_date: dt.date,
        name_mode: str,
        zip_mode: str,
        orthanc_base_url: str,
    ):
        self.state_dir = state_dir
        self.owner = owner
        self.backup_dir = backup_dir
        self.start_date = start_date
        self.end_date = end_date
        self.name_mode = name_mode
        self.zip_mode = zip_mode
        self.orthanc_base_url = orthanc_base_url
        self.state_path = state_dir / "state.json"
        self.current_date_path = state_dir / "current-date.txt"
        ensure_dir(self.state_dir, owner)
        self.state = self._load_or_initialize()

    def _base_metadata(self) -> dict[str, Any]:
        return {
            "script": "orthanc-export-local-by-date.py",
            "state_version": 1,
            "backup_dir": str(self.backup_dir),
            "start_date": self.start_date.isoformat(),
            "end_date": self.end_date.isoformat(),
            "name_mode": self.name_mode,
            "zip_mode": self.zip_mode,
            "orthanc_base_url": self.orthanc_base_url,
        }

    def _load_or_initialize(self) -> dict[str, Any]:
        existing = load_json(self.state_path)
        if existing is None:
            state = {
                **self._base_metadata(),
                "created_at": now_iso(),
                "updated_at": now_iso(),
                "status": "running",
                "current_date": self.start_date.isoformat(),
                "last_completed_day": None,
            }
            atomic_write_json(self.state_path, state, self.owner)
            atomic_write_text(self.current_date_path, self.start_date.isoformat() + "\n", self.owner)
            return state

        if not isinstance(existing, dict):
            cli_error(f"Invalid export state file: {self.state_path}")

        validate_expected_pairs(
            existing,
            {(key,): value for key, value in self._base_metadata().items()},
            root=self.state_dir,
            missing_message=lambda path_parts: (
                f"Existing state in {self.state_dir} is missing {path_parts[0]!r}. "
                "Use another --state-dir."
            ),
            mismatch_message=lambda path_parts, actual, expected: (
                f"Existing state in {self.state_dir} was created for a different run parameter: "
                f"{path_parts[0]}={actual!r}, expected {expected!r}. Use another --state-dir."
            ),
        )

        if not self.current_date_path.exists():
            current = existing.get("current_date", self.start_date.isoformat())
            atomic_write_text(self.current_date_path, str(current).strip() + "\n", self.owner)
        return existing

    def current_date(self) -> dt.date:
        text = self.current_date_path.read_text(encoding="utf-8").strip()
        if not text:
            return self.start_date
        return parse_iso_date(text)

    def set_current_date(self, day: dt.date) -> None:
        self.state["current_date"] = day.isoformat()
        self.state["updated_at"] = now_iso()
        atomic_write_text(self.current_date_path, day.isoformat() + "\n", self.owner)
        atomic_write_json(self.state_path, self.state, self.owner)

    def mark_day_completed(self, day: dt.date, next_day: dt.date | None) -> None:
        self.state["last_completed_day"] = day.isoformat()
        self.state["updated_at"] = now_iso()
        self.state["status"] = "running"
        if next_day is not None:
            self.state["current_date"] = next_day.isoformat()
            atomic_write_text(self.current_date_path, next_day.isoformat() + "\n", self.owner)
        atomic_write_json(self.state_path, self.state, self.owner)

    def mark_completed(self) -> None:
        finished = self.end_date + dt.timedelta(days=1)
        self.state["status"] = "completed"
        self.state["current_date"] = finished.isoformat()
        self.state["updated_at"] = now_iso()
        atomic_write_text(self.current_date_path, finished.isoformat() + "\n", self.owner)
        atomic_write_json(self.state_path, self.state, self.owner)
