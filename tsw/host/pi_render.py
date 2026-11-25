"""``tsw host render`` for the ``pi`` host integration (PI001 §3, §8).

pi reads the data directory's ``CLAUDE.md`` as a context file, so the main session's instructions
are the same file; its project skills are in ``.pi/skills/``. The skills Claude Code gets are
copied there unchanged, and the extension adds how their Claude Code wording maps to pi
(``tsw/host/pi_notes.md``). One source of every instruction for both hosts.
"""

from __future__ import annotations

from pathlib import Path

from tsw.home import Home
from tsw.storage.fs import atomic_write

CLAUDE_SKILLS = Path(".claude") / "skills"
PI_SKILLS = Path(".pi") / "skills"


def render(home: Home, written: list[Path]) -> list[Path]:
    """Copy the skills among ``written`` (what the Claude Code render wrote) to ``.pi/skills``."""
    source = home.root / CLAUDE_SKILLS
    out: list[Path] = []
    for path in written:
        try:
            rel = path.relative_to(source)
        except ValueError:
            continue
        target = home.root / PI_SKILLS / rel
        atomic_write(target, path.read_bytes())
        out.append(target)
    return out
