# SPDX-License-Identifier: GPL-2.0-or-later
"""What the workspace tools share: where the workspace is, private folders, a file lock.

The tools find artifacts/ and notes/ from ~/GlassineOS, not from their own path, so they work the
same from a worktree. GLASSINE_WORKSPACE overrides that, for tests.
"""
from __future__ import annotations

import fcntl
import os
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


class ToolError(Exception):
    """A refusal or a failure to explain in words: the tool prints it and exits 1."""


def home() -> Path:
    return Path(os.environ.get("GLASSINE_WORKSPACE") or Path.home() / "GlassineOS")


def artifacts() -> Path:
    return home() / "artifacts"


def notes() -> Path:
    return home() / "notes"


def private_dir(path: Path) -> Path:
    """Make a folder only this user can read, tightening it if it's already there and wider."""
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.stat().st_mode & 0o077:
        path.chmod(0o700)
    return path


@contextmanager
def locked(path: Path):
    """Hold an exclusive lock on a file next to what it guards, so sessions can't race."""
    with open(path, "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield


def utc(seconds: float | None = None) -> str:
    moment = datetime.now(timezone.utc) if seconds is None else datetime.fromtimestamp(seconds, timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def run(prog: str, main, argv=None) -> int:
    """Call a tool's main, turning a ToolError into words on stderr and exit status 1."""
    try:
        return main(argv) or 0
    except (ToolError, OSError) as e:
        print(f"{prog}: {e}", file=sys.stderr)
        return 1
