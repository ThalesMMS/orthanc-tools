from __future__ import annotations

import argparse
import signal
import sys
from dataclasses import dataclass
from typing import Any, Callable

from orthanc_tools.config import build_base_url, first_registered_user, read_json_file, resolve_config_paths
from orthanc_tools.dicom import parse_count, safe_text


@dataclass
class OrthancSettings:
    base_url: str
    username: str
    password: str
    dicom_aet: str | None = None
    timeout: float = 60.0
    getscu_timeout: float = 60.0
    dicom_modalities: dict[str, Any] | None = None


class StopRequestedFlag:
    def __init__(self) -> None:
        self.value = False

    def __bool__(self) -> bool:
        return self.value

    def set(self, value: bool) -> None:
        self.value = value


STOP_REQUESTED = StopRequestedFlag()


def cli_error(message: str) -> None:
    print(f"ERROR: {message}", file=sys.stderr)
    raise SystemExit(1)


def handle_signal(signum: int, _frame: Any) -> None:
    STOP_REQUESTED.set(True)
    name = signal.Signals(signum).name
    print(f"\nSignal received: {name}. Finishing the current operation.", file=sys.stderr)


def register_signal_handlers(handler: Callable[[int, Any], None] = handle_signal) -> None:
    signal.signal(signal.SIGINT, handler)
    signal.signal(signal.SIGTERM, handler)
    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, handler)


def _normalize_resource_id(value: Any) -> str | None:
    if value in (None, ""):
        return None
    text = str(value).strip()
    if not text:
        return None
    return text.rstrip("/").rsplit("/", 1)[-1]


def extract_resource_id(response: Any) -> str:
    if isinstance(response, dict):
        for key in ("ID", "Id", "id", "Path", "Uri", "URI", "URL"):
            resource_id = _normalize_resource_id(response.get(key))
            if resource_id is not None:
                return resource_id
    resource_id = _normalize_resource_id(response) if isinstance(response, str) else None
    if resource_id is not None:
        return resource_id
    raise RuntimeError(f"Could not extract resource ID from response: {response!r}")


def load_orthanc_settings(args: argparse.Namespace) -> OrthancSettings:
    orthanc_config_path, credentials_config_path = resolve_config_paths(
        args.config_dir,
        args.orthanc_config,
        args.credentials_config,
    )

    config: dict[str, Any] = {}
    if orthanc_config_path.exists():
        payload = read_json_file(orthanc_config_path)
        if not isinstance(payload, dict):
            cli_error(f"Invalid JSON object in {orthanc_config_path}")
        config = payload
    else:
        requires_calling_aet = hasattr(args, "calling_aet")
        has_base_url = bool(getattr(args, "base_url", None))
        has_calling_aet = bool(getattr(args, "calling_aet", None))
        if not has_base_url or (requires_calling_aet and not has_calling_aet):
            if requires_calling_aet:
                cli_error(
                    f"Orthanc config file not found: {orthanc_config_path}. "
                    "Provide --base-url and --calling-aet explicitly, or make orthanc.json readable."
                )
            cli_error(f"Orthanc config file not found: {orthanc_config_path}")

    username = getattr(args, "user", None)
    password = getattr(args, "password", None)
    if (username is None or password is None) and credentials_config_path.exists():
        credentials = read_json_file(credentials_config_path)
        if not isinstance(credentials, dict):
            cli_error(f"Invalid JSON object in {credentials_config_path}")
        default_username, default_password = first_registered_user(credentials)
        username = username or default_username
        password = password or default_password

    if username is None or password is None:
        cli_error("Orthanc credentials not provided. Use --user/--password or make credentials.json readable.")

    http_port = parse_count(config.get("HttpPort")) or 8042
    base_url = getattr(args, "base_url", None) or build_base_url(config, default_port=http_port)
    dicom_aet = safe_text(config.get("DicomAet")) or "ORTHANC"
    if hasattr(args, "calling_aet"):
        dicom_aet = getattr(args, "calling_aet", None) or dicom_aet
        args.calling_aet = dicom_aet

    timeout = float(getattr(args, "timeout", 60.0))
    getscu_timeout_value = getattr(args, "getscu_timeout", None)
    if getscu_timeout_value is None:
        getscu_timeout_value = getattr(args, "getscu_timeout_seconds", 60.0)
    getscu_timeout = float(getscu_timeout_value)
    dicom_modalities = config.get("DicomModalities")
    if not isinstance(dicom_modalities, dict):
        dicom_modalities = {}

    return OrthancSettings(
        base_url=base_url,
        username=username,
        password=password,
        dicom_aet=dicom_aet,
        timeout=timeout,
        getscu_timeout=getscu_timeout,
        dicom_modalities=dicom_modalities,
    )
