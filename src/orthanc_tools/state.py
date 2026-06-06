from __future__ import annotations

import json
import os
import pwd
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

PRIVATE_DIR_MODE = 0o700
PRIVATE_FILE_MODE = 0o600


@dataclass(frozen=True)
class Ownership:
    uid: int
    gid: int


def preferred_home() -> Path:
    sudo_user = os.environ.get("SUDO_USER")
    if os.geteuid() == 0 and sudo_user and sudo_user != "root":
        try:
            return Path(pwd.getpwnam(sudo_user).pw_dir)
        except KeyError:
            pass
    return Path.home()


def default_owner() -> Ownership | None:
    sudo_user = os.environ.get("SUDO_USER")
    if os.geteuid() == 0 and sudo_user and sudo_user != "root":
        try:
            entry = pwd.getpwnam(sudo_user)
            return Ownership(uid=entry.pw_uid, gid=entry.pw_gid)
        except KeyError:
            return None
    return None


def maybe_chown(path: Path, owner: Ownership | None) -> None:
    if owner is None:
        return
    try:
        os.chown(path, owner.uid, owner.gid)
    except (FileNotFoundError, PermissionError, OSError):
        return


def ensure_directory(path: Path, owner: Ownership | None = None) -> None:
    path.mkdir(parents=True, exist_ok=True)
    try:
        path.chmod(PRIVATE_DIR_MODE)
    except OSError:
        pass
    maybe_chown(path, owner)


def ensure_dir(path: Path, owner: Ownership | None = None) -> None:
    ensure_directory(path, owner)


def atomic_write_text(path: Path, content: str, owner: Ownership | None = None) -> None:
    ensure_directory(path.parent, owner)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        tmp.chmod(PRIVATE_FILE_MODE)
    except OSError:
        pass
    os.replace(tmp, path)
    maybe_chown(path, owner)


def atomic_write_json(path: Path, payload: Any, owner: Ownership | None = None) -> None:
    ensure_directory(path.parent, owner)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=False)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    try:
        tmp.chmod(PRIVATE_FILE_MODE)
    except OSError:
        pass
    os.replace(tmp, path)
    maybe_chown(path, owner)


def append_text(path: Path, text: str, owner: Ownership | None = None) -> None:
    ensure_directory(path.parent, owner)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        path.chmod(PRIVATE_FILE_MODE)
    except OSError:
        pass
    maybe_chown(path, owner)


def load_json_file(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def load_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    return load_json_file(path)


def utc_now_iso() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def local_now_human() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %z")


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")
