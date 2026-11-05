# SPDX-License-Identifier: GPL-2.0-or-later
"""Run the tools for real, against a workspace in a temporary folder."""
import os
import subprocess
import sys
from pathlib import Path

import pytest

TOOLS = Path(__file__).parents[1] / "tools"
sys.path.insert(0, str(TOOLS))


class Workspace:
    """A temporary ~/GlassineOS. The reference search reads only the places a test injects."""

    def __init__(self, tmp_path, monkeypatch):
        self.tmp = tmp_path
        self.home = tmp_path / "GlassineOS"
        self.home.mkdir()
        self.artifacts = self.home / "artifacts"
        self.old_tree = tmp_path / "old"
        self.old_tree.mkdir()
        self.checkout = self.home / "work" / "task" / "repo"
        self.checkout.mkdir(parents=True)
        (self.checkout / ".git").write_text("gitdir: elsewhere\n")
        self.user_home = tmp_path / "user"
        self.user_home.mkdir()
        monkeypatch.setenv("GLASSINE_WORKSPACE", str(self.home))
        monkeypatch.setenv("GLASSINE_ART_PLACES", os.pathsep.join(["work/*/*", "artifacts", str(self.old_tree)]))
        monkeypatch.setenv("HOME", str(self.user_home))

    def tool(self, name, *args, code=0):
        done = subprocess.run([sys.executable, str(TOOLS / name), *map(str, args)], capture_output=True, text=True,
                              cwd=self.tmp)
        assert done.returncode == code, done.stdout + done.stderr
        return done

    def art(self, *args, code=0):
        return self.tool("art", *args, code=code)

    def file(self, name="thing.bin", content="content\n"):
        path = self.tmp / name
        path.write_text(content)
        return path

    def add(self, name="thing.bin", *more, content=None, keep="2000-01-01"):
        path = self.file(name, content if content is not None else f"content of {name}\n")
        self.art("add", path, "--to", "bases", "--class", "derived", "--what", "a test file", "--keep", keep, *more)
        return path


@pytest.fixture
def ws(tmp_path, monkeypatch):
    return Workspace(tmp_path, monkeypatch)
