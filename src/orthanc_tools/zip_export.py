from __future__ import annotations

import hashlib
import re
import unicodedata
import zipfile
from datetime import date
from pathlib import Path
from typing import Any

from .dicom import date_to_orthanc


ZIP_MANIFEST_NAME = "__backup__/manifest.json"
ZIP_REJECTED_PREFIX = "__backup__/rejected/"


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
    digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]
    head = max(16, limit - 13)
    return f"{text[:head]}_{digest}"


def build_patient_base(study: Any, day: date) -> str:
    patient = ascii_slug(getattr(study, "patient_name", "") or "UNKNOWN")
    birth = ascii_slug(getattr(study, "patient_birth_date", "") or "UNKNOWN")
    study_date = ascii_slug(
        normalize_study_day(getattr(study, "study_date", "") or "", day),
        fallback=date_to_orthanc(day),
    )
    return truncate_with_hash(f"{patient}_{birth}_{study_date}")


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


def read_zip_manifest(path: Path) -> dict[str, Any] | None:
    if not path.exists() or not zipfile.is_zipfile(path):
        return None
    with zipfile.ZipFile(path, "r") as archive:
        try:
            with archive.open(ZIP_MANIFEST_NAME) as handle:
                import json

                payload = json.load(handle)
        except KeyError:
            return None
    return payload if isinstance(payload, dict) else None
