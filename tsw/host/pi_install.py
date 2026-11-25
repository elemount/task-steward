"""Installing the TaskSteward pi package (PI001 §8): ``pi install <checkout>/pi``.

A local package is loaded from the checkout in place, so there is no copy to keep current
(Claude Code's plugin cache has one). Whether it is installed is read from pi's own user
settings (``packages`` in ``<agent dir>/settings.json``); installing and removing go through
pi's command line, which owns that file.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

PI_TIMEOUT_SECONDS = 120


def package(checkout: Path) -> Path:
    return (checkout / "pi").resolve()


def agent_dir(env: Mapping[str, str]) -> Path:
    explicit = env.get("PI_CODING_AGENT_DIR")
    if explicit:
        return Path(explicit).expanduser()
    return Path(env.get("HOME") or Path.home()) / ".pi" / "agent"


def find_pi(env: Mapping[str, str]) -> str | None:
    explicit = env.get("TSW_PI")
    if explicit:
        return explicit if Path(explicit).is_file() else None
    return shutil.which("pi", path=env.get("PATH"))


def _declared(entry: Any) -> str | None:
    if isinstance(entry, str):
        return entry
    if isinstance(entry, dict):
        source: Any = cast("dict[str, Any]", entry).get("source")
        return source if isinstance(source, str) else None
    return None


def installed(env: Mapping[str, str], checkout: Path) -> bool:
    """Whether pi's user settings declare this checkout's package."""
    settings = agent_dir(env) / "settings.json"
    try:
        data: Any = json.loads(settings.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    entries: Any = cast("dict[str, Any]", data).get("packages") if isinstance(data, dict) else None
    wanted = os.path.normpath(str(package(checkout)))
    for entry in cast("list[Any]", entries) if isinstance(entries, list) else []:
        declared = _declared(entry)
        if declared is None:
            continue
        path = Path(declared).expanduser()
        if not path.is_absolute():
            path = settings.parent / path
        if os.path.normpath(str(path.resolve())) == wanted:
            return True
    return False


def run_pi(pi: str, env: Mapping[str, str], *args: str) -> dict[str, Any]:
    command = "pi " + " ".join(args)
    try:
        proc = subprocess.run(
            [pi, *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=dict(env),
            stdin=subprocess.DEVNULL,
            timeout=PI_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"command": command, "returncode": None, "error": str(exc)}
    lines = [line for line in f"{proc.stdout}\n{proc.stderr}".splitlines() if line.strip()]
    return {"command": command, "returncode": proc.returncode, "output": lines[-10:]}


def install(env: Mapping[str, str], pi: str, checkout: Path) -> dict[str, Any]:
    ran = run_pi(pi, env, "install", str(package(checkout)))
    return {"ok": installed(env, checkout), "ran": ran}


def remove(env: Mapping[str, str], pi: str, checkout: Path) -> dict[str, Any]:
    ran = run_pi(pi, env, "remove", str(package(checkout)))
    return {"ok": not installed(env, checkout), "ran": ran}
