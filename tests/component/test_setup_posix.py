"""``tsw setup`` on Linux / WSL (DN009 §12, PI001 §8): the ``tsw`` link, PATH left to the user,
and the pi host. pi and Claude Code are outside this repository: their stand-ins are
``tests/fixtures/fake_pi.py`` and ``tests/fixtures/fake_claude_plugins.py`` (simulated).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from tests.setup_harness import (
    fake_claude,
    make_product,
    record,
    run_setup,
    setup_env,
    write_answers,
)
from tests.support import REPO_ROOT, stand_in

pytestmark = [pytest.mark.posix, pytest.mark.use_case("UC-SETUP-001")]
FAKE_PI = REPO_ROOT / "tests" / "fixtures" / "fake_pi.py"


@pytest.mark.use_case("UC-PI-003")
def test_a_pi_install_links_tsw_installs_the_package_and_renders_pi_skills(tmp_path: Path) -> None:
    product = make_product(tmp_path)
    (tmp_path / "pi-bin").mkdir()
    agent = tmp_path / "agent"
    env = setup_env(
        tmp_path,
        TSW_PI=str(stand_in(tmp_path / "pi-bin" / "pi", FAKE_PI)),
        PI_CODING_AGENT_DIR=str(agent),
        FAKE_PI_LOG=str(tmp_path / "pi.log"),
    )
    home = tmp_path / "data"
    answers = write_answers(tmp_path, home, host="pi", reviewer="none")
    code, out, text = run_setup(product, "install", "--answers", str(answers), env=env)
    assert code == 0, text
    steps = {s["step"]: s["state"] for s in out["steps"]}
    assert steps["plugin"] == "todo" and steps["user_settings"] == "skipped"
    assert steps["workdeck"] == "skipped"
    packages = json.loads((agent / "settings.json").read_text(encoding="utf-8"))["packages"]
    assert packages == [str((product / "pi").resolve())]
    link = Path(env["HOME"]) / ".local" / "bin" / "tsw"
    assert link.is_symlink() and os.access(link, os.X_OK)
    assert (home / ".pi" / "skills" / "orchestrate" / "SKILL.md").is_file()
    config = json.loads((home / "config" / "task-steward.json").read_text(encoding="utf-8"))
    assert config["plugins"]["host_integration"] == "pi"
    assert config["roles"]["worker"]["session"] == "pi-rpc"
    checks = {c["check"]: c["ok"] for c in out["verify"]["checks"]}
    assert checks["pi_package"] is True and checks["shims"] is True
    assert "plugin_source" not in checks and "/skill:orchestrate" in out["next"]
    assert record(env)["steps"]["pi_package"]["installed_before"] is False
    assert "plugin" not in record(env)["steps"]

    again, rerun, _ = run_setup(product, "install", "--answers", str(answers), env=env)
    assert again == 0 and {s["step"]: s["state"] for s in rerun["steps"]}["plugin"] == "ok"

    code, gone, text = run_setup(product, "uninstall", "--yes", env=env)
    assert code == 0, text
    assert not link.exists() and not link.is_symlink()
    assert json.loads((agent / "settings.json").read_text(encoding="utf-8"))["packages"] == []
    assert (home / "tsw-home.json").is_file()  # the data directory is never removed
    assert gone["removed"]["pi_package"]["ok"] is True


def test_a_claude_code_install_on_linux_leaves_path_to_the_user(tmp_path: Path) -> None:
    product = make_product(tmp_path)
    claude = fake_claude(tmp_path)
    env = setup_env(tmp_path)
    shims = str(Path(env["HOME"]) / ".local" / "bin")
    env["PATH"] = os.pathsep.join(p for p in env["PATH"].split(os.pathsep) if p != shims)
    answers = write_answers(tmp_path, tmp_path / "data")
    code, planned, text = run_setup(
        product, "plan", "--answers", str(answers), "--claude", str(claude), env=env
    )
    assert code == 0, text
    path = next(s for s in planned["steps"] if s["step"] == "path")
    assert path["state"] == "manual" and 'export PATH="$HOME/.local/bin:$PATH"' in path["manual"]
    shim = next(s for s in planned["steps"] if s["step"] == "shim")
    assert shim["dir"] == str(Path(env["HOME"]) / ".local" / "bin")


@pytest.mark.use_case("UC-PI-003")
def test_a_pi_package_the_user_had_is_kept_on_uninstall(tmp_path: Path) -> None:
    product = make_product(tmp_path)
    (tmp_path / "pi-bin").mkdir()
    agent = tmp_path / "agent"
    agent.mkdir()
    package = str((product / "pi").resolve())
    (agent / "settings.json").write_text(json.dumps({"packages": [package]}), encoding="utf-8")
    env = setup_env(
        tmp_path,
        TSW_PI=str(stand_in(tmp_path / "pi-bin" / "pi", FAKE_PI)),
        PI_CODING_AGENT_DIR=str(agent),
    )
    answers = write_answers(tmp_path, tmp_path / "data", host="pi", reviewer="none")
    code, out, text = run_setup(product, "install", "--answers", str(answers), env=env)
    assert code == 0, text
    assert {s["step"]: s["state"] for s in out["steps"]}["plugin"] == "ok"
    code, gone, text = run_setup(product, "uninstall", "--yes", env=env)
    assert code == 0, text
    assert "保留" in gone["removed"]["pi_package"]["note"]
    assert json.loads((agent / "settings.json").read_text(encoding="utf-8"))["packages"] == [
        package
    ]


@pytest.mark.use_case("UC-PI-003")
def test_an_uninstall_that_cannot_remove_the_pi_package_stops_before_the_shim(
    tmp_path: Path,
) -> None:
    product = make_product(tmp_path)
    (tmp_path / "pi-bin").mkdir()
    env = setup_env(
        tmp_path,
        TSW_PI=str(stand_in(tmp_path / "pi-bin" / "pi", FAKE_PI)),
        PI_CODING_AGENT_DIR=str(tmp_path / "agent"),
    )
    answers = write_answers(tmp_path, tmp_path / "data", host="pi", reviewer="none")
    code, _out, text = run_setup(product, "install", "--answers", str(answers), env=env)
    assert code == 0, text
    env["TSW_PI"] = str(tmp_path / "no-pi")  # pi is gone
    code, out, _text = run_setup(product, "uninstall", "--yes", env=env)
    assert code != 0 and out["details"]["step"] == "pi_package"
    assert (Path(env["HOME"]) / ".local" / "bin" / "tsw").is_symlink()
    assert record(env)["status"] == "uninstalling"
