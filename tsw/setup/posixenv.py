"""Expose the checkout's recorded interpreter through ~/.local/bin/tsw (DN009 §12).

Keep the generated launcher in the private state directory. Only our symlink goes in
shared ~/.local/bin: never replace another installation or remove the user's directory.
"""

from __future__ import annotations

import os
import shlex
from collections.abc import Mapping
from pathlib import Path

from tsw.errors import rejected
from tsw.host.dashboard_service import state_dir
from tsw.storage.fs import atomic_write


def launcher(env: Mapping[str, str]) -> Path:
    return state_dir(env).absolute() / "bin" / "tsw"


def script(python: str, checkout: Path) -> bytes:
    command = shlex.join([python, "-X", "utf8", str(checkout / "plugin/bin/tsw_launch.py")])
    return f'#!/bin/sh\nexec {command} "$@"\n'.encode()


def _ours(link: Path, target: Path) -> bool:
    return link.is_symlink() and link.readlink() == target


def shim_state(link: Path, target: Path, text: bytes) -> str:
    if not _ours(link, target):
        return "conflict" if link.exists() or link.is_symlink() else "missing"
    if not target.is_file():
        return "missing"
    if not os.access(target, os.X_OK) or target.read_bytes() != text:
        return "differs"
    return "ok"


def write_shim(link: Path, target: Path, text: bytes) -> None:
    link.parent.mkdir(parents=True, exist_ok=True)
    if not _ours(link, target):
        try:
            link.symlink_to(target)
        except FileExistsError as exc:
            raise rejected(f"{link} 已被占用，不替换已有命令", reason="shim_conflict") from exc
    atomic_write(target, text)
    target.chmod(0o755)


def remove_shim(link: Path, target: Path) -> dict[str, list[str]]:
    removed: list[str] = []
    if _ours(link, target):
        link.unlink()
        removed.append(link.name)
    target.unlink(missing_ok=True)
    return {"removed": removed, "deferred": []}


def on_path(directory: Path, env: Mapping[str, str]) -> bool:
    return directory in (Path(p) for p in env.get("PATH", "").split(os.pathsep) if p)
