#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT_DIR / "src"))

from orthanc_tools.cli import main


if __name__ == "__main__":
    raise SystemExit(main(["backup-remote-to-zip", *sys.argv[1:]]))
