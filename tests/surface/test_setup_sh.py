"""``setup.sh``, the Linux / WSL terminal entry (DN009 §12, §16.4), as a separate process.

Claude Code is outside this repository: ``tests/fixtures/fake_claude_plugins.py`` stands in for its
plugin commands (simulated).
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.setup_harness import claude_calls, fake_claude, make_product, record, setup_env

pytestmark = pytest.mark.posix


@pytest.mark.use_case("UC-SETUP-001")
@pytest.mark.use_case("UC-SETUP-005")
def test_setup_sh_runs_the_interactive_install(tmp_path: Path) -> None:
    product, claude = make_product(tmp_path), fake_claude(tmp_path)
    env = setup_env(tmp_path, TSW_PYTHON=sys.executable)
    env["PATH"] = os.pathsep.join([str(claude.parent), env["PATH"]])
    home = tmp_path / "data"
    replies = [str(home), "", "", "", "", "", "否", "", "", "是"]  # reviewer, model, effort
    proc = subprocess.run(
        ["sh", str(product / "setup.sh"), "--no-browser"],
        input="\n".join(replies) + "\n",
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        cwd=str(tmp_path),
        timeout=300,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    # Piped input and output: the line questions without colour or cursor movement (DN009 §16.4).
    assert "\x1b" not in proc.stdout and "请回答下面的问题" in proc.stdout
    assert (home / "tsw-home.json").is_file()
    assert record(env)["status"] == "installed"
    assert (Path(env["HOME"]) / ".local" / "bin" / "tsw").is_symlink()
    assert claude_calls(tmp_path)[0] == f"plugin marketplace add {product}"


@pytest.mark.use_case("UC-SETUP-001")
def test_setup_sh_stops_without_a_usable_python(tmp_path: Path) -> None:
    product = make_product(tmp_path)
    env = setup_env(tmp_path, TSW_PYTHON=str(tmp_path / "no-python"))
    proc = subprocess.run(
        ["sh", str(product / "setup.sh")],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 1 and "Python 3.10+" in proc.stderr
