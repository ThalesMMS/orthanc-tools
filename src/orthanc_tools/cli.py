from __future__ import annotations

import argparse
import runpy
import sys


COMMANDS: dict[str, str] = {
    "sync-remote": "orthanc_tools.workflows.sync_remote",
    "backfill-by-date": "orthanc_tools.workflows.backfill_by_date",
    "backup-remote-to-zip": "orthanc_tools.workflows.backup_remote_to_zip",
    "export-local-to-zip": "orthanc_tools.workflows.export_local_to_zip",
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m orthanc_tools",
        description="Unified CLI for Orthanc deploy and workflow tooling.",
    )
    parser.add_argument("command", choices=sorted(COMMANDS), help="Workflow to execute.")
    parser.add_argument("args", nargs=argparse.REMAINDER, help="Arguments forwarded to the workflow.")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    namespace = parser.parse_args(argv)
    forwarded = list(namespace.args)
    if forwarded and forwarded[0] == "--":
        forwarded = forwarded[1:]
    module_name = COMMANDS[namespace.command]
    old_argv = sys.argv[:]
    sys.argv = [namespace.command, *forwarded]
    try:
        runpy.run_module(module_name, run_name="__main__")
    except SystemExit as exc:
        code = exc.code
        if code is None:
            return 0
        if isinstance(code, int):
            return code
        raise
    finally:
        sys.argv = old_argv
    return 0
