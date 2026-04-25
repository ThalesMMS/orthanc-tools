from __future__ import annotations

import argparse
import runpy
import sys
import types


COMMANDS: dict[str, dict[str, str]] = {
    "sync-remote": {
        "module": "orthanc_tools.workflows.sync_remote",
        "description": "Mirror a remote PACS into local Orthanc. Use for continuous sync and drift repair.",
    },
    "backfill-by-date": {
        "module": "orthanc_tools.workflows.backfill_by_date",
        "description": "Populate local Orthanc day by day from a remote PACS. Use for one-shot operational backfill.",
    },
    "backup-remote-to-zip": {
        "module": "orthanc_tools.workflows.backup_remote_to_zip",
        "description": "Export remote PACS studies directly to ZIP files. Use for remote backup to disk.",
    },
    "export-local-to-zip": {
        "module": "orthanc_tools.workflows.export_local_to_zip",
        "description": "Export studies already in local Orthanc to ZIP files. Use for an already-local archive.",
    },
}


def build_parser() -> argparse.ArgumentParser:
    workflows = "\n".join(
        f"  {command:<22} {details['description']}"
        for command, details in sorted(COMMANDS.items())
    )
    parser = argparse.ArgumentParser(
        prog="orthanc-tools",
        description="Unified CLI for Orthanc deploy and workflow tooling.",
        epilog=f"Workflows:\n{workflows}",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "command",
        choices=sorted(COMMANDS),
        metavar="COMMAND",
        help="Workflow to execute. See workflows below.",
    )
    parser.add_argument(
        "args",
        nargs=argparse.REMAINDER,
        metavar="ARGS",
        help="Arguments forwarded to the workflow.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    namespace = parser.parse_args(argv)
    forwarded = list(namespace.args)
    if forwarded and forwarded[0] == "--":
        forwarded = forwarded[1:]
    module_name = COMMANDS[namespace.command]["module"]
    old_argv = sys.argv[:]
    main_module = sys.modules.get("__main__")
    old_main_spec = (
        main_module.__spec__
        if isinstance(main_module, types.ModuleType)
        else None
    )
    sys.argv = [f"orthanc-tools {namespace.command}", *forwarded]
    if isinstance(main_module, types.ModuleType):
        main_module.__spec__ = None
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
        if isinstance(main_module, types.ModuleType):
            main_module.__spec__ = old_main_spec
    return 0
