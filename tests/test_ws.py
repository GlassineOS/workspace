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
