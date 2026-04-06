#!/usr/bin/env python3
"""Export Orthanc studies day by day into ZIP files with resumable state.

This script walks a local Orthanc instance from a start date to an end date
(inclusive). For each day, it creates ~/backup/YYYY/MM/DD (or another backup
root passed by CLI), lists all studies that exist in Orthanc for that exact
StudyDate, and writes one ZIP archive per study either by downloading
/studies/{id}/archive or by building the ZIP locally with ZIP_STORED.

The run is resumable. It persists:
  * current-date.txt  -> next date to process
  * state.json        -> global run metadata
  * logs/run.log      -> full log
  * logs/errors.log   -> errors only
  * days/YYYY-MM-DD/studies.json -> cached study list for that day
  * days/YYYY-MM-DD/status.json  -> per-study status
  * days/YYYY-MM-DD/progress.tsv -> human-readable progress table

If interrupted during an export, any stale ".part" file is deleted and the
study is re-downloaded on the next run.
"""

from __future__ import annotations

import argparse
import json
import os
import pwd
import re
import sys
import time
import unicodedata
import zipfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable
from urllib import parse

from orthanc_tools.config import (
    build_base_url as config_build_base_url,
    first_registered_user as config_first_registered_user,
    read_json_file as read_json_config,
    resolve_config_paths,
)
from orthanc_tools.orthanc_api import OrthancApiError, OrthancRestClient

SCRIPT_NAME = "orthanc-export-local-by-date.py"
STATE_VERSION = 1
DEFAULT_TIMEOUT = 120.0
DEFAULT_PAGE_SIZE = 200
DEFAULT_RETRIES = 3
DEFAULT_RETRY_DELAY = 5.0
DOWNLOAD_CHUNK_SIZE = 1024 * 1024

REQUESTED_TAGS = [
    "StudyInstanceUID",
    "PatientName",
    "PatientBirthDate",
    "StudyDate",
    "StudyDescription",
    "AccessionNumber",
]


OrthancHttpError = OrthancApiError


@dataclass(frozen=True)
class Ownership:
    uid: int
    gid: int


@dataclass(frozen=True)
class OrthancSettings:
    base_url: str
    username: str
    password: str
    timeout: float


@dataclass(frozen=True)
class StudyInfo:
    orthanc_id: str
    study_uid: str
    patient_name: str
    patient_birth_date: str
    study_date: str
    study_description: str
    accession_number: str
    is_stable: bool | None


@dataclass(frozen=True)
class InstanceInfo:
    orthanc_id: str
    parent_series_id: str
    sop_instance_uid: str
    instance_number: str
    index_in_series: int | None
    file_size: int | None


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def cli_error(message: str) -> None:
    print(f"ERROR: {message}", file=sys.stderr)
    raise SystemExit(1)


def parse_iso_date(value: str) -> date:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"Invalid date {value!r}; expected YYYY-MM-DD"
        ) from exc


def date_to_orthanc(day: date) -> str:
    return day.strftime("%Y%m%d")


def preferred_home() -> Path:
    sudo_user = os.environ.get("SUDO_USER")
    if os.geteuid() == 0 and sudo_user and sudo_user != "root":
        try:
            return Path(pwd.getpwnam(sudo_user).pw_dir)
        except KeyError:
            pass
    return Path.home()


def default_owner() -> Ownership | None:
    sudo_user = os.environ.get("SUDO_USER")
    if os.geteuid() == 0 and sudo_user and sudo_user != "root":
        try:
            entry = pwd.getpwnam(sudo_user)
            return Ownership(uid=entry.pw_uid, gid=entry.pw_gid)
        except KeyError:
            return None
    return None


def maybe_chown(path: Path, owner: Ownership | None) -> None:
    if owner is None:
        return
    try:
        os.chown(path, owner.uid, owner.gid)
    except FileNotFoundError:
        return
    except PermissionError:
        return
    except OSError:
        return


def ensure_directory(path: Path, owner: Ownership | None) -> None:
    path.mkdir(parents=True, exist_ok=True)
    maybe_chown(path, owner)


def atomic_write_text(path: Path, content: str, owner: Ownership | None) -> None:
    ensure_directory(path.parent, owner)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
    maybe_chown(path, owner)


def atomic_write_json(path: Path, payload: Any, owner: Ownership | None) -> None:
    ensure_directory(path.parent, owner)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=False)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
    maybe_chown(path, owner)


def load_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def unwrap_tag_value(value: Any) -> str | None:
    if value in (None, ""):
        return None
    if isinstance(value, dict):
        nested = value.get("Value")
        if nested in (None, ""):
            return None
        if isinstance(nested, list):
            parts = [str(item).strip() for item in nested if str(item).strip()]
            return "\\".join(parts) if parts else None
        return str(nested)
    if isinstance(value, list):
        parts = [str(item).strip() for item in value if str(item).strip()]
        return "\\".join(parts) if parts else None
    return str(value)


def pick_tag(item: Any, tag: str) -> str | None:
    if isinstance(item, dict):
        value = unwrap_tag_value(item.get(tag))
        if value not in (None, ""):
            return value
        for key in ("RequestedTags", "MainDicomTags", "PatientMainDicomTags"):
            child = item.get(key)
            if isinstance(child, dict):
                child_value = unwrap_tag_value(child.get(tag))
                if child_value not in (None, ""):
                    return child_value
    return None


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
    import hashlib

    digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]
    head = max(16, limit - 13)
    return f"{text[:head]}_{digest}"


def build_patient_base(study: StudyInfo, day: date) -> str:
    patient = ascii_slug(study.patient_name or "UNKNOWN")
    birth = ascii_slug(study.patient_birth_date or "UNKNOWN")
    study_date = ascii_slug(normalize_study_day(study.study_date, day), fallback=date_to_orthanc(day))
    base = f"{patient}_{birth}_{study_date}"
    return truncate_with_hash(base)


def validate_zip_file(path: Path) -> None:
    if not path.exists():
        raise RuntimeError(f"ZIP file not found: {path}")
    if path.stat().st_size <= 0:
        raise RuntimeError(f"ZIP file is empty: {path}")
    if not zipfile.is_zipfile(path):
        raise RuntimeError(f"Invalid ZIP file: {path}")
    with zipfile.ZipFile(path, "r") as archive:
        names = [name for name in archive.namelist() if not name.endswith("/")]
        if not names:
            raise RuntimeError(f"ZIP file has no members: {path}")
        bad = archive.testzip()
        if bad is not None:
            raise RuntimeError(f"ZIP CRC validation failed at member {bad!r} in {path}")


class Logger:
    def __init__(self, run_log: Path, error_log: Path, owner: Ownership | None):
        self.run_log = run_log
        self.error_log = error_log
        self.owner = owner
        ensure_directory(run_log.parent, owner)
        ensure_directory(error_log.parent, owner)

    def _append(self, path: Path, line: str) -> None:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        maybe_chown(path, self.owner)

    def info(self, message: str) -> None:
        line = f"[{now_iso()}] INFO  {message}"
        print(line)
        self._append(self.run_log, line)

    def error(self, message: str) -> None:
        line = f"[{now_iso()}] ERROR {message}"
        print(line, file=sys.stderr)
        self._append(self.run_log, line)
        self._append(self.error_log, line)


class OrthancClient(OrthancRestClient):
    def __init__(self, settings: OrthancSettings):
        self.settings = settings
        super().__init__(settings.base_url, settings.username, settings.password, timeout=settings.timeout)

    def download(self, path: str, output_path: Path) -> dict[str, Any]:
        return self.request("GET", path, accept="application/octet-stream", stream_to=output_path)

    def download_into_handle(self, path: str, handle: Any) -> dict[str, Any]:
        return self.request("GET", path, accept="application/octet-stream", stream_handle=handle)

    def system(self) -> dict[str, Any]:
        payload = self.get("/system")
        if not isinstance(payload, dict):
            raise RuntimeError("/system did not return a JSON object")
        return payload

    def find_studies_for_day(self, day: date, page_size: int) -> list[StudyInfo]:
        day_string = date_to_orthanc(day)
        studies: list[StudyInfo] = []
        seen_ids: set[str] = set()
        since = 0

        while True:
            payload = {
                "Level": "Study",
                "Expand": True,
                "Query": {
                    "StudyDate": day_string,
                },
                "RequestedTags": REQUESTED_TAGS,
                "Limit": page_size,
                "Since": since,
            }
            page = self.post("/tools/find", payload)
            if not isinstance(page, list):
                raise RuntimeError("/tools/find did not return a list")
            if not page:
                break

            new_items = 0
            for item in page:
                if not isinstance(item, dict):
                    raise RuntimeError("/tools/find returned a non-object item")
                orthanc_id = str(item.get("ID") or "").strip()
                study_uid = (pick_tag(item, "StudyInstanceUID") or "").strip()
                if not orthanc_id:
                    raise RuntimeError("/tools/find returned a study without ID")
                if not study_uid:
                    raise RuntimeError(
                        f"Study {orthanc_id} is missing StudyInstanceUID in the /tools/find response"
                    )
                if orthanc_id in seen_ids:
                    continue
                seen_ids.add(orthanc_id)
                studies.append(
                    StudyInfo(
                        orthanc_id=orthanc_id,
                        study_uid=study_uid,
                        patient_name=(pick_tag(item, "PatientName") or "").strip(),
                        patient_birth_date=(pick_tag(item, "PatientBirthDate") or "").strip(),
                        study_date=(pick_tag(item, "StudyDate") or "").strip(),
                        study_description=(pick_tag(item, "StudyDescription") or "").strip(),
                        accession_number=(pick_tag(item, "AccessionNumber") or "").strip(),
                        is_stable=item.get("IsStable") if isinstance(item.get("IsStable"), bool) else None,
                    )
                )
                new_items += 1

            if new_items == 0:
                raise RuntimeError("Pagination stalled while listing studies for the day")
            since += len(page)

        studies.sort(key=lambda s: (normalize_study_day(s.study_date, day), s.study_uid, s.orthanc_id))
        return studies

    def find_study_by_uid(self, study_uid: str) -> StudyInfo | None:
        payload = {
            "Level": "Study",
            "Expand": True,
            "Query": {
                "StudyInstanceUID": study_uid,
            },
            "RequestedTags": REQUESTED_TAGS,
            "Limit": 2,
            "Since": 0,
        }
        result = self.post("/tools/find", payload)
        if not isinstance(result, list):
            raise RuntimeError("/tools/find did not return a list while locating StudyInstanceUID")
        if not result:
            return None
        if len(result) > 1:
            raise RuntimeError(f"More than one study matched StudyInstanceUID {study_uid}")
        item = result[0]
        if not isinstance(item, dict):
            raise RuntimeError("/tools/find returned a non-object item while locating a study")
        orthanc_id = str(item.get("ID") or "").strip()
        if not orthanc_id:
            raise RuntimeError(f"/tools/find returned no ID for StudyInstanceUID {study_uid}")
        return StudyInfo(
            orthanc_id=orthanc_id,
            study_uid=(pick_tag(item, "StudyInstanceUID") or "").strip() or study_uid,
            patient_name=(pick_tag(item, "PatientName") or "").strip(),
            patient_birth_date=(pick_tag(item, "PatientBirthDate") or "").strip(),
            study_date=(pick_tag(item, "StudyDate") or "").strip(),
            study_description=(pick_tag(item, "StudyDescription") or "").strip(),
            accession_number=(pick_tag(item, "AccessionNumber") or "").strip(),
            is_stable=item.get("IsStable") if isinstance(item.get("IsStable"), bool) else None,
        )

    def download_study_archive(self, orthanc_id: str, output_path: Path) -> dict[str, Any]:
        encoded = parse.quote(orthanc_id, safe="")
        return self.download(f"/studies/{encoded}/archive", output_path)

    def list_study_series(self, orthanc_id: str) -> list[dict[str, Any]]:
        encoded = parse.quote(orthanc_id, safe="")
        payload = self.get(f"/studies/{encoded}/series?expand")
        if not isinstance(payload, list):
            raise RuntimeError(f"/studies/{orthanc_id}/series?expand did not return a list")
        if not all(isinstance(item, dict) for item in payload):
            raise RuntimeError(f"/studies/{orthanc_id}/series?expand returned a non-object item")
        return payload  # type: ignore[return-value]

    def list_study_instances(self, orthanc_id: str) -> list[InstanceInfo]:
        encoded = parse.quote(orthanc_id, safe="")
        payload = self.get(f"/studies/{encoded}/instances")
        if not isinstance(payload, list):
            raise RuntimeError(f"/studies/{orthanc_id}/instances did not return a list")
        result: list[InstanceInfo] = []
        for item in payload:
            if not isinstance(item, dict):
                raise RuntimeError(f"/studies/{orthanc_id}/instances returned a non-object item")
            instance_id = str(item.get("ID") or "").strip()
            if not instance_id:
                raise RuntimeError(f"/studies/{orthanc_id}/instances returned an item without ID")
            result.append(
                InstanceInfo(
                    orthanc_id=instance_id,
                    parent_series_id=str(item.get("ParentSeries") or "").strip(),
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

    def download_instance_file_into_handle(self, orthanc_id: str, handle: Any) -> dict[str, Any]:
        encoded = parse.quote(orthanc_id, safe="")
        return self.download_into_handle(f"/instances/{encoded}/file", handle)


class RunState:
    def __init__(
        self,
        state_dir: Path,
        owner: Ownership | None,
        logger: Logger,
        *,
        backup_dir: Path,
        start_date: date,
        end_date: date,
        name_mode: str,
        zip_mode: str,
        orthanc_base_url: str,
    ):
        self.state_dir = state_dir
        self.owner = owner
        self.logger = logger
        self.backup_dir = backup_dir
        self.start_date = start_date
        self.end_date = end_date
        self.name_mode = name_mode
        self.zip_mode = zip_mode
        self.orthanc_base_url = orthanc_base_url
        self.state_path = state_dir / "state.json"
        self.current_date_path = state_dir / "current-date.txt"
        ensure_directory(self.state_dir, owner)
        self.state = self._load_or_initialize()

    def _base_metadata(self) -> dict[str, Any]:
        return {
            "script": SCRIPT_NAME,
            "state_version": STATE_VERSION,
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

        for key, expected in self._base_metadata().items():
            actual = existing.get(key)
            if actual != expected:
                cli_error(
                    f"Existing state in {self.state_dir} was created for a different run parameter: "
                    f"{key}={actual!r}, expected {expected!r}. Use another --state-dir."
                )

        if not self.current_date_path.exists():
            current = existing.get("current_date", self.start_date.isoformat())
            atomic_write_text(self.current_date_path, str(current).strip() + "\n", self.owner)

        return existing

    def current_date(self) -> date:
        text = self.current_date_path.read_text(encoding="utf-8").strip()
        if not text:
            return self.start_date
        return parse_iso_date(text)

    def set_current_date(self, day: date) -> None:
        self.state["current_date"] = day.isoformat()
        self.state["updated_at"] = now_iso()
        atomic_write_text(self.current_date_path, day.isoformat() + "\n", self.owner)
        atomic_write_json(self.state_path, self.state, self.owner)

    def mark_day_completed(self, day: date, next_day: date | None) -> None:
        self.state["last_completed_day"] = day.isoformat()
        self.state["updated_at"] = now_iso()
        self.state["status"] = "running"
        if next_day is not None:
            self.state["current_date"] = next_day.isoformat()
            atomic_write_text(self.current_date_path, next_day.isoformat() + "\n", self.owner)
        atomic_write_json(self.state_path, self.state, self.owner)

    def mark_completed(self) -> None:
        finished = self.end_date + timedelta(days=1)
        self.state["status"] = "completed"
        self.state["current_date"] = finished.isoformat()
        self.state["updated_at"] = now_iso()
        atomic_write_text(self.current_date_path, finished.isoformat() + "\n", self.owner)
        atomic_write_json(self.state_path, self.state, self.owner)


class ExportApp:
    def __init__(self, args: argparse.Namespace, client: OrthancClient, owner: Ownership | None):
        self.args = args
        self.client = client
        self.owner = owner
        self.backup_dir = args.backup_dir
        self.state_dir = args.state_dir
        self.logs_dir = self.state_dir / "logs"
        self.days_state_dir = self.state_dir / "days"
        self.logger = Logger(self.logs_dir / "run.log", self.logs_dir / "errors.log", owner)
        self.run_state = RunState(
            self.state_dir,
            owner,
            self.logger,
            backup_dir=self.backup_dir,
            start_date=args.start_date,
            end_date=args.end_date,
            name_mode=args.name,
            zip_mode=args.zip_mode,
            orthanc_base_url=self.client.settings.base_url,
        )

    def run(self) -> int:
        ensure_directory(self.backup_dir, self.owner)
        ensure_directory(self.days_state_dir, self.owner)

        system = self.client.system()
        self.logger.info(
            f"Connected to Orthanc {system.get('Version', '?')} at {self.client.settings.base_url}"
        )
        self.logger.info(
            f"Backup root: {self.backup_dir} | State dir: {self.state_dir} | Name mode: {self.args.name} | Zip mode: {self.args.zip_mode}"
        )

        current = self.run_state.current_date()
        if current < self.args.start_date:
            current = self.args.start_date
            self.run_state.set_current_date(current)

        while current <= self.args.end_date:
            self.run_state.set_current_date(current)
            ok = self.process_day(current)
            if not ok:
                self.logger.error(
                    f"Day {current.isoformat()} is incomplete. Fix the error and rerun the script; it will resume from this day."
                )
                return 2
            current += timedelta(days=1)
            if current <= self.args.end_date:
                self.run_state.mark_day_completed(current - timedelta(days=1), current)
            else:
                self.run_state.mark_day_completed(current - timedelta(days=1), None)

        self.run_state.mark_completed()
        self.logger.info(
            f"Export completed for the inclusive range {self.args.start_date.isoformat()} to {self.args.end_date.isoformat()}"
        )
        return 0

    def process_day(self, day: date) -> bool:
        day_dir = self.output_day_dir(day)
        day_state_dir = self.days_state_dir / day.isoformat()
        ensure_directory(day_dir, self.owner)
        ensure_directory(day_state_dir, self.owner)

        studies_path = day_state_dir / "studies.json"
        status_path = day_state_dir / "status.json"
        progress_path = day_state_dir / "progress.tsv"

        if status_path.exists():
            status = load_json(status_path, default={})
        else:
            status = {
                "date": day.isoformat(),
                "output_dir": str(day_dir),
                "name_mode": self.args.name,
                "zip_mode": self.args.zip_mode,
                "complete": False,
                "created_at": now_iso(),
                "updated_at": now_iso(),
                "studies": {},
            }

        live_studies = self.refresh_day_inventory(day, studies_path, status)
        self.sync_existing_completed_files(day_dir, status)
        self.write_day_status(status_path, progress_path, status)

        passes = 0
        while True:
            passes += 1
            pending = [
                entry for entry in self.iter_day_entries(status)
                if entry.get("required", True) and entry.get("status") != "completed"
            ]
            if not pending:
                refreshed = self.refresh_day_inventory(day, studies_path, status)
                self.sync_existing_completed_files(day_dir, status)
                self.write_day_status(status_path, progress_path, status)
                pending = [
                    entry for entry in self.iter_day_entries(status)
                    if entry.get("required", True) and entry.get("status") != "completed"
                ]
                if not pending:
                    status["complete"] = True
                    status["updated_at"] = now_iso()
                    self.write_day_status(status_path, progress_path, status)
                    self.logger.info(
                        f"Day {day.isoformat()} completed: {len(status['studies'])} study ZIP(s) present in {day_dir}"
                    )
                    return True
                if refreshed:
                    self.logger.info(
                        f"Day {day.isoformat()} gained new studies during the final refresh; continuing the same day"
                    )

            progress_this_pass = False
            self.logger.info(
                f"Processing day {day.isoformat()} pass {passes}: {len(pending)} study(ies) pending"
            )
            for entry in pending:
                if self.export_one_study(day, day_dir, status, entry, status_path, progress_path):
                    progress_this_pass = True

            if not progress_this_pass:
                self.write_day_status(status_path, progress_path, status)
                return False

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

    def merge_studies_into_status(self, day: date, studies: list[StudyInfo], status: dict[str, Any]) -> None:
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

        # Preserve existing assignments when possible.
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
                study = StudyInfo(
                    orthanc_id=str(entry["orthanc_id"]),
                    study_uid=study_uid,
                    patient_name=str(entry.get("patient_name") or ""),
                    patient_birth_date=str(entry.get("patient_birth_date") or ""),
                    study_date=str(entry.get("study_date") or ""),
                    study_description=str(entry.get("study_description") or ""),
                    accession_number=str(entry.get("accession_number") or ""),
                    is_stable=entry.get("is_stable") if isinstance(entry.get("is_stable"), bool) else None,
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
        ordered = sorted(
            studies.values(),
            key=lambda e: (
                str(e.get("study_date") or ""),
                str(e.get("filename") or ""),
                str(e.get("study_uid") or ""),
            ),
        )
        return ordered

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
                    self.logger.error(
                        f"Existing ZIP is invalid for study {entry['study_uid']}: {final_zip} ({exc})"
                    )
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
            except Exception:
                final_zip.unlink(missing_ok=True)
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
            except OrthancHttpError as exc:
                partial_zip.unlink(missing_ok=True)
                if exc.status == 404:
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
                self.logger.error(
                    f"Failed study {study_uid} attempt {attempt}/{self.args.retries}: {exc}"
                )
            except Exception as exc:
                partial_zip.unlink(missing_ok=True)
                entry["status"] = "error"
                entry["error"] = str(exc)
                entry["updated_at"] = now_iso()
                self.write_day_status(status_path, progress_path, status)
                self.logger.error(
                    f"Failed study {study_uid} attempt {attempt}/{self.args.retries}: {exc}"
                )

            if attempt < self.args.retries:
                time.sleep(self.args.retry_delay)

        return False

    def build_local_stored_zip(self, day: date, entry: dict[str, Any], output_path: Path) -> dict[str, Any]:
        study = StudyInfo(
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
        series_labels: dict[str, str] = {}
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
            series_labels[series_id] = ascii_slug(f"{modality}_{description}", fallback=f"SERIES_{index:03d}")

        used_paths: set[str] = set()
        bytes_written = 0
        with zipfile.ZipFile(output_path, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
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
    def study_to_json(study: StudyInfo, day: date) -> dict[str, Any]:
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


def read_json_file(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def first_registered_user(credentials: dict[str, Any]) -> tuple[str, str]:
    users = credentials.get("RegisteredUsers")
    if not isinstance(users, dict) or not users:
        raise ValueError("RegisteredUsers is missing or empty in credentials.json")
    username = next(iter(users))
    password = users[username]
    if not isinstance(password, str):
        raise ValueError(f"Password for Orthanc user {username!r} is not a string")
    return username, password


def load_settings(args: argparse.Namespace) -> OrthancSettings:
    orthanc_config, credentials_config = resolve_config_paths(
        args.config_dir,
        args.orthanc_config,
        args.credentials_config,
    )

    config: dict[str, Any] = {}
    if orthanc_config.exists():
        data = read_json_config(orthanc_config)
        if isinstance(data, dict):
            config = data
    elif not args.base_url:
        cli_error(f"Orthanc config file not found: {orthanc_config}")

    username = args.user
    password = args.password
    if (username is None or password is None) and credentials_config.exists():
        credentials = read_json_config(credentials_config)
        if not isinstance(credentials, dict):
            cli_error(f"Invalid JSON object in {credentials_config}")
        user_from_cfg, password_from_cfg = config_first_registered_user(credentials)
        username = username or user_from_cfg
        password = password or password_from_cfg

    if username is None or password is None:
        cli_error("Orthanc credentials were not provided; use --user and --password or make credentials.json readable")

    http_port = config.get("HttpPort", 8042) if isinstance(config, dict) else 8042
    base_url = args.base_url or config_build_base_url(config, default_port=int(http_port))
    return OrthancSettings(
        base_url=base_url,
        username=username,
        password=password,
        timeout=args.timeout,
    )


def parse_args() -> argparse.Namespace:
    home = preferred_home()
    default_backup_dir = home / "backup"
    default_state_dir = default_backup_dir / ".orthanc-export-state"

    parser = argparse.ArgumentParser(
        description=(
            "Export all local Orthanc studies in an inclusive date range into one ZIP per study, "
            "stored as backup/YYYY/MM/DD/*.zip with resumable progress files."
        )
    )
    parser.add_argument("--start-date", type=parse_iso_date, required=True, help="Inclusive start date in YYYY-MM-DD")
    parser.add_argument("--end-date", type=parse_iso_date, required=True, help="Inclusive end date in YYYY-MM-DD")
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
        "--backup-dir",
        type=lambda s: Path(s).expanduser(),
        default=default_backup_dir,
        help=f"Backup root directory. Default: {default_backup_dir}",
    )
    parser.add_argument(
        "--state-dir",
        type=lambda s: Path(s).expanduser(),
        default=default_state_dir,
        help=f"Directory for resumable state/log files. Default: {default_state_dir}",
    )
    parser.add_argument("--base-url", help="Orthanc REST base URL. Default: http://127.0.0.1:<HttpPort from config>")
    parser.add_argument("--user", help="Orthanc REST username")
    parser.add_argument("--password", help="Orthanc REST password")
    parser.add_argument(
        "--config-dir",
        type=lambda s: Path(s).expanduser(),
        default=Path("/etc/orthanc"),
        help="Orthanc config directory. Default: /etc/orthanc",
    )
    parser.add_argument(
        "--orthanc-config",
        type=lambda s: Path(s).expanduser(),
        help="Explicit path to orthanc.json. Overrides --config-dir for the main config.",
    )
    parser.add_argument(
        "--credentials-config",
        type=lambda s: Path(s).expanduser(),
        help="Explicit path to credentials.json. Overrides --config-dir for credentials.",
    )
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help=f"HTTP timeout in seconds. Default: {DEFAULT_TIMEOUT}")
    parser.add_argument(
        "--page-size",
        type=int,
        default=DEFAULT_PAGE_SIZE,
        help=(
            "Requested page size for /tools/find pagination. The Orthanc server may clamp this value. "
            f"Default: {DEFAULT_PAGE_SIZE}"
        ),
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=DEFAULT_RETRIES,
        help=f"How many download attempts per study in one run. Default: {DEFAULT_RETRIES}",
    )
    parser.add_argument(
        "--retry-delay",
        type=float,
        default=DEFAULT_RETRY_DELAY,
        help=f"Delay in seconds between retries. Default: {DEFAULT_RETRY_DELAY}",
    )
    parser.add_argument(
        "--zip-mode",
        choices=("archive", "stored"),
        default="stored",
        help=(
            "ZIP creation mode. Default: 'stored'. "
            "'archive' downloads /studies/{id}/archive from Orthanc; "
            "'stored' builds the ZIP locally with ZIP_STORED by downloading each instance."
        ),
    )

    args = parser.parse_args()

    today = date.today()
    if args.start_date > args.end_date:
        cli_error("--start-date must be less than or equal to --end-date")
    if args.end_date > today:
        cli_error(f"--end-date cannot be in the future (today is {today.isoformat()})")
    if args.page_size <= 0:
        cli_error("--page-size must be a positive integer")
    if args.retries <= 0:
        cli_error("--retries must be a positive integer")
    if args.retry_delay < 0:
        cli_error("--retry-delay cannot be negative")

    args.backup_dir = args.backup_dir.resolve()
    args.state_dir = args.state_dir.resolve()
    if args.orthanc_config is not None:
        args.orthanc_config = args.orthanc_config.resolve()
    if args.credentials_config is not None:
        args.credentials_config = args.credentials_config.resolve()
    args.config_dir = args.config_dir.resolve()
    return args


def main() -> int:
    args = parse_args()
    owner = default_owner()
    settings = load_settings(args)
    client = OrthancClient(settings)
    app = ExportApp(args, client, owner)
    return app.run()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        raise SystemExit(130)
