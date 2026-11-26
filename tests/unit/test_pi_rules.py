"""Pure rules of the pi host integration and the pi-cli reviewer (PI001, PI004)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tsw.config import load_config, valid_model_id
from tsw.home import Home
from tsw.host import pi_guard, pi_sessions
from tsw.host.guard import bash_problem
from tsw.host.identity import session_of
from tsw.machine.ask_user import is_automated
from tsw.review import pi
from tsw.review.models import Model, ModelList
from tsw.setup import questions as q

PI = "UC-PI-002"
HOSTED = "UC-PI-003"


@pytest.mark.use_case(PI)
def test_the_session_comes_from_tsw_then_claude_then_pi() -> None:
    assert session_of({"TSW_SESSION_ID": "pi:w", "PI_SESSION_ID": "x"}) == "pi:w"
    assert session_of({"CLAUDE_CODE_HOST_SESSION_ID": "c", "PI_SESSION_ID": "x"}) == "c"
    assert session_of({"PI_SESSION_ID": "x"}) == "pi:x"
    assert session_of({}) is None


@pytest.mark.use_case(PI)
def test_pi_tools_reach_the_guard_under_the_names_it_knows() -> None:
    env = {"TSW_SESSION_ID": "pi:w"}
    write = pi_guard.hook_payload(
        "PreToolUse", {"tool": "write", "input": {"path": "/x", "content": "c"}, "cwd": "/w"}, env
    )
    assert write["tool_name"] == "Write" and write["tool_input"] == {
        "file_path": "/x",
        "content": "c",
    }
    assert write["session_id"] == "pi:w" and write["cwd"] == "/w"
    bash = pi_guard.hook_payload("PreToolUse", {"tool": "bash", "input": {"command": "ls"}}, env)
    assert bash["tool_name"] == "Bash" and bash["tool_input"] == {"command": "ls"}
    read = pi_guard.hook_payload("PreToolUse", {"tool": "read", "input": {"path": "/s"}}, env)
    assert read["tool_name"] == "Read" and read["tool_input"] == {"path": "/s"}
    prompt = pi_guard.hook_payload("UserPromptSubmit", {"prompt": "好"}, env)
    assert prompt["prompt"] == "好" and "tool_name" not in prompt


@pytest.mark.use_case(PI)
def test_the_guards_answers_become_what_the_extension_does() -> None:
    denied = {"hookSpecificOutput": {"permissionDecision": "deny", "permissionDecisionReason": "R"}}
    assert pi_guard.outcome("PreToolUse", denied) == {"block": True, "reason": "R"}
    added = {"hookSpecificOutput": {"additionalContext": "C"}}
    assert pi_guard.outcome("SessionStart", added) == {"context": "C"}
    assert pi_guard.outcome("Stop", {"decision": "block", "reason": "S"}) == {"followUp": "S"}
    assert pi_guard.outcome("Stop", None) == {}


@pytest.mark.use_case("UC-PI-001")
def test_messages_between_sessions_are_never_the_users_words() -> None:
    assert is_automated(pi_sessions.wrap("pi:orch", "请继续"))
    with pytest.raises(Exception, match="会话名"):
        pi_sessions.check_name("-bad name")
    assert pi_sessions.name_of(pi_sessions.ref("t001-a1-g1")) == "t001-a1-g1"


def _write(home: Home, layer: dict[str, Any]) -> list[str]:
    home.path("config/task-steward.json").write_text(json.dumps(layer), encoding="utf-8")
    return load_config(home).problems


@pytest.mark.use_case(HOSTED)
def test_a_pi_configuration_from_the_install_is_valid_and_its_rules_hold(
    home: Home, tmp_path: Path
) -> None:
    pi_command = tmp_path / "pi"
    pi_command.write_text("", encoding="utf-8")  # pi is offered only where it is found
    answers, problems = q.normalize(
        {
            "home": str(home.root),
            "host": "pi",
            "reviewer": "pi-cli",
            "reviewer_model": "openai/gpt-5.4",
            "reviewer_effort": "medium",
        },
        q.questions({"PATH": "", "TSW_PI": str(pi_command)}, Path.cwd()),
    )
    assert problems == []
    layer = q.config_layer(answers)
    assert _write(home, layer) == []
    assert layer["roles"]["worker"] == {"session": "pi-rpc", "model": "default"}
    wrong = {**layer, "routine": {"enabled": True}}
    assert any(p.startswith("routine.enabled") for p in _write(home, wrong))
    claude_worker = {**layer, "roles": {**layer["roles"], "worker": {"session": "claude-app"}}}
    assert any("pi-rpc" in p for p in _write(home, claude_worker))


@pytest.mark.use_case(HOSTED)
def test_pi_models_carry_their_provider() -> None:
    assert valid_model_id("pi-cli", "openai/gpt-5.4")
    assert not valid_model_id("pi-cli", "gpt-5.4")
    assert valid_model_id("pi-rpc", "default")
    assert not valid_model_id("codex-app-server", "openai/gpt-5.4")


@pytest.mark.use_case(HOSTED)
@pytest.mark.posix
def test_the_host_defaults_to_pi_only_without_claude(tmp_path: Path) -> None:
    folder = tmp_path / "bin"
    folder.mkdir()
    (folder / "pi").write_text("#!/bin/sh\n", encoding="utf-8")
    (folder / "pi").chmod(0o755)
    assert q.default_host({"PATH": str(folder)}) == "pi"
    (folder / "claude").write_text("#!/bin/sh\n", encoding="utf-8")
    (folder / "claude").chmod(0o755)
    assert q.default_host({"PATH": str(folder)}) == "claude-code"


def _events(*records: dict[str, Any]) -> list[bytes]:
    return [json.dumps(r).encode("utf-8") + b"\n" for r in records]


def _reply(provider: str = "openai", stop: str = "stop", text: str = "{}") -> dict[str, Any]:
    message = {
        "role": "assistant",
        "provider": provider,
        "model": "gpt-5.4",
        "stopReason": stop,
        "content": [{"type": "text", "text": text}],
    }
    return {"type": "message_end", "message": message}


SETTLED = {"type": "agent_settled", "aborted": False}


@pytest.mark.use_case("UC-REVIEW-002")
def test_a_pi_review_is_its_final_settled_reply_by_the_configured_model() -> None:
    done = pi.read_output(_events(_reply(text='{"a":1}'), SETTLED), "openai/gpt-5.4")
    assert (done.status, done.final_message, done.model) == (
        "completed",
        '{"a":1}',
        "openai/gpt-5.4",
    )
    other = pi.read_output(_events(_reply(provider="x"), SETTLED), "openai/gpt-5.4")
    assert other.status == "failed" and "provider/model" in str(other.error)
    unsettled = pi.read_output(_events(_reply()), "openai/gpt-5.4")
    assert unsettled.status == "failed"
    cut = pi.read_output(_events(_reply(stop="length"), SETTLED), "openai/gpt-5.4")
    assert cut.status == "failed"
    assert pi.read_output([b"not json\n"], "openai/gpt-5.4").status == "failed"


@pytest.mark.use_case(PI)
def test_the_shell_knows_the_pi_commands_and_refuses_the_hook_entry(home: Home) -> None:
    def problem(command: str) -> str | None:
        return bash_problem(
            command,
            home=home,
            push_allowed=False,
            pr_allowed=False,
            worker=False,
            orchestrator=True,
        )

    assert problem("tsw pi stop --name t001-a1-g1") is None
    assert problem("tsw pi list") is None
    assert "hook" in str(problem("tsw pi guard UserPromptSubmit"))


@pytest.mark.use_case("UC-REVIEW-004")
def test_the_terminal_accepts_a_pi_model_with_its_provider(tmp_path: Path) -> None:
    listed = ModelList("pi-cli", [Model("github-copilot/claude-haiku-4.5"), Model("openai/x")])
    replies = iter([str(tmp_path / "data"), "", "pi-cli", "", "", "", "", "", "", ""])
    said: list[str] = []
    answers = q.ask(
        q.questions({"PATH": ""}, Path.cwd()),
        {},
        lambda _prompt: next(replies),
        said.append,
        lambda _reviewer: listed,
    )
    assert answers["reviewer"] == "pi-cli"
    assert answers["reviewer_model"] == "github-copilot/claude-haiku-4.5"
    assert not any("格式不正确" in line for line in said)
    # The question bound to pi-cli checks a model on its own too (the arrow-key interface).
    model = next(x for x in q.questions({"PATH": ""}, Path.cwd()) if x.id == "reviewer_model")
    bound = q.bind(model, {"reviewer": "pi-cli"}, lambda _reviewer: listed)
    assert bound is not None
    assert q.normalize({"reviewer_model": "openai/x"}, [bound])[1] == []
