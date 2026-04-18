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
import json
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Iterable

from orthanc_tools.dicom import (
    compute_default_end_date,
    extract_dicom_ids,
    iso_to_dicom_date,
    parse_count,
    parse_iso_date,
    pick_tag,
    safe_text,
    short_uid,
    unique_instance_records,
)
from orthanc_tools.orthanc_api import OrthancApiError
from orthanc_tools.state import (
    Ownership,
    atomic_write_json,
    default_owner,
    ensure_dir,
    preferred_home,
    utc_now_iso,
)
from orthanc_tools.zip_export import (
    BackupZipMixin,
    ZIP_MANIFEST_NAME,
    ZIP_REJECTED_PREFIX,
    format_duration,
    format_size,
    read_zip_manifest,
    validate_zip_file,
)
from orthanc_tools.workflows.client import OrthancClient
from orthanc_tools.workflows.primitives import (
    STOP_REQUESTED,
    cli_error,
    handle_signal,
    load_orthanc_settings,
    register_signal_handlers,
)
from orthanc_tools.workflows.retrieval import (
    BACKUP_MATERIAL_STUDY_STATE_EXTRA_KEYS,
    ImportOutcome,
    RemoteStudy,
    RemoteStudyWorkflowMixin,
    RetrievalPlan,
    material_study_state,
)
from orthanc_tools.workflows.state_manager import StateManager


class BackupZipApp(BackupZipMixin, RemoteStudyWorkflowMixin):
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

    def _day_query_fields(self, day: dt.date) -> dict[str, str]:
        return {
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

    def _manifest_query_fields(self) -> dict[str, str]:
        return {
            "PatientID": "",
            "PatientName": "",
            "PatientBirthDate": "",
            "StudyDate": "",
            "NumberOfStudyRelatedSeries": "",
            "NumberOfStudyRelatedInstances": "",
        }

    def _temp_dir_prefix(self) -> str:
        return "orthanc-backup-"

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

    def process_study(self, day: dt.date, study: RemoteStudy, day_status: dict[str, Any]) -> bool:
        study_states = day_status.setdefault("studies", {})
        if not isinstance(study_states, dict):
            raise RuntimeError("Invalid studies state")
        s = study_states.setdefault(study.study_uid, {})
        if not isinstance(s, dict):
            s = {}
            study_states[study.study_uid] = s

        previous_snapshot = json.dumps(
            material_study_state(s, extra_keys=BACKUP_MATERIAL_STUDY_STATE_EXTRA_KEYS),
            sort_keys=True,
            default=str,
        )
        s["last_checked_at"] = utc_now_iso()
        s.setdefault("rejected_instances", {})
        if not isinstance(s["rejected_instances"], dict):
            s["rejected_instances"] = {}
        s.setdefault("instance_failures", {})
        if not isinstance(s["instance_failures"], dict):
            s["instance_failures"] = {}
        s["last_error"] = ""

        if self.maybe_complete_from_existing_zip(day, study, s):
            current_snapshot = json.dumps(
                material_study_state(s, extra_keys=BACKUP_MATERIAL_STUDY_STATE_EXTRA_KEYS),
                sort_keys=True,
                default=str,
            )
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

        current_snapshot = json.dumps(
            material_study_state(s, extra_keys=BACKUP_MATERIAL_STUDY_STATE_EXTRA_KEYS),
            sort_keys=True,
            default=str,
        )
        return previous_snapshot != current_snapshot

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
    register_signal_handlers(handle_signal)
    return app.run()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except OrthancApiError as exc:
        print(f"Orthanc API error: {exc}", file=sys.stderr)
        raise SystemExit(1)
    except KeyboardInterrupt:
        raise SystemExit(130)
