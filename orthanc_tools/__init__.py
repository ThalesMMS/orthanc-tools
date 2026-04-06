from __future__ import annotations

from pathlib import Path


_ROOT = Path(__file__).resolve().parent
_SRC_PACKAGE = _ROOT.parent / "src" / "orthanc_tools"

__path__ = [str(_ROOT), str(_SRC_PACKAGE)]
__all__ = []
