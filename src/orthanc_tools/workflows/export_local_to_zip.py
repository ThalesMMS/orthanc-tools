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
import sys
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from orthanc_tools.dicom import parse_iso_date
from orthanc_tools.orthanc_api import OrthancApiError
from orthanc_tools.state import Ownership, append_text, default_owner, ensure_dir, load_json, now_iso, preferred_home
from orthanc_tools.workflows.client import OrthancClient
from orthanc_tools.workflows.primitives import (
    cli_error,
    load_orthanc_settings,
)
from orthanc_tools.workflows.state_manager import ExportRunState
from orthanc_tools.zip_export import ExportWorkflowMixin

DEFAULT_TIMEOUT = 120.0
DEFAULT_PAGE_SIZE = 200
DEFAULT_RETRIES = 3
DEFAULT_RETRY_DELAY = 5.0
DOWNLOAD_CHUNK_SIZE = 1024 * 1024


class Logger:
    def __init__(self, run_log: Path, error_log: Path, owner: Ownership | None):
        self.run_log = run_log
        self.error_log = error_log
        self.owner = owner
        ensure_dir(run_log.parent, owner)
        ensure_dir(error_log.parent, owner)

    def _append(self, path: Path, line: str) -> None:
        append_text(path, line + "\n", self.owner)

    def info(self, message: str) -> None:
        line = f"[{now_iso()}] INFO  {message}"
        print(line)
        self._append(self.run_log, line)

    def error(self, message: str) -> None:
        line = f"[{now_iso()}] ERROR {message}"
        print(line, file=sys.stderr)
        self._append(self.run_log, line)
        self._append(self.error_log, line)


class ExportApp(ExportWorkflowMixin):
    def __init__(self, args: argparse.Namespace, client: OrthancClient, owner: Ownership | None):
        self.args = args
        self.client = client
        self.owner = owner
        self.backup_dir = args.backup_dir
        self.state_dir = args.state_dir
        self.logs_dir = self.state_dir / "logs"
        self.days_state_dir = self.state_dir / "days"
        self.logger = Logger(self.logs_dir / "run.log", self.logs_dir / "errors.log", owner)
        self.run_state = ExportRunState(
            self.state_dir,
            owner,
            backup_dir=self.backup_dir,
            start_date=args.start_date,
            end_date=args.end_date,
            name_mode=args.name,
            zip_mode=args.zip_mode,
            orthanc_base_url=self.client.settings.base_url,
        )

    def run(self) -> int:
        ensure_dir(self.backup_dir, self.owner)
        ensure_dir(self.days_state_dir, self.owner)

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
        ensure_dir(day_dir, self.owner)
        ensure_dir(day_state_dir, self.owner)

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
    settings = load_orthanc_settings(args)
    client = OrthancClient(settings)
    app = ExportApp(args, client, owner)
    return app.run()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        raise SystemExit(130)
