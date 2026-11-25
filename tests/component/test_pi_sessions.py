"""The pi session server, install steps and reviewer against the pi stand-in (PI001, PI004).

pi is outside this repository: ``tests/fixtures/fake_pi.py`` stands in for it (simulated).
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest

from tests.machine_harness import Harness
from tests.support import REPO_ROOT, stand_in
from tsw.home import Home
from tsw.host import pi_cli, pi_install, pi_sessions
from tsw.review import models, pi
from tsw.setup import posixenv

# The pi host is Linux / WSL first (PI001 §1): Unix sockets, process groups, sh stand-ins.
pytestmark = pytest.mark.posix
FAKE = REPO_ROOT / "tests" / "fixtures" / "fake_pi.py"
SESSIONS = "UC-PI-001"


@pytest.fixture
def fake_pi(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / "bin").mkdir()
    command = stand_in(tmp_path / "bin" / "pi", FAKE)
    monkeypatch.setenv("TSW_PI", str(command))
    monkeypatch.setenv("FAKE_PI_LOG", str(tmp_path / "pi.log"))
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(tmp_path / "agent"))
    return command


def calls(tmp_path: Path) -> list[dict[str, Any]]:
    log = tmp_path / "pi.log"
    return (
        [json.loads(x) for x in log.read_text(encoding="utf-8").splitlines()]
        if log.is_file()
        else []
    )


def wait_for(check: Any, timeout: float = 20.0) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        found = check()
        if found:
            return found
        time.sleep(0.05)
    raise AssertionError("timed out")


@pytest.mark.use_case(SESSIONS)
def test_a_worker_is_started_messaged_resumed_and_stopped(
    home: Home, tmp_path: Path, fake_pi: Path
) -> None:
    server = pi_sessions.Server(home)
    workdir = tmp_path / "wt"
    workdir.mkdir()
    started = server.handle(
        {
            "op": "start",
            "name": "t001-a1-g1",
            "cwd": str(workdir),
            "prompt": "任务书",
            "model": "openai/gpt-5.4",
            "thinking": "high",
        }
    )
    assert started["session_ref"] == "pi:t001-a1-g1"
    tail = wait_for(lambda: server.handle({"op": "tail", "name": "t001-a1-g1"})["replies"])
    assert tail[0]["text"] == "echo: 任务书"
    spawned = next(c for c in calls(tmp_path) if "argv" in c)
    assert spawned["session"] == "pi:t001-a1-g1" and spawned["cwd"] == str(workdir)
    argv = spawned["argv"]
    assert argv[argv.index("--session-id") + 1] == "t001-a1-g1"
    assert argv[argv.index("--model") + 1] == "openai/gpt-5.4"
    assert argv[argv.index("-e") + 1] == str(REPO_ROOT / "pi")
    again = server.handle({"op": "start", "name": "t001-a1-g1", "cwd": str(workdir)})
    assert again["existed"] is True
    server.handle({"op": "stop", "name": "t001-a1-g1"})
    assert (pi_sessions.read_state(home, "t001-a1-g1") or {}).get("state") == "stopped"
    # A message to a stopped worker resumes the same pi session in the same directory.
    server.handle({"op": "send", "name": "t001-a1-g1", "message": "继续"})
    wait_for(lambda: any(c.get("rpc", {}).get("message") == "继续" for c in calls(tmp_path)))
    assert sum(1 for c in calls(tmp_path) if "argv" in c) == 2
    listed = server.listing()
    assert listed[0]["name"] == "t001-a1-g1" and listed[0]["alive"]
    server.close()


@pytest.mark.use_case(SESSIONS)
def test_messages_to_the_main_session_wait_in_its_mailbox(home: Home) -> None:
    out = pi_sessions.send(home, "pi:t001-a1-g1", "pi:orch", "T001 已提交")
    assert out["delivered"] == "mailbox"
    taken = pi_sessions.take(home, "pi:orch")
    assert len(taken) == 1 and "T001 已提交" in taken[0] and taken[0].startswith("<cross-session")
    assert pi_sessions.take(home, "pi:orch") == []


@pytest.mark.use_case(SESSIONS)
def test_only_a_recorded_attempt_gets_a_session_in_its_own_folder(
    home: Home, tmp_path: Path, fake_pi: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = Harness(home)
    h.register("T001")
    h.plan("T001")
    workdir = tmp_path / "wt" / "T001"
    workdir.mkdir(parents=True)
    env = {"TSW_SESSION_ID": "orch-1"}
    with pytest.raises(Exception, match="attempt_start"):
        pi_cli.start(
            home, env, home.root, name="t001-a1-g1", folder=workdir, prompt="p", title=None
        )
    h.run("task_dispatch", task_id="T001", workdir=str(workdir), user_turn=True)
    started = h.run("attempt_start", task_id="T001", purpose="dispatch", user_turn=True)
    name = str(started["events"][0]["data"]["session_name"])
    with pytest.raises(Exception, match="执行目录"):
        pi_cli.start(home, env, home.root, name=name, folder=tmp_path, prompt="p", title=None)
    with pytest.raises(Exception, match="主会话"):
        pi_cli.start(home, env, workdir, name=name, folder=workdir, prompt="p", title=None)
    requested: list[dict[str, Any]] = []

    def asked(_home: Home, payload: dict[str, Any], **_: Any) -> dict[str, Any]:
        requested.append(payload)
        return {"session_ref": "x"}

    monkeypatch.setattr(pi_sessions, "request", asked)
    pi_cli.start(home, env, home.root, name=name, folder=workdir, prompt="p", title="T001")
    assert requested[0]["cwd"] == str(workdir.resolve()) and requested[0]["prompt"] == "p"


@pytest.mark.use_case("UC-PI-002")
def test_the_main_session_polls_its_mailbox(home: Home) -> None:
    Harness(home)
    pi_sessions.post(home, "orch-1", "hello")
    polled = pi_cli.poll(home, {"TSW_SESSION_ID": "orch-1"}, home.root)
    assert polled == {"messages": ["hello"], "handoff_due": []}
    who = pi_cli.whoami(home, {"TSW_SESSION_ID": "orch-1"}, home.root)
    assert who["role"] == "orchestrator" and "tsw_start_session" in who["notes"]
    assert who["mcp"]["args"][-1] == "mcp"
    assert pi_cli.whoami(home, {"TSW_SESSION_ID": "x"}, Path("/"))["notes"] == ""


@pytest.mark.use_case("UC-PI-003")
def test_the_pi_package_is_installed_and_removed_through_pi(tmp_path: Path, fake_pi: Path) -> None:
    env = {"PI_CODING_AGENT_DIR": str(tmp_path / "agent"), "FAKE_PI_LOG": str(tmp_path / "pi.log")}
    assert not pi_install.installed(env, REPO_ROOT)
    assert pi_install.install(env, str(fake_pi), REPO_ROOT)["ok"]
    assert pi_install.installed(env, REPO_ROOT)
    assert pi_install.remove(env, str(fake_pi), REPO_ROOT)["ok"]


@pytest.mark.use_case("UC-SETUP-001")
def test_the_posix_shim_is_a_link_to_a_launcher_and_never_replaces_another(tmp_path: Path) -> None:
    link, target = tmp_path / "bin" / "tsw", tmp_path / "state" / "bin" / "tsw"
    text = posixenv.script("/usr/bin/python3", tmp_path / "checkout")
    assert posixenv.shim_state(link, target, text) == "missing"
    posixenv.write_shim(link, target, text)
    assert posixenv.shim_state(link, target, text) == "ok"
    assert b"tsw_launch.py" in target.read_bytes()
    assert posixenv.remove_shim(link, target)["removed"] == ["tsw"] and not link.exists()
    link.write_text("someone else's", encoding="utf-8")
    assert posixenv.shim_state(link, target, text) == "conflict"
    with pytest.raises(Exception, match="已被占用"):
        posixenv.write_shim(link, target, text)
    assert link.read_text(encoding="utf-8") == "someone else's"


@pytest.mark.use_case("UC-REVIEW-004")
def test_pi_lists_its_models_with_the_provider(fake_pi: Path) -> None:
    found = models.list_models("pi-cli", str(fake_pi))
    assert [m.id for m in found.models] == ["openai/gpt-5.4", "anthropic/claude-x"]


@pytest.mark.use_case("UC-REVIEW-002")
def test_a_pi_review_runs_read_only_without_extensions(
    tmp_path: Path, fake_pi: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_PI_ANSWER", '{"verdict": "pass"}')
    run = pi.run_review(
        "p",
        cwd=tmp_path,
        model="openai/gpt-5.4",
        effort="medium",
        timeout_seconds=60,
        executable=str(fake_pi),
    )
    assert run.status == "completed" and run.final_message == '{"verdict": "pass"}'
    argv = next(c for c in calls(tmp_path) if "argv" in c)["argv"]
    for flag in (
        "--no-extensions",
        "--no-mcp",
        "--no-skills",
        "--no-context-files",
        "--no-session",
    ):
        assert flag in argv
    assert argv[argv.index("--tools") + 1] == "read,grep,find,ls"
