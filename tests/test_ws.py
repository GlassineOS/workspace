# SPDX-License-Identifier: GPL-2.0-or-later
import importlib.machinery
import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

loader = importlib.machinery.SourceFileLoader("ws", str(Path(__file__).parents[1] / "ws"))
spec = importlib.util.spec_from_loader("ws", loader)
ws = importlib.util.module_from_spec(spec)
loader.exec_module(ws)


def run(*args, cwd):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@e", *args], cwd=cwd, check=True,
                          capture_output=True, text=True).stdout.strip()


@pytest.fixture
def root(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    lines = []
    for name in ("alpha", ".github"):
        bare = tmp_path / f"{name}.git"
        seed = tmp_path / f"seed{name}"
        seed.mkdir()
        run("init", "-q", "-b", "main", cwd=seed)
        (seed / "f").write_text("x")
        run("add", "f", cwd=seed)
        run("commit", "-qm", "one", cwd=seed)
        run("clone", "-q", "--bare", str(seed), str(bare), cwd=tmp_path)
        lines.append(f'[repo."{name}"]\nurl = "{bare}"\n')
    (root / "workspace.toml").write_text('[identity]\nname = "N"\nemail = "n@example.org"\n\n' + "\n".join(lines))
    return root


def test_sync_clones_then_fetches_and_drops_the_dot(root, capsys):
    assert ws.main(["sync"], root) == 0
    assert (root / "alpha" / "f").exists() and (root / "github" / "f").exists()
    assert ws.main(["sync", "alpha"], root) == 0
    assert "fetched" in capsys.readouterr().out
    assert ws.main(["sync", "nope"], root) == 1


def test_sync_sets_the_identity_on_new_and_existing_checkouts(root):
    ws.main(["sync"], root)
    assert run("config", "--local", "user.email", cwd=root / "alpha") == "n@example.org"
    run("config", "user.email", "work@example.com", cwd=root / "alpha")
    ws.main(["sync", "alpha"], root)
    assert run("config", "--local", "user.email", cwd=root / "alpha") == "n@example.org"


def test_new_status_done(root, capsys):
    ws.main(["sync"], root)
    assert ws.main(["new", "fix", "alpha", ".github"], root) == 0
    alpha = root / "work" / "fix" / "alpha"
    assert run("rev-parse", "--abbrev-ref", "HEAD", cwd=alpha) == "fix"
    assert run("rev-parse", "--abbrev-ref", "HEAD", cwd=root / "alpha") == "main"  # the main checkout didn't move
    capsys.readouterr()
    ws.main(["status", "--json"], root)
    report = json.loads(capsys.readouterr().out)
    assert {m["repo"] for m in report["main"]} == {"alpha", ".github"}
    assert [c["repo"] for c in report["tasks"]["fix"]] == ["alpha", "github"]
    assert ws.main(["done", "fix"], root) == 0
    assert not (root / "work" / "fix").exists()


def test_done_removes_a_worktree_with_a_submodule_unless_it_holds_work(root, tmp_path, capsys):
    ws.main(["sync"], root)
    ws.main(["new", "fix", "alpha"], root)
    alpha = root / "work" / "fix" / "alpha"
    run("-c", "protocol.file.allow=always", "submodule", "add", "-q", str(tmp_path / ".github.git"), "sub", cwd=alpha)
    run("commit", "-qm", "sub", cwd=alpha)
    run("push", "-q", "origin", "fix", cwd=alpha)
    (alpha / "sub" / "f").write_text("changed")
    assert ws.main(["done", "fix"], root) == 1
    assert alpha.exists() and "isn't on GitHub" in capsys.readouterr().err
    run("checkout", "-q", "--", "f", cwd=alpha / "sub")
    assert ws.main(["done", "fix"], root) == 0
    assert not (root / "work" / "fix").exists()


def test_done_refuses_while_work_is_not_on_github(root, capsys):
    ws.main(["sync"], root)
    ws.main(["new", "fix", "alpha"], root)
    alpha = root / "work" / "fix" / "alpha"
    (alpha / "g").write_text("y")
    assert ws.main(["done", "fix"], root) == 1
    assert "1 uncommitted" in capsys.readouterr().err and alpha.exists()
    run("add", "g", cwd=alpha)
    run("commit", "-qm", "two", cwd=alpha)
    assert ws.main(["done", "fix"], root) == 1
    assert "1 unpushed" in capsys.readouterr().err
    run("push", "-q", "origin", "fix", cwd=alpha)
    assert ws.main(["done", "fix"], root) == 0


def ignored_task(root, *names, size=10):
    ws.main(["sync"], root)
    ws.main(["new", "fix", "alpha"], root)
    alpha = root / "work" / "fix" / "alpha"
    (alpha / ".gitignore").write_text("out/\n*.log\n")
    run("add", ".gitignore", cwd=alpha)
    run("commit", "-qm", "ignore", cwd=alpha)
    run("push", "-q", "origin", "fix", cwd=alpha)
    (alpha / "out").mkdir()
    for name in names:
        (alpha / name).write_bytes(b"x" * size)
    return alpha


def test_done_lists_a_few_ignored_files_and_removes_them(root, capsys):
    alpha = ignored_task(root, "out/a.bin", "b.log")
    assert ws.main(["done", "fix"], root) == 0
    out = capsys.readouterr().out
    assert "2 ignored files" in out and "out/a.bin" in out and "b.log" in out and "fix: removed" in out
    assert not alpha.exists()


def test_done_says_nothing_about_ignored_files_when_there_are_none(root, capsys):
    ignored_task(root)
    assert ws.main(["done", "fix"], root) == 0
    assert "ignored" not in capsys.readouterr().out


def test_done_refuses_past_the_limit_until_told(root, capsys):
    alpha = ignored_task(root, "out/big.bin", size=ws.MAX_IGNORED + 1)
    assert ws.main(["done", "fix"], root) == 1
    captured = capsys.readouterr()
    assert "out/big.bin" in captured.out and "--drop-ignored" in captured.err
    assert (alpha / "out" / "big.bin").exists()
    assert ws.main(["done", "fix", "--drop-ignored"], root) == 0
    assert not alpha.exists()


def test_done_counts_exactly_up_to_the_limit_as_fine(root):
    alpha = ignored_task(root, "out/big.bin", size=ws.MAX_IGNORED)
    assert ws.main(["done", "fix"], root) == 0
    assert not alpha.exists()


def test_done_adds_up_across_the_task_and_checks_before_removing_any(root, tmp_path, capsys):
    ws.main(["sync"], root)
    ws.main(["new", "fix", "alpha", ".github"], root)
    for repo in ("alpha", "github"):
        c = root / "work" / "fix" / repo
        (c / ".gitignore").write_text("*.bin\n")
        run("add", ".gitignore", cwd=c)
        run("commit", "-qm", "ignore", cwd=c)
        run("push", "-q", "origin", "fix", cwd=c)
        (c / "x.bin").write_bytes(b"x" * (ws.MAX_IGNORED // 2 + 1))
    assert ws.main(["done", "fix"], root) == 1
    assert (root / "work" / "fix" / "alpha").exists() and (root / "work" / "fix" / "github").exists()


def test_done_counts_ignored_files_in_a_submodule_and_a_nested_repository(root, tmp_path, capsys):
    alpha = ignored_task(root, size=1)
    run("-c", "protocol.file.allow=always", "submodule", "add", "-q", str(tmp_path / ".github.git"), "sub", cwd=alpha)
    run("commit", "-qm", "sub", cwd=alpha)
    run("push", "-q", "origin", "fix", cwd=alpha)
    sub = alpha / "sub"
    exclude = run("rev-parse", "--git-path", "info/exclude", cwd=sub)
    Path(sub, exclude).parent.mkdir(parents=True, exist_ok=True)
    Path(sub, exclude).write_text("*.bin\n")
    (sub / "deep.bin").write_bytes(b"x" * (ws.MAX_IGNORED + 1))
    assert ws.main(["done", "fix"], root) == 1
    assert "deep.bin" in capsys.readouterr().out
    (sub / "deep.bin").unlink()
    run("init", "-q", cwd=alpha / "out")
    (alpha / "out" / "nested.bin").write_bytes(b"x" * (ws.MAX_IGNORED + 1))
    assert ws.main(["done", "fix"], root) == 1
    assert "nested.bin" in capsys.readouterr().out


def test_done_names_what_it_leaves_in_the_task_folder(root, capsys):
    ignored_task(root)
    (root / "work" / "fix" / ".venv").mkdir()
    assert ws.main(["done", "fix"], root) == 0
    out = capsys.readouterr().out
    assert ".venv" in out and "fix: removed" in out
    assert (root / "work" / "fix" / ".venv").exists()


def test_status_and_done_cope_with_tools_and_ignored_folders_in_the_workspace(root, capsys):
    (root / "tools").mkdir()
    (root / "artifacts" / "dumps").mkdir(parents=True)
    (root / "notes" / ".git").mkdir(parents=True)
    ws.main(["sync"], root)
    ws.main(["new", "fix", "alpha"], root)
    capsys.readouterr()
    assert ws.main(["status", "--json"], root) == 0
    report = json.loads(capsys.readouterr().out)
    assert {m["repo"] for m in report["main"]} == {"alpha", ".github"} and list(report["tasks"]) == ["fix"]
    assert ws.main(["done", "fix"], root) == 0
    assert (root / "artifacts" / "dumps").exists() and (root / "notes").exists() and (root / "tools").exists()


def test_new_continues_an_existing_branch(root):
    ws.main(["sync"], root)
    run("branch", "topic", cwd=root / "alpha")
    run("push", "-q", "origin", "topic", cwd=root / "alpha")
    run("branch", "-D", "topic", cwd=root / "alpha")
    assert ws.main(["new", "t", "alpha", "--branch", "topic"], root) == 0
    assert run("rev-parse", "--abbrev-ref", "HEAD", cwd=root / "work" / "t" / "alpha") == "topic"


@pytest.mark.parametrize("args", [["new", "../x", "alpha"], ["new", "x", "nope"], ["new", "x", ".github"], ["done", "../x"], ["done", "nope"]])
def test_bad_requests_are_refused(root, args):
    ws.main(["sync", "alpha"], root)
    assert ws.main(args, root) == 1
    assert not (root / "x").exists()
