"""``tsw pi`` as separate processes (PI001): what the pi extension runs, and ``tsw pi chat``.

pi is outside this repository: ``tests/fixtures/fake_pi.py`` stands in for it (simulated).
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest

from tests.machine_harness import Harness
from tests.support import REPO_ROOT, child_env, run_tsw, stand_in
from tsw.home import Home
from tsw.host import pi_sessions

pytestmark = pytest.mark.posix
FAKE = REPO_ROOT / "tests" / "fixtures" / "fake_pi.py"


def tsw_json(home: Home, *args: str, cwd: Path, **env: str) -> tuple[int, dict[str, Any]]:
    proc = run_tsw(*args, "--json", env=child_env(TSW_HOME=str(home.root), **env), cwd=cwd)
    stream = proc.stdout if proc.stdout.strip() else proc.stderr
    lines = stream.strip().splitlines()
    return proc.returncode, json.loads(lines[-1]) if lines else {}


@pytest.mark.use_case("UC-PI-002")
def test_a_worker_message_reaches_the_main_sessions_poll(home: Home) -> None:
    Harness(home)
    code, sent = tsw_json(
        home,
        "pi",
        "send",
        "--to",
        "orch-1",
        "--message",
        "T001 已提交",
        cwd=home.root,
        TSW_SESSION_ID="pi:t001-a1-g1",
    )
    assert code == 0 and sent["delivered"] == "mailbox"
    code, polled = tsw_json(home, "pi", "poll", cwd=home.root, TSW_SESSION_ID="orch-1")
    assert code == 0 and len(polled["messages"]) == 1
    assert polled["messages"][0].startswith('<cross-session-message from="pi:t001-a1-g1">')
    code, who = tsw_json(home, "pi", "whoami", cwd=home.root, TSW_SESSION_ID="orch-1")
    assert code == 0 and who["role"] == "orchestrator" and who["notes"]


@pytest.mark.use_case("UC-PI-002")
def test_the_guard_blocks_a_pi_write_into_the_ledger(home: Home) -> None:
    Harness(home)
    event = {"tool": "write", "input": {"path": str(home.root / "ledger" / "x.jsonl")}}
    proc = run_tsw(
        "pi",
        "guard",
        "PreToolUse",
        "--json",
        env=child_env(TSW_HOME=str(home.root), TSW_SESSION_ID="orch-1"),
        cwd=home.root,
        stdin=json.dumps({"cwd": str(home.root), **event}),
    )
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert proc.returncode == 0 and out["block"] is True and "ledger" in out["reason"]

    def guarded(tool: str, path: str) -> dict[str, Any]:
        proc = run_tsw(
            "pi",
            "guard",
            "PreToolUse",
            "--json",
            env=child_env(TSW_HOME=str(home.root), TSW_SESSION_ID="orch-1"),
            cwd=home.root,
            stdin=json.dumps({"cwd": str(home.root), "tool": tool, "input": {"path": path}}),
        )
        return json.loads(proc.stdout.strip().splitlines()[-1])

    assert "block" not in guarded("read", "x")
    # The Dashboard token folder is refused to the read-only tools too (CC-B12).
    secret = str(home.root.parent / "state" / "tsw-dashboard-secret" / "k.json")
    assert guarded("read", secret)["block"] is True
    assert guarded("grep", secret)["block"] is True


@pytest.mark.use_case("UC-PI-001")
def test_chat_is_the_users_own_and_refused_in_a_session(home: Home) -> None:
    Harness(home)
    code, out = tsw_json(home, "pi", "chat", "T001", cwd=home.root, TSW_SESSION_ID="orch-1")
    assert code != 0 and out["details"]["reason"] == "host_session_cli"


@pytest.mark.use_case("UC-PI-001")
def test_the_main_session_starts_lists_and_stops_a_worker_through_the_server(
    home: Home, tmp_path: Path
) -> None:
    h = Harness(home)
    h.register("T001")
    h.plan("T001")
    workdir = tmp_path / "wt" / "T001"
    workdir.mkdir(parents=True)
    h.run("task_dispatch", task_id="T001", workdir=str(workdir), user_turn=True)
    started = h.run("attempt_start", task_id="T001", purpose="dispatch", user_turn=True)
    name = str(started["events"][0]["data"]["session_name"])
    (tmp_path / "bin").mkdir()
    env = {
        "TSW_SESSION_ID": "orch-1",
        "TSW_PI": str(stand_in(tmp_path / "bin" / "pi", FAKE)),
        "FAKE_PI_LOG": str(tmp_path / "pi.log"),
        "TSW_PI_IDLE_SECONDS": "1",
    }
    try:
        code, out = tsw_json(
            home,
            "pi",
            "start",
            "--name",
            name,
            "--cwd",
            str(workdir),
            "--prompt",
            "任务书",
            cwd=home.root,
            **env,
        )
        assert code == 0, out
        assert out["session_ref"] == f"pi:{name}"
        log = tmp_path / "pi.log"

        def prompts() -> list[dict[str, Any]]:
            lines = log.read_text(encoding="utf-8").splitlines() if log.is_file() else []
            found = [json.loads(x) for x in lines]
            return [r for r in found if r.get("rpc", {}).get("type") == "prompt"]

        deadline = time.monotonic() + 30
        while not prompts() and time.monotonic() < deadline:
            time.sleep(0.1)
        prompt = prompts()[0]
        assert prompt["session"] == f"pi:{name}" and prompt["rpc"]["message"] == "任务书"
        code, listed = tsw_json(home, "pi", "list", cwd=home.root, **env)
        assert code == 0 and listed["sessions"][0]["alive"] is True
    finally:
        tsw_json(home, "pi", "stop", "--name", name, cwd=home.root, **env)
        socket = pi_sessions.socket_path(home)
        deadline = time.monotonic() + 30
        while socket.exists() and time.monotonic() < deadline:
            time.sleep(0.1)
    assert not socket.exists()  # the server exits once no worker runs
