from __future__ import annotations

import json
from pathlib import Path
from typing import Any


DEFAULT_CONFIG_DIR = Path("/etc/orthanc")


def read_json_file(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def resolve_config_paths(
    config_dir: str | Path = DEFAULT_CONFIG_DIR,
    orthanc_config: str | Path | None = None,
    credentials_config: str | Path | None = None,
) -> tuple[Path, Path]:
    config_root = Path(config_dir)
    main_config = Path(orthanc_config) if orthanc_config else config_root / "orthanc.json"
    credentials = Path(credentials_config) if credentials_config else config_root / "credentials.json"
    return main_config, credentials


def first_registered_user(credentials: dict[str, Any]) -> tuple[str, str]:
    users = credentials.get("RegisteredUsers")
    if not isinstance(users, dict) or not users:
        raise ValueError("RegisteredUsers is missing or empty in credentials.json.")
    username = next(iter(users))
    password = users[username]
    if not isinstance(password, str):
        raise ValueError(f"Password for Orthanc user {username!r} is not a string.")
    return username, password


def resolve_http_port(config: dict[str, Any], default: int = 8042) -> int:
    value = config.get("HttpPort", default)
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return default


def build_base_url(config: dict[str, Any], host: str = "127.0.0.1", default_port: int = 8042) -> str:
    return f"http://{host}:{resolve_http_port(config, default=default_port)}"
