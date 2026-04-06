from __future__ import annotations

import argparse
import datetime as dt
import re
import subprocess
from pathlib import Path
from typing import Any


TAG_KEYS = {
    "PatientBirthDate": "0010,0030",
    "PatientID": "0010,0020",
    "PatientName": "0010,0010",
    "SeriesDescription": "0008,103e",
    "SeriesInstanceUID": "0020,000e",
    "SOPClassUID": "0008,0016",
    "SOPInstanceUID": "0008,0018",
    "StudyDate": "0008,0020",
    "StudyDescription": "0008,1030",
    "StudyInstanceUID": "0020,000d",
    "AccessionNumber": "0008,0050",
    "Modality": "0008,0060",
    "InstanceNumber": "0020,0013",
}


DCMDUMP_TAG_MAP = {
    "0008,0016": "sop_class_uid",
    "0008,0018": "sop_uid",
    "0020,000d": "study_uid",
    "0020,000e": "series_uid",
}


def parse_iso_date(value: str) -> dt.date:
    try:
        return dt.date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"Invalid ISO date: {value!r}. Expected YYYY-MM-DD.") from exc


def compute_default_end_date() -> dt.date:
    return dt.date.today() - dt.timedelta(days=1)


def iso_to_dicom_date(value: dt.date) -> str:
    return value.strftime("%Y%m%d")


def date_to_orthanc(day: dt.date) -> str:
    return day.strftime("%Y%m%d")


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


def pick_tag(item: Any, tag_name: str) -> str | None:
    if isinstance(item, dict):
        direct = unwrap_tag_value(item.get(tag_name))
        if direct not in (None, ""):
            return direct
        for key in ("RequestedTags", "MainDicomTags", "PatientMainDicomTags"):
            child = item.get(key)
            if isinstance(child, dict):
                nested = unwrap_tag_value(child.get(tag_name))
                if nested not in (None, ""):
                    return nested
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


def sanitize_tsv(value: str) -> str:
    return value.replace("\t", " ").replace("\r", " ").replace("\n", " ").strip()


def nullable_int(value: int | None) -> int | str:
    return "" if value is None else value


def short_uid(uid: str | None, length: int = 32) -> str:
    if not uid:
        return "-"
    if len(uid) <= length:
        return uid
    head = max(10, length - 10)
    return f"{uid[:head]}...{uid[-7:]}"


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


def format_duration(seconds: float) -> str:
    return human_duration(seconds)


def unique_instance_records(records: list[dict[str, str]]) -> list[dict[str, str]]:
    seen: set[tuple[str, str, str]] = set()
    unique: list[dict[str, str]] = []
    for record in records:
        key = (
            safe_text(record.get("study_uid")),
            safe_text(record.get("series_uid")),
            safe_text(record.get("sop_uid")),
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(record)
    return unique


def extract_dicom_ids(path: Path) -> dict[str, str]:
    try:
        result = subprocess.run(
            [
                "dcmdump",
                "+P",
                "(0008,0016)",
                "+P",
                "(0008,0018)",
                "+P",
                "(0020,000d)",
                "+P",
                "(0020,000e)",
                str(path),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
    except Exception:
        return {}
    if result.returncode != 0:
        return {}
    pattern = re.compile(r"\(([0-9A-Fa-f]{4}),([0-9A-Fa-f]{4})\)\s+\w+\s+\[(.*)\]")
    extracted: dict[str, str] = {}
    for line in result.stdout.splitlines():
        match = pattern.search(line)
        if not match:
            continue
        tag = f"{match.group(1).lower()},{match.group(2).lower()}"
        field = DCMDUMP_TAG_MAP.get(tag)
        value = match.group(3).strip()
        if field and value:
            extracted[field] = value
    return extracted
