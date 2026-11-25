# Pi's JSONL events are walked generically.
# pyright: reportUnknownMemberType=false, reportUnknownArgumentType=false
# pyright: reportUnknownVariableType=false
"""One ephemeral, read-only Pi CLI review per round (PI004)."""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from tsw.errors import ErrorCode, TswError
from tsw.review.codex import ReviewRun
from tsw.review.process import ProcessTree

# Do not load code, tools or prompts from either the reviewed project or installed extensions.
ISOLATE = (
    "--no-approve",
    "--no-extensions",
    "--no-mcp",
    "--no-skills",
    "--no-context-files",
    "--no-prompt-templates",
    "--no-session",
)


def resolve(configured: str | None) -> str:
    found = shutil.which(configured or "pi")
    if not found:
        raise TswError(
            ErrorCode.UNAVAILABLE,
            "找不到 Pi 评审器命令（需要 pi CLI）",
            reason="reviewer_unavailable",
        )
    return found


def read_output(lines: Iterable[bytes], model: str) -> ReviewRun:
    """Keep only the completed assistant answer, never reasoning deltas or tool output."""
    actual: str | None = None
    answer: str | None = None
    settled = False
    for line in lines:
        try:
            event: Any = json.loads(line)
        except (ValueError, UnicodeError):
            return ReviewRun(None, "failed", None, actual, "Pi 输出了无效的 JSONL")
        if not isinstance(event, dict):
            continue
        if event.get("type") == "agent_settled":
            settled = True
        if event.get("type") != "message_end":
            continue
        message: Any = event.get("message")
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        settled = False
        actual = f"{message.get('provider')}/{message.get('model')}"
        if actual != model:
            return ReviewRun(
                None, "failed", None, actual, "Pi 实际使用的 provider/model 与配置不符"
            )
        answer = None
        if message.get("stopReason") == "stop":
            answer = "".join(
                p["text"]
                for p in message.get("content", [])
                if isinstance(p, dict)
                and p.get("type") == "text"
                and isinstance(p.get("text"), str)
            )
    if not settled or not answer or not answer.strip():
        return ReviewRun(None, "failed", None, actual, "Pi 未正常结束或缺少最终回复")
    return ReviewRun(None, "completed", answer, actual)


def run_review(
    prompt: str,
    *,
    cwd: Path,
    model: str,
    effort: str,
    timeout_seconds: int,
    executable: str | None = None,
    **_: Any,
) -> ReviewRun:
    provider, _sep, model_id = model.partition("/")
    argv = [
        executable or "pi",
        *ISOLATE,
        "--print",
        "--mode",
        "json",
        "--tools",
        "read,grep,find,ls",
        "--provider",
        provider,
        "--model",
        model_id,
        "--thinking",
        effort,
        "--",
        prompt,
    ]
    # JSON mode also emits incremental message snapshots. Spool to an anonymous file, not
    # memory or the ledger. A descendant holding a pipe open cannot outlive the deadline.
    with tempfile.TemporaryFile() as output:
        tree = ProcessTree(
            argv, cwd=cwd, stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.DEVNULL
        )
        try:
            code = tree.proc.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            return ReviewRun(None, "timedOut", None, None, "Pi 评审超时")
        finally:
            tree.end()
        if code:
            return ReviewRun(
                None, "failed", None, None, f"Pi 以退出码 {code} 结束；请核对版本、登录与模型"
            )
        output.seek(0)
        return read_output(output, model)
