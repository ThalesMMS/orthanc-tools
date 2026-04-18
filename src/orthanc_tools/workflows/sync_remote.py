#!/usr/bin/env python3
"""Mirror a remote DICOM modality into local Orthanc with a live terminal UI."""

from __future__ import annotations

import argparse
import shutil
import sys
import time
from collections import deque
from typing import Any

from orthanc_tools.dicom import parse_count, pick_tag, safe_text, short_uid
from orthanc_tools.orthanc_api import OrthancApiError
from orthanc_tools.workflows.client import OrthancClient
from orthanc_tools.workflows.primitives import (
    STOP_REQUESTED,
    cli_error,
    handle_signal,
    load_orthanc_settings,
    register_signal_handlers,
)
from orthanc_tools.workflows.retrieval import LocalStudySummary, ManifestDiff, MirrorWorkflowMixin, StudyState, compare_manifests

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
        "--getscu-timeout-seconds",
        type=float,
        default=60.0,
        help="Timeout for each getscu call when --retrieve-method=get. Default: 60",
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


def format_count(value: int | None) -> str:
    return "?" if value is None else str(value)


def human_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h{minutes:02d}m{secs:02d}s"
    if minutes:
        return f"{minutes}m{secs:02d}s"
    return f"{secs}s"
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


class OrthancMirrorApp(MirrorWorkflowMixin):
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
    register_signal_handlers(handle_signal)
    args = parse_args()
    settings = load_orthanc_settings(args)
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
