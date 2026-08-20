#!/usr/bin/env python3
"""Install merge-safe global TAM guidance for supported coding agents."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import tempfile


BEGIN = "<!-- TAM agent instructions: begin -->"
END = "<!-- TAM agent instructions: end -->"


def _block(agent: str, root: Path) -> str:
    guide = root / "docs" / "ARGUS.md"
    if agent == "codex":
        body = f"""# Shared TAM task tracking

- TAM is the shared task database for agent work across projects.
- Before operating TAM, starting long-running work, or recording task progress,
  read `{guide}` and follow it.
- Use a distinct `TAM_ACTOR`, read issue history before acting, bind watches to
  launched jobs, and never leave an issue `in_progress` with nothing running.
"""
    else:
        body = f"""# Shared TAM task tracking

TAM is the shared task database for agent work across projects. Before
operating TAM, starting long-running work, or recording task progress, read and
follow @{guide}.
"""
    return f"{BEGIN}\n{body.rstrip()}\n{END}\n"


def _without_managed_block(text: str) -> str:
    start = text.find(BEGIN)
    if start < 0:
        return text
    finish = text.find(END, start)
    if finish < 0:
        raise ValueError(f"found {BEGIN!r} without matching {END!r}")
    finish += len(END)
    if finish < len(text) and text[finish] == "\n":
        finish += 1
    return (text[:start].rstrip() + "\n" + text[finish:].lstrip()).strip()


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o644
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def update(path: Path, block: str | None) -> str:
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    clean = _without_managed_block(existing)
    if block is None:
        if not clean:
            path.unlink(missing_ok=True)
            return "removed"
        output = clean.rstrip() + "\n"
    else:
        output = f"{clean.rstrip()}\n\n{block}" if clean else block
    if output == existing:
        return "unchanged"
    _write(path, output)
    return "updated" if existing else "created"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Install persistent TAM guidance for Codex, Claude, and Gemini."
    )
    parser.add_argument("--remove", action="store_true", help="remove managed blocks")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--home", type=Path, default=Path.home(), help=argparse.SUPPRESS)
    args = parser.parse_args()

    root = args.root.resolve()
    if not (root / "docs" / "ARGUS.md").is_file():
        parser.error(f"TAM guide not found under {root}")

    targets = {
        "codex": args.home / ".codex" / "AGENTS.md",
        "claude": args.home / ".claude" / "CLAUDE.md",
        "gemini": args.home / ".gemini" / "GEMINI.md",
    }
    for agent, path in targets.items():
        block = None if args.remove else _block(agent, root)
        print(f"{update(path, block):9} {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
