#!/usr/bin/env python3
"""Mirror a remote DICOM modality into local Orthanc with a live terminal UI."""

from __future__ import annotations

import argparse
import json
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib import parse

from orthanc_tools.config import (
    build_base_url as config_build_base_url,
    first_registered_user as config_first_registered_user,
    read_json_file as parse_json_config,
    resolve_config_paths,
)
from orthanc_tools.orthanc_api import OrthancApiError, OrthancRestClient

try:
    from rich import box
    from rich.console import Group
    from rich.live import Live
    from rich.panel import Panel
    from rich.table import Table

    RICH_AVAILABLE = True
except Exception:
    Group = Live = Panel = Table = box = None  # type: ignore[assignment]
    RICH_AVAILABLE = False


STOP_REQUESTED = False


def handle_signal(signum: int, _frame: Any) -> None:
    global STOP_REQUESTED
    STOP_REQUESTED = True
    signal_name = signal.Signals(signum).name
    print(f"\nSignal received: {signal_name}. Finishing the current operation.", file=sys.stderr)


signal.signal(signal.SIGINT, handle_signal)
signal.signal(signal.SIGTERM, handle_signal)


def cli_error(message: str) -> None:
    print(f"ERROR: {message}", file=sys.stderr)
    raise SystemExit(1)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Query a remote Orthanc modality, retrieve missing studies, then verify "
            "that local Orthanc contains the same studies, series and instances."
        )
    )
    parser.add_argument(
        "--remote",
        help="Remote modality name as configured in Orthanc DicomModalities.",
    )
    parser.add_argument(
        "--base-url",
        help="Orthanc REST base URL. Defaults to http://127.0.0.1:<HttpPort> from the config.",
    )
    parser.add_argument("--user", help="Orthanc REST username.")
    parser.add_argument("--password", help="Orthanc REST password.")
    parser.add_argument(
        "--config-dir",
        default="/etc/orthanc",
        help="Orthanc config directory. Default: /etc/orthanc",
    )
    parser.add_argument(
        "--orthanc-config",
        help="Explicit path to orthanc.json. Overrides --config-dir for the main config.",
    )
    parser.add_argument(
        "--credentials-config",
        help="Explicit path to credentials.json. Overrides --config-dir for credentials.",
    )
    parser.add_argument(
        "--repair-mode",
        choices=("replace", "retrieve"),
        default="replace",
        help=(
            "replace: delete local drift before re-fetching so the mirror can be exact. "
            "retrieve: only add missing data. Default: replace"
        ),
    )
    parser.add_argument(
        "--target-aet",
        help=(
            "Optional C-MOVE destination AE title. Ignored when --retrieve-method=get."
        ),
    )
    parser.add_argument(
        "--retrieve-method",
        choices=("get", "move"),
        default="get",
        help=(
            "DICOM retrieve method to use against the remote modality. "
            "Default: get"
        ),
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=60.0,
        help="HTTP timeout in seconds for Orthanc REST calls. Default: 60",
    )
    parser.add_argument(
        "--settle-seconds",
        type=float,
        default=5.0,
        help="Extra time to wait after retrieve/delete before re-checking. Default: 5",
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        default=2,
        help="How many repair attempts per study before marking failure. Default: 2",
    )
    parser.add_argument(
        "--no-rich",
        action="store_true",
        help="Disable the live rich dashboard and print plain log lines instead.",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Do not prompt before destructive repair in replace mode.",
    )
    parser.add_argument(
        "--limit-studies",
        type=int,
        help="Only process the first N remote studies. Useful for testing.",
    )
    parser.add_argument(
        "--allow-empty-remote",
        action="store_true",
        help="Allow the run to succeed if the remote modality returns zero studies.",
    )
    return parser.parse_args()


def parse_json_file(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def first_registered_user(credentials: dict[str, Any]) -> tuple[str, str]:
    users = credentials.get("RegisteredUsers")
    if not isinstance(users, dict) or not users:
        raise ValueError("RegisteredUsers is missing or empty in credentials.json.")
    username = next(iter(users))
    password = users[username]
    if not isinstance(password, str):
        raise ValueError(f"Password for Orthanc user {username!r} is not a string.")
    return username, password


def pick_tag(item: Any, tag: str) -> str | None:
    if isinstance(item, dict):
        direct = item.get(tag)
        if direct not in (None, ""):
            return str(direct)
        for key in ("RequestedTags", "MainDicomTags", "PatientMainDicomTags"):
            child = item.get(key)
            if isinstance(child, dict):
                value = child.get(tag)
                if value not in (None, ""):
                    return str(value)
    return None


def parse_count(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.isdigit():
            return int(stripped)
    return None


def safe_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


def format_count(value: int | None) -> str:
    return "?" if value is None else str(value)


def short_uid(uid: str | None, length: int = 28) -> str:
    if not uid:
        return "-"
    if len(uid) <= length:
        return uid
    head = max(10, length - 10)
    return f"{uid[:head]}...{uid[-7:]}"


def human_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h{minutes:02d}m{secs:02d}s"
    if minutes:
        return f"{minutes}m{secs:02d}s"
    return f"{secs}s"


@dataclass
class OrthancSettings:
    base_url: str
    username: str
    password: str
    dicom_aet: str
    dicom_modalities: dict[str, Any]


class OrthancClient(OrthancRestClient):
    def __init__(self, settings: OrthancSettings, timeout: float):
        self.settings = settings
        super().__init__(settings.base_url, settings.username, settings.password, timeout=timeout)

    def system(self) -> dict[str, Any]:
        return self.get("/system")

    def list_modalities(self) -> list[str]:
        payload = self.get("/modalities")
        if not isinstance(payload, list):
            raise RuntimeError("/modalities did not return a list.")
        return [safe_text(item) for item in payload]

    def create_remote_study_query(self, modality: str) -> str:
        payload = {
            "Level": "Study",
            "Query": {
                "StudyInstanceUID": "",
                "PatientName": "",
                "PatientID": "",
                "StudyDate": "",
                "StudyDescription": "",
                "AccessionNumber": "",
                "NumberOfStudyRelatedSeries": "",
                "NumberOfStudyRelatedInstances": "",
            },
        }
        response = self.post(f"/modalities/{parse.quote(modality, safe='')}/query", payload)
        return extract_resource_id(response)

    def get_query_answers(self, query_id: str) -> list[str]:
        answers = self.get(f"/queries/{parse.quote(query_id, safe='')}/answers")
        if not isinstance(answers, list):
            raise RuntimeError(f"/queries/{query_id}/answers did not return a list.")
        return [safe_text(answer) for answer in answers]

    def get_query_answer_content(self, query_id: str, answer_id: str) -> dict[str, Any]:
        content = self.get(
            f"/queries/{parse.quote(query_id, safe='')}/answers/{parse.quote(answer_id, safe='')}/content"
        )
        if not isinstance(content, dict):
            raise RuntimeError(f"Unexpected answer content for query {query_id}, answer {answer_id}.")
        return content

    def get_query_answer_pairs(self, query_id: str) -> list[tuple[str, dict[str, Any]]]:
        answer_ids = self.get_query_answers(query_id)
        if not answer_ids:
            return []
        try:
            expanded = self.get(
                f"/queries/{parse.quote(query_id, safe='')}/answers?expand&simplify"
            )
            if (
                isinstance(expanded, list)
                and len(expanded) == len(answer_ids)
                and all(isinstance(item, dict) for item in expanded)
            ):
                return list(zip(answer_ids, expanded))
        except Exception:
            pass
        return [
            (answer_id, self.get_query_answer_content(query_id, answer_id))
            for answer_id in answer_ids
        ]

    def create_child_query(
        self,
        query_id: str,
        answer_id: str,
        child_level: str,
        tags: dict[str, str],
    ) -> str:
        response = self.post(
            f"/queries/{parse.quote(query_id, safe='')}/answers/{parse.quote(answer_id, safe='')}/query-{child_level}",
            {"Query": tags},
        )
        return extract_resource_id(response)

    def lookup_local_study(self, study_uid: str) -> dict[str, Any] | None:
        payload = {
            "Level": "Study",
            "Expand": True,
            "Query": {
                "StudyInstanceUID": study_uid,
            },
        }
        result = self.post("/tools/find", payload)
        if not isinstance(result, list):
            raise RuntimeError("Unexpected response from /tools/find.")
        if not result:
            return None
        if len(result) > 1:
            raise RuntimeError(
                f"Local Orthanc contains multiple studies with StudyInstanceUID {study_uid}."
            )
        item = result[0]
        if not isinstance(item, dict):
            raise RuntimeError("Expanded /tools/find response was not a JSON object.")
        return item

    def get_study_statistics(self, study_id: str) -> dict[str, Any]:
        stats = self.get(f"/studies/{parse.quote(study_id, safe='')}/statistics")
        if not isinstance(stats, dict):
            raise RuntimeError(f"Unexpected statistics response for study {study_id}.")
        return stats

    def get_study_series_expanded(self, study_id: str) -> list[dict[str, Any]]:
        items = self.get(f"/studies/{parse.quote(study_id, safe='')}/series?expand")
        if isinstance(items, list) and all(isinstance(item, dict) for item in items):
            return items
        if isinstance(items, list):
            return [
                self.get(f"/series/{parse.quote(safe_text(series_id), safe='')}")
                for series_id in items
            ]
        raise RuntimeError(f"Unexpected series listing for study {study_id}.")

    def get_study_instances_expanded(self, study_id: str) -> list[dict[str, Any]]:
        items = self.get(f"/studies/{parse.quote(study_id, safe='')}/instances?expand")
        if isinstance(items, list) and all(isinstance(item, dict) for item in items):
            return items
        if isinstance(items, list):
            return [
                self.get(f"/instances/{parse.quote(safe_text(instance_id), safe='')}")
                for instance_id in items
            ]
        raise RuntimeError(f"Unexpected instance listing for study {study_id}.")

    def retrieve_study(
        self,
        modality: str,
        study_uid: str,
        method: str,
        target_aet: str | None,
    ) -> Any:
        if method == "move":
            payload: dict[str, Any] = {
                "Level": "Study",
                "Resources": [
                    {
                        "StudyInstanceUID": study_uid,
                    }
                ],
                # Fail loudly on retrieve errors so the TUI shows the real DICOM issue.
                "Synchronous": True,
                "TargetAet": target_aet or self.settings.dicom_aet,
            }
            return self.post(f"/modalities/{parse.quote(modality, safe='')}/move", payload)

        remote = self.settings.dicom_modalities.get(modality)
        if remote is None:
            raise RuntimeError(f"Remote modality {modality!r} is missing from DicomModalities.")
        remote_aet, remote_host, remote_port = extract_modality_endpoint(remote)

        with tempfile.TemporaryDirectory(prefix="orthanc-getscu-") as temp_dir:
            command = [
                "getscu",
                "-S",
                "-od",
                temp_dir,
                "-aet",
                self.settings.dicom_aet,
                "-aec",
                remote_aet,
                remote_host,
                str(remote_port),
                "-k",
                "QueryRetrieveLevel=STUDY",
                "-k",
                f"StudyInstanceUID={study_uid}",
            ]
            result = subprocess.run(command, capture_output=True, text=True)
            if result.returncode != 0:
                details = "\n".join(
                    part for part in (result.stdout.strip(), result.stderr.strip()) if part
                )
                raise RuntimeError(
                    f"getscu failed with exit code {result.returncode}: {details or 'no output'}"
                )

            files = sorted(path for path in Path(temp_dir).rglob("*") if path.is_file())
            if not files:
                raise RuntimeError("getscu completed without receiving any instances.")
            for instance_path in files:
                self.upload_instance_file(instance_path)
            return {"Imported": len(files)}

    def upload_instance_file(self, path: Path) -> Any:
        headers = {
            "Authorization": self._auth_header,
            "Accept": "application/json",
            "Content-Type": "application/dicom",
        }
        req = request.Request(
            self._url("/instances"),
            data=path.read_bytes(),
            headers=headers,
            method="POST",
        )
        try:
            with request.urlopen(req, timeout=self.timeout) as response:
                return self._decode_body(response, response.read())
        except error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise OrthancApiError("POST", "/instances", exc.code, body) from exc
        except error.URLError as exc:
            raise RuntimeError(f"POST /instances failed: {exc.reason}") from exc

    def delete_study(self, study_id: str) -> Any:
        return self.delete(f"/studies/{parse.quote(study_id, safe='')}")

    def delete_query(self, query_id: str) -> Any:
        return self.delete(f"/queries/{parse.quote(query_id, safe='')}")

    def list_local_study_map(self) -> dict[str, str]:
        try:
            items = self.get("/studies?expand")
            if isinstance(items, list) and all(isinstance(item, dict) for item in items):
                return build_study_uid_map(items)
        except Exception:
            pass
        study_ids = self.get("/studies")
        if not isinstance(study_ids, list):
            raise RuntimeError("Unexpected response from /studies.")
        result: dict[str, str] = {}
        for study_id in study_ids:
            study = self.get(f"/studies/{parse.quote(safe_text(study_id), safe='')}")
            if isinstance(study, dict):
                study_uid = pick_tag(study, "StudyInstanceUID")
                if study_uid:
                    result[study_uid] = safe_text(study.get("ID") or study_id)
        return result


def extract_resource_id(response: Any) -> str:
    if isinstance(response, dict):
        for key in ("ID", "Id", "id"):
            value = response.get(key)
            if value not in (None, ""):
                return safe_text(value)
        path = response.get("Path")
        if isinstance(path, str) and path.strip():
            return path.rstrip("/").split("/")[-1]
    if isinstance(response, str) and response.strip():
        return response.rstrip("/").split("/")[-1]
    raise RuntimeError(f"Could not extract resource id from response: {response!r}")


def build_study_uid_map(items: list[dict[str, Any]]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for item in items:
        study_uid = pick_tag(item, "StudyInstanceUID")
        study_id = safe_text(item.get("ID"))
        if study_uid and study_id:
            mapping[study_uid] = study_id
    return mapping


@dataclass
class LocalStudySummary:
    orthanc_id: str
    series_count: int | None
    instance_count: int | None


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
        pieces = [short_uid(self.study_uid)]
        if self.study_date:
            pieces.append(self.study_date)
        if self.patient_id:
            pieces.append(self.patient_id)
        return " | ".join(pieces)


class Dashboard:
    def __init__(self, app: "OrthancMirrorApp", use_rich: bool):
        self.app = app
        self.use_rich = use_rich and RICH_AVAILABLE and sys.stdout.isatty()
        self.events: deque[str] = deque(maxlen=12)
        self.live: Live | None = None

    def start(self) -> None:
        if self.use_rich:
            self.live = Live(self.render(), screen=True, refresh_per_second=4)
            self.live.__enter__()

    def stop(self) -> None:
        if self.live is not None:
            self.live.__exit__(None, None, None)
            self.live = None

    def log(self, message: str) -> None:
        line = f"{time.strftime('%H:%M:%S')} {message}"
        self.events.appendleft(line)
        if not self.use_rich:
            print(line)
        self.refresh()

    def refresh(self) -> None:
        if self.live is not None:
            self.live.update(self.render())

    def render(self) -> Any:
        if not self.use_rich:
            return ""

        summary = Table(box=box.SIMPLE_HEAD, expand=True)
        summary.add_column("Metric")
        summary.add_column("Value")
        summary.add_row("Phase", self.app.phase)
        summary.add_row("Remote modality", self.app.remote_modality or "-")
        summary.add_row("Orthanc", self.app.client.settings.base_url)
        summary.add_row("Repair mode", self.app.args.repair_mode)
        summary.add_row("Retrieve method", self.app.args.retrieve_method.upper())
        summary.add_row("Runtime", human_duration(time.time() - self.app.started_at))
        summary.add_row("Remote studies", str(len(self.app.studies)))
        summary.add_row("Summary matched", str(sum(1 for s in self.app.studies if s.summary_status == "matched")))
        summary.add_row(
            "Exact verified",
            str(sum(1 for s in self.app.studies if s.exact_status == "verified")),
        )
        summary.add_row(
            "Failed studies",
            str(sum(1 for s in self.app.studies if s.exact_status == "failed")),
        )
        summary.add_row("Extra local studies", str(len(self.app.extra_local_studies)))

        current = Table(box=box.SIMPLE_HEAD, expand=True)
        current.add_column("Current")
        current.add_column("Value")
        if self.app.current_study is None:
            current.add_row("Study", "-")
            current.add_row("Action", self.app.phase)
        else:
            study = self.app.current_study
            current.add_row("Study", study.label())
            current.add_row("Patient", study.patient_name or "-")
            current.add_row("Description", study.description or "-")
            current.add_row("Action", study.action)
            current.add_row(
                "Remote counts",
                f"{format_count(study.remote_series_count)} series / "
                f"{format_count(study.remote_instance_count)} instances",
            )
            current.add_row(
                "Local counts",
                f"{format_count(study.local_series_count)} series / "
                f"{format_count(study.local_instance_count)} instances",
            )
            current.add_row("Retries", str(study.retrieve_attempts))
            current.add_row("Error", study.error or "-")

        events = Table(box=box.SIMPLE_HEAD, expand=True)
        events.add_column("Recent events")
        if self.events:
            for line in self.events:
                events.add_row(line)
        else:
            events.add_row("No events yet.")

        return Group(
            Panel(summary, title="Orthanc Remote Mirror", border_style="cyan"),
            Panel(current, title="Current Study", border_style="green"),
            Panel(events, title="Event Log", border_style="yellow"),
        )


class OrthancMirrorApp:
    def __init__(self, args: argparse.Namespace, client: OrthancClient, remote_modality: str):
        self.args = args
        self.client = client
        self.remote_modality = remote_modality
        self.started_at = time.time()
        self.phase = "initializing"
        self.current_study: StudyState | None = None
        self.studies: list[StudyState] = []
        self.remote_query_id: str | None = None
        self.extra_local_studies: dict[str, str] = {}
        self.dashboard = Dashboard(self, use_rich=not args.no_rich)

    def run(self) -> int:
        self.dashboard.start()
        try:
            self.check_connectivity()
            self.load_remote_inventory()
            self.summary_sync()
            self.exact_sync()
            self.check_or_repair_extra_local_studies()
        finally:
            if self.remote_query_id:
                try:
                    self.client.delete_query(self.remote_query_id)
                except Exception:
                    pass
            self.dashboard.refresh()
            self.dashboard.stop()
        if STOP_REQUESTED:
            return 130
        failed = [study for study in self.studies if study.exact_status == "failed"]
        if failed:
            return 2
        if self.extra_local_studies:
            return 3
        return 0

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
        if self.args.limit_studies is not None:
            pairs = pairs[: self.args.limit_studies]
        for answer_id, content in pairs:
            study_uid = pick_tag(content, "StudyInstanceUID")
            if not study_uid:
                self.dashboard.log(
                    f"Skipping remote answer {answer_id} because StudyInstanceUID is missing"
                )
                continue
            state = StudyState(
                answer_id=answer_id,
                study_uid=study_uid,
                patient_name=safe_text(pick_tag(content, "PatientName")),
                patient_id=safe_text(pick_tag(content, "PatientID")),
                study_date=safe_text(pick_tag(content, "StudyDate")),
                description=safe_text(pick_tag(content, "StudyDescription")),
                remote_series_count=parse_count(pick_tag(content, "NumberOfStudyRelatedSeries")),
                remote_instance_count=parse_count(
                    pick_tag(content, "NumberOfStudyRelatedInstances")
                ),
            )
            if any(existing.study_uid == state.study_uid for existing in self.studies):
                self.dashboard.log(
                    f"Skipping duplicate remote StudyInstanceUID {short_uid(state.study_uid)}"
                )
                continue
            self.studies.append(state)
        if not self.studies and not self.args.allow_empty_remote:
            raise RuntimeError(
                f"Remote modality {self.remote_modality} returned zero studies."
            )
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
                    f"(remote {format_count(study.remote_series_count)}/"
                    f"{format_count(study.remote_instance_count)} vs "
                    f"local {format_count(study.local_series_count)}/"
                    f"{format_count(study.local_instance_count)})"
                )
                self.retrieve_until_summary_ok(study)
            except Exception as exc:
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
        if (
            study.remote_instance_count is not None
            and study.local_instance_count != study.remote_instance_count
        ):
            return False
        return True

    def retrieve_until_summary_ok(self, study: StudyState) -> None:
        while not STOP_REQUESTED:
            if study.retrieve_attempts > self.args.max_retries:
                study.exact_status = "failed"
                study.error = "Summary mismatch remained after retrieve retries."
                self.dashboard.log(f"Giving up on {study.label()}: {study.error}")
                return
            study.retrieve_attempts += 1
            study.action = "retrieve study"
            self.dashboard.log(
                f"Retrieving {study.label()} (attempt {study.retrieve_attempts})"
            )
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
                verified = self.ensure_exact_study(study)
                if verified:
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
            {
                "SeriesInstanceUID": "",
                "SOPInstanceUID": "",
            },
        )
        try:
            pairs = self.client.get_query_answer_pairs(query_id)
            manifest: dict[str, set[str]] = {}
            missing_series = False
            for _answer_id, content in pairs:
                series_uid = pick_tag(content, "SeriesInstanceUID")
                sop_uid = pick_tag(content, "SOPInstanceUID")
                if not sop_uid:
                    raise RuntimeError(
                        f"Remote instance query for {study.study_uid} returned no SOPInstanceUID."
                    )
                if not series_uid:
                    missing_series = True
                    break
                manifest.setdefault(series_uid, set()).add(sop_uid)
            if not missing_series:
                return manifest
            self.dashboard.log(
                f"Remote instance query omitted SeriesInstanceUID for {study.label()}, "
                "falling back to series walk"
            )
            return self.fetch_remote_manifest_via_series(study)
        finally:
            try:
                self.client.delete_query(query_id)
            except Exception:
                pass

    def fetch_remote_manifest_via_series(self, study: StudyState) -> dict[str, set[str]]:
        if self.remote_query_id is None:
            raise RuntimeError("Remote query was not created.")
        series_query_id = self.client.create_child_query(
            self.remote_query_id,
            study.answer_id,
            "series",
            {
                "SeriesInstanceUID": "",
            },
        )
        try:
            series_pairs = self.client.get_query_answer_pairs(series_query_id)
            manifest: dict[str, set[str]] = {}
            for series_answer_id, content in series_pairs:
                series_uid = pick_tag(content, "SeriesInstanceUID")
                if not series_uid:
                    raise RuntimeError(
                        f"Remote series query for {study.study_uid} returned no SeriesInstanceUID."
                    )
                instance_query_id = self.client.create_child_query(
                    series_query_id,
                    series_answer_id,
                    "instances",
                    {
                        "SOPInstanceUID": "",
                    },
                )
                try:
                    instance_pairs = self.client.get_query_answer_pairs(instance_query_id)
                    manifest[series_uid] = set()
                    for _answer_id, instance in instance_pairs:
                        sop_uid = pick_tag(instance, "SOPInstanceUID")
                        if not sop_uid:
                            raise RuntimeError(
                                f"Remote instance query for series {series_uid} returned no SOPInstanceUID."
                            )
                        manifest[series_uid].add(sop_uid)
                finally:
                    try:
                        self.client.delete_query(instance_query_id)
                    except Exception:
                        pass
            return manifest
        finally:
            try:
                self.client.delete_query(series_query_id)
            except Exception:
                pass

    def fetch_local_manifest(self, study: StudyState) -> dict[str, set[str]]:
        if study.local_id is None:
            return {}
        study.action = "local manifest read"
        series_items = self.client.get_study_series_expanded(study.local_id)
        instances_items = self.client.get_study_instances_expanded(study.local_id)
        series_uid_by_id: dict[str, str] = {}
        manifest: dict[str, set[str]] = {}
        for series in series_items:
            series_id = safe_text(series.get("ID"))
            series_uid = pick_tag(series, "SeriesInstanceUID")
            if series_id and series_uid:
                series_uid_by_id[series_id] = series_uid
                manifest.setdefault(series_uid, set())
        for instance in instances_items:
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
            self.dashboard.log(f"Deleting extra local study {short_uid(study_uid)}")
            self.client.delete_study(study_id)
            self.wait_after_change()
            self.extra_local_studies.pop(study_uid, None)
        self.dashboard.log("Extra local study cleanup completed")


def extract_modality_endpoint(value: Any) -> tuple[str, str, int]:
    if isinstance(value, dict):
        aet = safe_text(value.get("AET"))
        host = safe_text(value.get("Host"))
        port_value = value.get("Port")
    elif isinstance(value, (list, tuple)) and len(value) >= 3:
        aet = safe_text(value[0])
        host = safe_text(value[1])
        port_value = value[2]
    else:
        raise RuntimeError(f"Unsupported DicomModalities entry: {value!r}")

    if isinstance(port_value, int):
        port = port_value
    elif isinstance(port_value, str) and port_value.isdigit():
        port = int(port_value)
    else:
        raise RuntimeError(f"Invalid remote modality port: {port_value!r}")

    if not aet or not host:
        raise RuntimeError(f"Incomplete remote modality configuration: {value!r}")
    return aet, host, port


def compare_manifests(
    remote_manifest: dict[str, set[str]],
    local_manifest: dict[str, set[str]],
) -> ManifestDiff:
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


def load_settings(args: argparse.Namespace) -> OrthancSettings:
    orthanc_config, credentials_config = resolve_config_paths(
        args.config_dir,
        args.orthanc_config,
        args.credentials_config,
    )

    config = {}
    if orthanc_config.exists():
        config = parse_json_config(orthanc_config)
    elif not args.base_url:
        cli_error(f"Orthanc config file not found: {orthanc_config}")

    username = args.user
    password = args.password
    if (username is None or password is None) and credentials_config.exists():
        credentials = parse_json_config(credentials_config)
        default_username, default_password = config_first_registered_user(credentials)
        username = username or default_username
        password = password or default_password
    if username is None or password is None:
        cli_error(
            "Orthanc credentials were not provided. Use --user/--password or make credentials.json readable."
        )

    http_port = config.get("HttpPort", 8042) if isinstance(config, dict) else 8042
    base_url = args.base_url or config_build_base_url(config, default_port=int(http_port))
    dicom_aet = safe_text(config.get("DicomAet", "ORTHANC")) if isinstance(config, dict) else "ORTHANC"
    dicom_modalities = config.get("DicomModalities", {}) if isinstance(config, dict) else {}
    return OrthancSettings(
        base_url=base_url,
        username=username,
        password=password,
        dicom_aet=dicom_aet,
        dicom_modalities=dicom_modalities if isinstance(dicom_modalities, dict) else {},
    )


def choose_remote_modality(client: OrthancClient, requested: str | None) -> str:
    modalities = client.list_modalities()
    if requested:
        if requested not in modalities:
            cli_error(
                f"Remote modality {requested!r} is not configured in local Orthanc. "
                f"Available modalities: {', '.join(modalities) or '(none)'}"
            )
        return requested
    if not modalities:
        cli_error("No DicomModalities are configured in local Orthanc.")
    if not sys.stdin.isatty():
        cli_error("Use --remote when running non-interactively.")
    print("Configured remote modalities:")
    for index, modality in enumerate(modalities, start=1):
        print(f"  {index}. {modality}")
    while True:
        choice = input("Select a remote modality by number: ").strip()
        if not choice.isdigit():
            print("Please enter a number.", file=sys.stderr)
            continue
        selected = int(choice)
        if 1 <= selected <= len(modalities):
            return modalities[selected - 1]
        print("Choice out of range.", file=sys.stderr)


def confirm_destructive_mode(args: argparse.Namespace, remote_modality: str) -> None:
    if args.repair_mode != "replace" or args.yes:
        return
    if not sys.stdin.isatty():
        cli_error("--repair-mode replace requires --yes when stdin is not interactive.")
    prompt = (
        f"replace mode can delete local studies that do not exactly match remote modality "
        f"{remote_modality!r}. Continue? [y/N]: "
    )
    answer = input(prompt).strip().lower()
    if answer not in {"y", "yes"}:
        raise SystemExit(1)


def main() -> int:
    args = parse_args()
    settings = load_settings(args)
    client = OrthancClient(settings, timeout=args.timeout)
    remote_modality = choose_remote_modality(client, args.remote)
    confirm_destructive_mode(args, remote_modality)
    app = OrthancMirrorApp(args, client, remote_modality)
    return app.run()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except OrthancApiError as exc:
        print(f"Orthanc API error: {exc}", file=sys.stderr)
        raise SystemExit(1)
    except KeyboardInterrupt:
        raise SystemExit(130)
    except Exception as exc:
        print(f"Fatal error: {exc}", file=sys.stderr)
        raise SystemExit(1)
