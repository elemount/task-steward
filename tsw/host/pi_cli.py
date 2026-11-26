"""The ``tsw pi`` commands (PI001): what the pi extension runs, and ``tsw pi chat`` for the user.

The extension runs every command but ``chat`` with ``TSW_SESSION_ID`` set, so each resolves its
caller from the session as the MCP server and the hooks do (DN001 §7.1); none takes a role on the
command line. ``chat`` is the user's own, in a terminal outside any session (PI001 §5.4).
"""

from __future__ import annotations

import sys
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from tsw import direct, lease
from tsw.config import load_config
from tsw.errors import rejected
from tsw.events import utc_now
from tsw.home import Home, workdir_path
from tsw.host import pi_guard, pi_sessions
from tsw.host.identity import Identity, resolve, session_of, within
from tsw.ledger import Ledger
from tsw.machine.ask_user import handoff_due
from tsw.machine.model import row_dict

CHAT_POLL_SECONDS = 0.5
# How the Claude Code wording of the instructions maps to pi, added to every TaskSteward
# session's system prompt by the extension (PI001 §4).
NOTES = Path(__file__).resolve().parent / "pi_notes.md"


def identity(home: Home, env: Mapping[str, str], cwd: Path) -> Identity:
    with Ledger(home).view() as conn:
        return resolve(conn, home, env, cwd)


def whoami(home: Home, env: Mapping[str, str], cwd: Path) -> dict[str, Any]:
    """What the extension needs at session start: the role, and how to run the MCP server."""
    who = identity(home, env, cwd)
    launcher = pi_sessions.REPO_ROOT / "plugin" / "bin" / "tsw_launch.py"
    return {
        **who.to_dict(),
        "home": str(home.root),
        "notes": NOTES.read_text(encoding="utf-8") if who.role != "none" or who.pending else "",
        "mcp": {
            "command": sys.executable,
            "args": ["-X", "utf8", "-u", str(launcher), "mcp"],
            "env": {"TSW_HOME": str(home.root)},
        },
    }


def _orchestrator(home: Home, env: Mapping[str, str], cwd: Path, what: str) -> Identity:
    who = identity(home, env, cwd)
    if who.role != "orchestrator":
        raise rejected(f"只有主会话能{what}", reason="role_denied", role=who.role)
    return who


def start(
    home: Home,
    env: Mapping[str, str],
    cwd: Path,
    *,
    name: str,
    folder: Path,
    prompt: str,
    title: str | None,
) -> dict[str, Any]:
    """Create the worker session ``attempt_start`` asked for: pi's ``start_session``, in the
    folder it returned (the task's worktree, or the execution directory)."""
    _orchestrator(home, env, cwd, "创建 worker 会话")
    pi_sessions.check_name(name)
    with Ledger(home).view() as conn:
        row = row_dict(
            conn.execute(
                "SELECT * FROM attempts WHERE session_name = ? ORDER BY rowid DESC LIMIT 1",
                (name,),
            ).fetchone()
        )
    # Recorded before created, as the start_session rule on Claude Code: a session no attempt
    # asked for is refused, and so is one whose attempt is past starting.
    if row is None or row["phase"] != "starting":
        raise rejected(
            "先用 attempt_start 落账，再按它返回的 session_name 创建会话",
            reason="not_recorded",
            session_name=name,
        )
    roots = [home.root / "work" / str(row["task_id"]) / str(row["attempt"])]
    if row["workdir"]:
        roots.append(workdir_path(home, str(row["workdir"])))
    if not any(within(folder, root) for root in roots):
        raise rejected(
            "会话目录须是 attempt_start 返回的执行目录或其中的 worktree",
            reason="workdir_mismatch",
            cwd=str(folder),
        )
    config = load_config(home)
    return pi_sessions.request(
        home,
        {
            "op": "start",
            "name": name,
            "cwd": str(folder.resolve()),
            "prompt": prompt,
            "title": title or name,
            "model": config.get("roles.worker.model"),
            "thinking": config.get("roles.worker.effort"),
        },
    )


def send(home: Home, env: Mapping[str, str], *, target: str, message: str) -> dict[str, Any]:
    sender = session_of(env)
    if not sender:
        raise rejected("需要在会话中运行（TSW_SESSION_ID）", reason="role_denied")
    return pi_sessions.send(home, sender, target, message)


def stop(home: Home, env: Mapping[str, str], cwd: Path, *, name: str) -> dict[str, Any]:
    _orchestrator(home, env, cwd, "停止 worker 会话")
    return pi_sessions.request(home, {"op": "stop", "name": name}, start=False)


def poll(home: Home, env: Mapping[str, str], cwd: Path) -> dict[str, Any]:
    """The main session's wake-ups (PI001 §5.3): its mailbox, and the questions past their
    handoff time when it holds the lease (the routine session's job on Claude Code, §5.4)."""
    who = identity(home, env, cwd)
    if who.session_id is None:
        return {"messages": [], "handoff_due": []}
    messages = pi_sessions.take(home, who.session_id)
    due: list[str] = []
    if who.role == "orchestrator":
        with Ledger(home).view() as conn:
            if lease.orchestrator_session(conn) == who.session_id:
                due = [str(r["request_id"]) for r in handoff_due(conn, utc_now())]
    return {"messages": messages, "handoff_due": due}


def guard(event: str, home: Home, env: Mapping[str, str], stdin: str) -> dict[str, Any]:
    return pi_guard.run(event, home, env, stdin)


# ---------------------------------------------------------------------- chat


def chat_target(home: Home, target: str) -> str:
    """A pi session name from a session name, a ``pi:`` reference or a task ID."""
    name = pi_sessions.name_of(target) or target
    if pi_sessions.read_state(home, name) is not None:
        return name
    with Ledger(home).view() as conn:
        row = row_dict(
            conn.execute(
                "SELECT session_ref FROM attempts WHERE task_id = ? AND session_ref LIKE 'pi:%' "
                "ORDER BY rowid DESC LIMIT 1",
                (target.upper(),),
            ).fetchone()
        )
    found = pi_sessions.name_of(str(row["session_ref"])) if row else None
    if found is None:
        raise rejected(f"找不到 {target} 的 pi 会话", reason="unknown_session", target=target)
    return found


def chat(
    home: Home,
    env: Mapping[str, str],
    target: str,
    *,
    read: Callable[[str], str] = input,
    write: Callable[[str], None] = print,
) -> dict[str, Any]:
    """The user talks to a worker directly (PI001 §5.4): its replies are shown as they finish."""
    direct.require_outside_session(env, "与 worker 直接对话（tsw pi chat）")
    name = chat_target(home, target)
    seen = 0

    def show(wait: bool) -> None:
        nonlocal seen
        idle_polls = 0
        while True:
            got = pi_sessions.request(home, {"op": "tail", "name": name, "after": seen})
            for reply in got["replies"]:
                seen = max(seen, int(reply["seq"]))
                write(f"\n{name}> {reply['text']}\n")
            if not wait or not got["alive"]:
                return
            idle_polls = idle_polls + 1 if not got["busy"] else 0
            if idle_polls >= 3:
                return
            time.sleep(CHAT_POLL_SECONDS)

    write(f"与 {name} 对话；空行不发送，输入 /quit 或 Ctrl+D 结束。")
    show(wait=False)
    sent = 0
    while True:
        try:
            line = read("你> ")
        except (EOFError, KeyboardInterrupt):
            break
        if line.strip() == "/quit":
            break
        if not line.strip():
            continue
        pi_sessions.request(home, {"op": "chat", "name": name, "message": line})
        sent += 1
        show(wait=True)
    return {"session": pi_sessions.ref(name), "sent": sent, "_silent": True}
