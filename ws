#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""The workspace: main checkouts of the organization's repositories, and task worktrees.

  ws sync [REPO...]                clone what's missing, fetch the rest
  ws status [--json]               what's dirty, unpushed, and which task holds what
  ws new TASK REPO [REPO...]       work/TASK/REPO worktrees, on branch TASK (or --branch)
  ws done TASK [--drop-ignored]    remove them; refuses while anything isn't pushed, and past
                                   1 MiB of ignored files unless told they can go

Only ever touches main checkouts by fetching: their working trees stay as they are.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
MAX_IGNORED = 1 << 20  # a few caches are fine. Anything more may be a result nobody meant to lose.
SHOWN_IGNORED = 20


class WsError(Exception):
    pass


def git(*args, cwd=None, check=True) -> str:
    done = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, errors="surrogateescape", env=None)
    if check and done.returncode != 0:
        raise WsError(f"git {' '.join(args)}: {done.stderr.strip()}")
    return done.stdout.strip() if done.returncode == 0 else ""


def manifest(root: Path) -> dict:
    return tomllib.loads((root / "workspace.toml").read_text())["repo"]


def set_identity(root: Path, path: Path) -> None:
    identity = tomllib.loads((root / "workspace.toml").read_text())["identity"]
    git("config", "user.name", identity["name"], cwd=path)
    git("config", "user.email", identity["email"], cwd=path)


def folder(root: Path, repo: str) -> Path:
    """Where a repository's main checkout lives. A leading dot goes: .github is this repo's own folder."""
    return root / repo.lstrip(".")


def state(path: Path) -> dict:
    """One checkout in words: branch, uncommitted files, commits on no remote."""
    dirty = git("status", "--porcelain", cwd=path, check=False)
    return {"path": str(path), "branch": git("rev-parse", "--abbrev-ref", "HEAD", cwd=path, check=False) or "?",
            "dirty": len(dirty.splitlines()),
            "unpushed": len(git("log", "--oneline", "HEAD", "--not", "--remotes", cwd=path, check=False).splitlines())}


def _size(path: Path) -> int:
    try:
        return path.lstat().st_size
    except OSError:
        return 0  # gone, or unreadable: it's still listed


def ignored(path: Path) -> list[tuple[Path, int]]:
    """Every ignored file in a checkout and its submodules, with its size. git status doesn't see these."""
    found = []
    for name in git("ls-files", "--others", "--ignored", "--exclude-standard", "-z", cwd=path).split("\0"):
        if name.endswith("/"):  # a repository inside an ignored folder: git stops at its edge
            for at, _, files in os.walk(path / name):
                found += [(Path(at) / f, _size(Path(at) / f)) for f in files]
        elif name:
            found.append((path / name, _size(path / name)))
    for line in git("ls-files", "--stage", "-z", cwd=path).split("\0"):
        if line.startswith("160000 ") and (path / line.split("\t", 1)[1] / ".git").exists():
            found += ignored(path / line.split("\t", 1)[1])
    return found


def status(root: Path) -> dict:
    mains = []
    for repo in manifest(root):
        path = folder(root, repo)
        if (path / ".git").exists():
            mains.append({"repo": repo, **state(path)})
    tasks = {}
    work = root / "work"
    for task in sorted(work.iterdir()) if work.is_dir() else []:
        tasks[task.name] = [{"repo": c.name, **state(c)} for c in sorted(task.iterdir()) if (c / ".git").exists()]
    return {"main": mains, "tasks": tasks}


def sync(root: Path, only: list[str]) -> None:
    repos = manifest(root)
    for repo in only or repos:
        if repo not in repos:
            raise WsError(f"{repo} isn't in workspace.toml")
        path = folder(root, repo)
        if (path / ".git").exists():
            git("fetch", "--quiet", "--prune", "origin", cwd=path)
            print(f"{repo}: fetched")
        else:
            git("clone", "--quiet", repos[repo]["url"], str(path))
            if (path / ".gitmodules").exists():
                git("submodule", "update", "--init", "--quiet", cwd=path)
            print(f"{repo}: cloned")
        set_identity(root, path)


def new(root: Path, task: str, repos: list[str], branch: str | None, start: str | None) -> None:
    if not _NAME.fullmatch(task):
        raise WsError(f"not a task name: {task}")
    known = manifest(root)
    branch = branch or task
    for repo in repos:
        if repo not in known:
            raise WsError(f"{repo} isn't in workspace.toml")
        if not (folder(root, repo) / ".git").exists():
            raise WsError(f"no main checkout of {repo}: run ws sync {repo}")
    for repo in repos:
        main, into = folder(root, repo), root / "work" / task / repo.lstrip(".")
        if into.exists():
            raise WsError(f"{into} already exists")
        git("fetch", "--quiet", "origin", cwd=main)
        exists = git("rev-parse", "--verify", "--quiet", f"refs/heads/{branch}", cwd=main, check=False)
        remote = git("rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{branch}", cwd=main, check=False)
        into.parent.mkdir(parents=True, exist_ok=True)
        if exists or remote:
            git("worktree", "add", "--quiet", str(into), branch, cwd=main)
        else:
            base = start or git("symbolic-ref", "--short", "refs/remotes/origin/HEAD", cwd=main)
            git("worktree", "add", "--quiet", "-b", branch, "--no-track", str(into), base, cwd=main)
        if (into / ".gitmodules").exists():
            git("submodule", "update", "--init", "--quiet", cwd=into)
        print(into)


def _bytes(n: int) -> str:
    return f"{n} B" if n < 1024 else f"{n // 1024} KiB" if n < 1 << 20 else f"{n / (1 << 20):.1f} MiB"


def done(root: Path, task: str, drop_ignored: bool = False) -> None:
    if not _NAME.fullmatch(task):
        raise WsError(f"not a task name: {task}")
    directory = root / "work" / task
    if not directory.is_dir():
        raise WsError(f"no task {task}")
    checkouts = [c for c in sorted(directory.iterdir()) if (c / ".git").exists()]
    held = [s for s in (state(c) for c in checkouts) if s["dirty"] or s["unpushed"]]
    if held:
        lines = "\n".join(f"  {s['path']}: {s['dirty']} uncommitted, {s['unpushed']} unpushed" for s in held)
        raise WsError(f"not removing {task}, it holds work that isn't on GitHub:\n{lines}")
    files = sorted((item for c in checkouts for item in ignored(c)), key=lambda item: -item[1])
    total = sum(size for _, size in files)
    if files:
        lines = [f"  {_bytes(size):>10}  {path}" for path, size in files[:SHOWN_IGNORED]]
        if len(files) > SHOWN_IGNORED:
            lines.append(f"  and {len(files) - SHOWN_IGNORED} more")
        print(f"{task}: {len(files)} ignored files, {_bytes(total)}, go with the worktree:\n" + "\n".join(lines))
    if total > MAX_IGNORED and not drop_ignored:
        raise WsError(f"not removing {task}: {_bytes(total)} of ignored files is past the limit of {_bytes(MAX_IGNORED)}. "
                      "Keep what matters (art add), or --drop-ignored if they can all go")
    for c in checkouts:
        # git won't remove a worktree with a submodule unless forced. The check above is what guards it.
        git("worktree", "remove", "--force", str(c), cwd=c)
    left = sorted(p.name for p in directory.iterdir())
    if left:
        print(f"left in {directory}, not part of a worktree: {', '.join(left)}")
    else:
        directory.rmdir()
    print(f"{task}: removed")


def show(report: dict) -> None:
    for m in report["main"]:
        note = "" if not (m["dirty"] or m["unpushed"]) else f"  {m['dirty']} uncommitted, {m['unpushed']} unpushed"
        print(f"{m['repo']:32} {m['branch']}{note}")
    for task, checkouts in report["tasks"].items():
        print(f"\nwork/{task}")
        for c in checkouts:
            print(f"  {c['repo']:30} {c['branch']}  {c['dirty']} uncommitted, {c['unpushed']} unpushed")


def main(argv=None, root: Path = ROOT) -> int:
    p = argparse.ArgumentParser(prog="ws", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("sync").add_argument("repos", nargs="*")
    sub.add_parser("status").add_argument("--json", action="store_true")
    n = sub.add_parser("new")
    n.add_argument("task")
    n.add_argument("repos", nargs="+")
    n.add_argument("--branch")
    n.add_argument("--from", dest="start", help="start a new branch here instead of the default branch")
    d = sub.add_parser("done")
    d.add_argument("task")
    d.add_argument("--drop-ignored", action="store_true", help="remove them even past the limit of ignored files")
    a = p.parse_args(argv)
    try:
        if a.command == "sync":
            sync(root, a.repos)
        elif a.command == "status":
            report = status(root)
            print(json.dumps(report, indent=2)) if a.json else show(report)
        elif a.command == "new":
            new(root, a.task, a.repos, a.branch, a.start)
        else:
            done(root, a.task, a.drop_ignored)
    except WsError as e:
        print(f"ws: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
