"""``tsw pi guard <Event>``: pi extension events through the same guard as Claude Code's hooks
(PI001 §4.2, §6.1).

The extension sends what pi gave it (the tool's name and input, the prompt, the directory); this
module turns it into the hook payload ``tsw.host.guard`` reads, with pi's tools under the names the
block list knows, and turns the guard's answer into what the extension does: block the tool call
with the guard's reason, add context, or start one more turn. The rules stay in the guard.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, cast

from tsw.home import Home
from tsw.host import guard
from tsw.host.identity import session_of

EVENTS = ("SessionStart", "PreToolUse", "UserPromptSubmit", "Stop")
# pi tool -> (the name the guard knows, pi's argument -> the guard's)
TOOLS: dict[str, tuple[str, dict[str, str]]] = {
    "write": ("Write", {"path": "file_path"}),
    "edit": ("Edit", {"path": "file_path"}),
    "bash": ("Bash", {}),
    # Read-only tools reach the guard for the Dashboard token folder (CC-B12, review F2).
    "read": ("Read", {}),
    "grep": ("Grep", {}),
    "find": ("Glob", {}),
    "ls": ("LS", {}),
}


def hook_payload(event: str, data: Mapping[str, Any], env: Mapping[str, str]) -> dict[str, Any]:
    out: dict[str, Any] = {
        "hook_event_name": event,
        "session_id": session_of(env),
        "cwd": str(data.get("cwd") or "."),
    }
    if event == "PreToolUse":
        tool = str(data.get("tool") or "")
        raw: Any = data.get("input")
        given = cast("dict[str, Any]", raw) if isinstance(raw, dict) else {}
        name, renames = TOOLS.get(tool, (tool, {}))
        out["tool_name"] = name
        out["tool_input"] = {renames.get(k, k): v for k, v in given.items()}
    if event == "UserPromptSubmit":
        out["prompt"] = str(data.get("prompt") or "")
    return out


def outcome(event: str, output: Mapping[str, Any] | None) -> dict[str, Any]:
    """The guard's answer as the extension acts on it."""
    if not output:
        return {}
    if output.get("decision") == "block":  # Stop: keep going with the reason
        return {"followUp": str(output.get("reason") or "")}
    raw: Any = output.get("hookSpecificOutput")
    specific = cast("dict[str, Any]", raw) if isinstance(raw, dict) else {}
    if specific.get("permissionDecision") == "deny":
        return {"block": True, "reason": str(specific.get("permissionDecisionReason") or "")}
    if specific.get("additionalContext"):
        return {"context": str(specific["additionalContext"])}
    del event
    return {}


def run(event: str, home: Home, env: Mapping[str, str], stdin: str) -> dict[str, Any]:
    if event not in EVENTS:
        return {}
    try:
        raw: Any = json.loads(stdin) if stdin.strip() else {}
    except json.JSONDecodeError:
        raw = {}
    data = cast("dict[str, Any]", raw) if isinstance(raw, dict) else {}
    text = guard.run(event, home, env, json.dumps(hook_payload(event, data, env)))
    return outcome(event, json.loads(text) if text else None)
