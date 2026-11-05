# SPDX-License-Identifier: GPL-2.0-or-later
"""Everything runs against a temporary workspace, with made-up sources. Flagged strings are built
at run time, so this file itself passes the scan."""
import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

import notesindex
import noteslib
import wslib

TOOLS = Path(__file__).parents[1] / "tools"
SCAN = TOOLS.parent / ".glassine/org/scripts/safety-scan.sh"
TODAY = date.today().isoformat()
ADDRESS = ":".join(["12", "34", "56", "78", "9a", "bc"])
ZERO_ADDRESS = ":".join(["00"] * 6)
HOME_PATH = "/" + "home/someone/file"
EVIDENCE = "base=b1, cmd=run it"


@pytest.fixture
def nb(ws):
    if not SCAN.exists():
        pytest.fail("no safety scan to test against: git submodule update --init")
    (ws.home / "workspace.toml").write_text('[identity]\nname = "N"\nemail = "n@example.org"\n')
    return Notes(ws)


class Notes:
    def __init__(self, ws):
        self.ws = ws
        self.root = ws.home / "notes"
        self.file = self.root / "a" / "t.md"

    def run(self, *args, code=0):
        return self.ws.tool("notes", *args, code=code)

    def add(self, words, *more, code=0):
        return self.run("add", "a/t", words, *more, code=code)

    def set(self, *more, code=0):
        return self.run("set", "a/t", *more, code=code)

    def seen(self, words):
        return self.set(words, "--seen", EVIDENCE).stdout.split()[1]

    def text(self):
        return self.file.read_text()

    def section(self, name):
        return re.split(r"^## ", self.text().split(f"## {name}\n")[1], flags=re.M)[0]

    def git(self, *args, code=0):
        done = subprocess.run(["git", *args], cwd=self.root, capture_output=True, text=True)
        assert done.returncode == code, done.stdout + done.stderr
        return done.stdout

    def commits(self):
        return int(self.git("rev-list", "--count", "HEAD"))

    def hand_commit(self, text, code=0):
        self.file.parent.mkdir(exist_ok=True, parents=True)
        self.file.write_text(text)
        self.git("add", "a/t.md")
        done = subprocess.run(["git", "commit", "-q", "-m", "by hand"], cwd=self.root, capture_output=True, text=True)
        assert done.returncode == code, done.stdout + done.stderr
        return done.stderr

    def sources(self, *entries):
        with open(self.ws.home / "workspace.toml", "a") as f:
            for name, path, kind in entries:
                f.write(f'\n[[notes.source]]\nname = "{name}"\npath = "{path}"\nkind = "{kind}"\n')

    def find(self, *words):
        out = self.run("find", *words, "--json").stdout
        return [json.loads(line) for line in out.splitlines()]


def page(current="", open_="", log=""):
    return f"# a/t\n\n## Current\n{current}## Open\n{open_}## Log\n{log}"


# Writing


def test_first_add_makes_a_private_local_repository_and_commits(nb):
    nb.add("Tried X, stopped because Y.")
    assert stat.S_IMODE(nb.root.stat().st_mode) == 0o700
    assert stat.S_IMODE(nb.file.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(nb.file.stat().st_mode) == 0o600
    assert nb.section("Log") == f"### {TODAY}\n- Tried X, stopped because Y.\n"
    assert nb.git("remote") == ""
    assert nb.git("status", "--porcelain") == ""
    assert nb.git("config", "user.email").strip() == "n@example.org"
    assert nb.commits() == 2


def test_a_new_day_opens_a_new_heading_at_the_end_of_the_log(nb):
    nb.add("start")
    nb.hand_commit(page(log="### 2000-01-01\n- Long ago\n"))
    nb.add("Today")
    assert nb.section("Log") == f"### 2000-01-01\n- Long ago\n### {TODAY}\n- Today\n"
    nb.run("lint")


def test_commit_messages_hold_no_note_text(nb):
    nb.add("secretword in the note")
    assert "secretword" not in nb.git("log", "--format=%B")


def test_notes_never_commit_to_the_workspace_around_them(nb):
    subprocess.run(["git", "init", "-q"], cwd=nb.ws.home, check=True)
    nb.add("words")
    done = subprocess.run(["git", "rev-list", "--all"], cwd=nb.ws.home, capture_output=True, text=True)
    assert done.stdout == ""
    assert nb.commits() == 2


def test_add_gives_claims_ids_and_open_items_go_to_open(nb):
    assert nb.add("A theory", "--guess").stdout.split() == ["a/t", "c1"]
    assert nb.add("Another?", "--guess", "--open").stdout.split() == ["a/t", "c2"]
    assert "- c1 [guess] A theory\n" in nb.section("Log")
    assert nb.section("Open") == "- c2 [guess] Another?\n"
    assert nb.add("Old words", "--archive", "--open").stdout.split() == ["a/t", "c3"]
    assert "c3 [archive] Old words" in nb.section("Open")


def test_open_item_needs_provenance(nb):
    assert "needs provenance" in nb.add("Why?", "--open", code=1).stderr


@pytest.mark.parametrize("topic", ["t", "a/b/c", "../x/y", "a/../b", "/a/b"])
def test_a_topic_is_area_and_name(nb, topic):
    assert "AREA/TOPIC" in nb.run("add", topic, "words", code=1).stderr


def test_seen_needs_a_base_and_something_behind_it_even_in_the_log(nb):
    assert "needs a base" in nb.add("x", "--seen", "base=b1", code=1).stderr
    assert "needs a base" in nb.add("x", "--seen", "cmd=run", code=1).stderr
    assert "needs a base" in nb.add("x", "--seen", "", code=1).stderr


def test_set_into_current_with_each_allowed_provenance(nb):
    assert nb.seen("Seen it") == "c1"
    nb.set("Told so", "--user")
    nb.set("So it follows", "--inferred", "c1,c2")
    assert nb.section("Current") == (f"- c1 [seen: {EVIDENCE}] Seen it\n- c2 [user] Told so\n"
                                     "- c3 [inferred: c1,c2] So it follows\n")


def test_a_command_in_the_evidence_may_hold_brackets_and_commas(nb):
    nb.set("Found", "--seen", "base=b1, cmd=grep [a-z], x")
    assert nb.find("found")[0]["label"] == "c1 [seen: base=b1, cmd=grep [a-z], x]"


def test_a_guess_or_archive_claim_cant_enter_current_and_nothing_changes(nb):
    nb.add("first")
    before, commits = nb.text(), nb.commits()
    assert "stays in Open or Log" in nb.set("A guess", "--guess", code=1).stderr
    assert "stays in Open or Log" in nb.set("Old claim", "--archive", code=1).stderr
    assert nb.text() == before and nb.commits() == commits


def test_set_without_provenance_refuses(nb):
    assert "provenance" in nb.set("Words", code=1).stderr


def test_an_inference_must_rest_on_current(nb):
    nb.add("A guess", "--guess")
    assert "isn't in Current" in nb.set("So", "--inferred", "c1", code=1).stderr
    assert "isn't in Current" in nb.set("So", "--inferred", "the datasheet", code=1).stderr
    assert "no claim here" in nb.set("So", "--inferred", "c99", code=1).stderr


def test_seen_with_an_artifact_must_name_one_that_exists(nb):
    assert "isn't in artifacts" in nb.set("X", "--seen", "base=b1, art=missing.bin", code=1).stderr
    nb.ws.add("real.bin")
    nb.set("X", "--seen", "base=b1, art=real.bin")


def test_the_doc_s_example_lints_clean(nb):
    nb.ws.add("NAME")
    assert noteslib.lint(f"""## Current
- c12 [seen: base=ID, art=NAME] Faults stop with the fix: 0 in the run named there.
- c14 [inferred: c12] The earlier hangs share that cause.
## Open
- [guess] Same fault in the other ring? Owner: kernel and GPU work.
## Log
### {TODAY}
- c9 [guess] The preemption path. RETRACTED {TODAY} by c12: gone with preemption still on.
- Tried X, stopped because Y.
""") == []


@pytest.mark.parametrize("text, words", [
    (page("- c1 [guess] x\n"), "stays in Open or Log"),
    (page("- c1 [archive: old] x\n"), "stays in Open or Log"),
    (page(f"- c1 [seen] x\n"), "needs a base"),
    (page("- c1 [inferred: c2] x\n- c2 [inferred: c1] y\n"), "comes back to itself"),
    (page("- c1 [inferred: c1] x\n"), "comes back to itself"),
    (page("- c2 [user] x\n", log=f"### {TODAY}\n- c1 [user] y RETRACTED {TODAY} by c2: z\n").replace("c2 [user] x", "c3 [inferred: c1] x"),
     "isn't in Current"),
    (page(f"- c1 [user] x RETRACTED {TODAY} by c1: y\n"), "can't stay in Current"),
    (page("- [user] x\n"), "needs an id"),
    (page("- bare claim\n"), "needs provenance"),
    (page("A paragraph of fact.\n"), "only claims and headings"),
    (page("- c1 [vibes] x\n"), "not vibes"),
    (page("- c1 [user x\n"), "never closes"),
    (page("- c1 [user]\n"), "says nothing"),
    (page("- c1 x\n"), "no provenance"),
    (page("- c1 [user] x\n- c1 [user] y\n"), "used twice"),
    (page("- c1 [user] x RETRACTED\n"), "a retraction reads"),
    (page(log="- not under a date\n"), "dated heading"),
    (page(log="### soon\n- x\n"), "a date as YYYY-MM-DD"),
    ("## Current\n## Log\n", "Open is missing"),
    ("## Log\n## Open\n## Current\n", "out of order"),
    (page() + "## Extra\n", "not Extra"),
    ("- c1 [user] x\n" + page(), "outside Current"),
    (page(open_="- no tag?\n"), "needs provenance"),
])
def test_lint_refuses(nb, text, words):
    problems = noteslib.lint(text)
    assert any(words in said for _, said in problems), problems


def test_lint_command_reports_and_exits_1_and_doesnt_touch_a_thing(nb):
    nb.add("words")
    nb.file.write_text(page("- c1 [guess] x\n"))
    done = nb.run("lint", code=1)
    assert done.stdout.startswith("a/t.md:4: c1 is guess")
    nb.run("lint", "a/other", code=1)
    assert nb.file.read_text() == page("- c1 [guess] x\n")
    nb.file.write_text(page("- c1 [user] x\n"))
    assert nb.run("lint", "a/t").stdout == ""


def test_a_hand_broken_topic_blocks_the_next_write_in_words(nb):
    nb.add("words")
    nb.file.write_text(page("- c1 [guess] x\n"))
    assert "stays in Open or Log" in nb.add("more", code=1).stderr
    assert nb.file.read_text() == page("- c1 [guess] x\n")


def test_a_topic_missing_its_sections_refuses_in_words(nb):
    nb.add("words")
    nb.file.write_text("# a/t\n")
    for done in (nb.set("A fact", "--user", code=1), nb.add("Why?", "--guess", "--open", code=1)):
        assert "is missing" in done.stderr and "Traceback" not in done.stderr
    assert nb.file.read_text() == "# a/t\n"


def test_lint_and_find_create_nothing(nb):
    assert "0 topics" in nb.run("lint").stderr
    assert nb.find("anything") == []
    assert not nb.root.exists()


def test_a_note_in_the_wrong_place_is_a_problem(nb):
    nb.add("words")
    (nb.root / "loose.md").write_text(page())
    assert "AREA/TOPIC" in nb.run("lint", code=1).stdout


# Replacing, retracting, closing: nothing is deleted


def test_replace_stays_in_place_and_keeps_the_old_words_in_the_log(nb):
    nb.seen("One")
    nb.seen("Two")
    nb.seen("Three")
    assert nb.set("Two, better", "--user", "--replaces", "c2").stdout.split() == ["a/t", "c4"]
    assert nb.section("Current") == f"- c1 [seen: {EVIDENCE}] One\n- c4 [user] Two, better\n- c3 [seen: {EVIDENCE}] Three\n"
    assert f"- c2 [seen: {EVIDENCE}] Two REPLACED {TODAY} by c4.\n" in nb.section("Log")
    nb.run("lint")


def test_a_log_claim_is_promoted_when_seen_again_and_marked_where_it_stays(nb):
    nb.add("Old claim", "--archive", "doc.md")
    nb.set("Old claim", "--seen", EVIDENCE, "--replaces", "c1")
    assert f"- c2 [seen: {EVIDENCE}] Old claim" in nb.section("Current")
    assert f"- c1 [archive: doc.md] Old claim REPLACED {TODAY} by c2." in nb.section("Log")


def test_replacing_takes_out_what_rested_on_the_old_claim(nb):
    nb.seen("One")
    nb.set("So", "--inferred", "c1")
    assert "out of Current with it: c2" in nb.set("One, new", "--user", "--replaces", "c1").stdout
    assert f"- c2 [inferred: c1] So OUT {TODAY}: rested on c1." in nb.section("Log")


def test_retraction_takes_out_what_rests_on_it_all_the_way_down(nb):
    nb.seen("Base")
    nb.seen("Other")
    nb.set("Direct", "--inferred", "c1")
    nb.set("Indirect", "--inferred", "c3")
    nb.set("Unrelated", "--inferred", "c2")
    nb.seen("Shows it wrong")
    done = nb.set("--retract", "c1", "--by", "c6", "--why", "gone with it still on")
    assert "out of Current with it: c3 c4" in done.stdout
    assert nb.section("Current") == (f"- c2 [seen: {EVIDENCE}] Other\n- c5 [inferred: c2] Unrelated\n"
                                     f"- c6 [seen: {EVIDENCE}] Shows it wrong\n")
    log = nb.section("Log")
    assert f"- c1 [seen: {EVIDENCE}] Base RETRACTED {TODAY} by c6: gone with it still on\n" in log
    assert f"- c3 [inferred: c1] Direct OUT {TODAY}: rested on c1.\n" in log
    assert f"- c4 [inferred: c3] Indirect OUT {TODAY}: rested on c1.\n" in log
    nb.run("lint")


def test_a_retraction_in_the_log_stays_where_it_is(nb):
    nb.add("A theory", "--guess")
    nb.add("Later line")
    nb.seen("Fact")
    nb.set("--retract", "c1", "--by", "c2", "--why", "no")
    assert nb.section("Log") == f"### {TODAY}\n- c1 [guess] A theory RETRACTED {TODAY} by c2: no\n- Later line\n"


@pytest.mark.parametrize("args, words", [
    (("--retract", "c1", "--by", "c9", "--why", "x"), "no claim c9"),
    (("--retract", "c9", "--by", "c1", "--why", "x"), "no claim c9"),
    (("--retract", "c1", "--by", "c1", "--why", "x"), "can't retract itself"),
    (("--retract", "c1", "--why", "x"), "needs --by"),
    (("--retract", "c1", "--by", "c1"), "needs --by"),
    (("--close", "c1", "--why", "x"), "isn't in Open"),
    (("--close", "c1",), "needs --why"),
])
def test_retract_and_close_refuse(nb, args, words):
    nb.seen("Fact")
    before = nb.text()
    assert words in nb.set(*args, code=1).stderr
    assert nb.text() == before


def test_retracted_twice_or_replaced_after_refuses(nb):
    nb.seen("Fact")
    nb.seen("Other")
    nb.set("--retract", "c1", "--by", "c2", "--why", "x")
    assert "already retracted" in nb.set("--retract", "c1", "--by", "c2", "--why", "x", code=1).stderr
    assert "already retracted" in nb.set("New", "--user", "--replaces", "c1", code=1).stderr


def test_a_guess_cant_retract_anything(nb):
    nb.seen("Fact")
    nb.add("A guess against it", "--guess", "--open")
    before = nb.text()
    assert "isn't in Current" in nb.set("--retract", "c1", "--by", "c2", "--why", "x", code=1).stderr
    assert nb.text() == before


def test_close_moves_an_open_item_to_the_log(nb):
    nb.add("Why?", "--guess", "--open")
    nb.set("--close", "c1", "--why", "no longer asked")
    assert nb.section("Open") == ""
    assert f"- c1 [guess] Why? CLOSED {TODAY}: no longer asked\n" in nb.section("Log")


def test_ids_never_come_back(nb):
    nb.seen("One")
    nb.set("Two", "--user", "--replaces", "c1")
    assert nb.set("Three", "--user").stdout.split() == ["a/t", "c3"]


def test_hand_wrapped_claims_are_moved_whole(nb):
    nb.add("start")
    nb.hand_commit(page("- c1 [user] first line\n  second line\n- c2 [user] other\n"))
    nb.set("--retract", "c1", "--by", "c2", "--why", "x")
    assert f"- c1 [user] first line second line RETRACTED {TODAY} by c2: x\n" in nb.section("Log")
    assert nb.section("Current") == "- c2 [user] other\n"


# The scan


@pytest.mark.parametrize("flagged", [ADDRESS, HOME_PATH])
def test_the_scan_refuses_a_new_note_and_leaves_no_trace(nb, flagged):
    done = nb.add(f"found at {flagged} today", code=1)
    assert "nothing was written" in done.stderr and "safety scan refused" in done.stderr
    assert flagged not in done.stderr
    assert not nb.file.exists()
    assert nb.git("status", "--porcelain") == ""
    assert nb.git("fsck", "--unreachable", "--no-reflogs") == ""


def test_the_scan_refusal_restores_an_existing_note(nb):
    nb.add("clean")
    before, commits = nb.text(), nb.commits()
    nb.add(f"at {ADDRESS}", code=1)
    nb.set(f"at {HOME_PATH}", "--user", code=1)
    assert nb.text() == before and nb.commits() == commits
    assert nb.git("status", "--porcelain") == ""


def test_an_allowed_address_doesnt_hide_a_real_one(nb):
    """The org scan greps only the first address-shaped line, so this one needs the tool's own check."""
    nb.add(f"zero is fine: {ZERO_ADDRESS}")
    done = nb.add(f"this is not: {ADDRESS}", code=1)
    assert "an address at line" in done.stderr


def test_a_real_address_on_a_line_with_an_allowed_one_is_refused(nb):
    assert "an address" in nb.add(f"{ZERO_ADDRESS} and {ADDRESS}", code=1).stderr


def test_no_scan_to_run_means_nothing_goes_in(nb, tmp_path):
    bare = tmp_path / "elsewhere" / "tools"
    shutil.copytree(TOOLS, bare, ignore=shutil.ignore_patterns("__pycache__"))
    done = subprocess.run([sys.executable, str(bare / "notes"), "add", "a/t", "words"], capture_output=True, text=True,
                          cwd=tmp_path)
    assert done.returncode == 1 and "safety scan isn't at" in done.stderr
    assert not nb.file.exists()


def test_a_broken_scan_refuses_too(nb):
    script = nb.ws.home / ".glassine/org/scripts/safety-scan.sh"
    script.parent.mkdir(parents=True)
    script.write_text("#!/bin/sh\necho broken >&2\nexit 2\n")
    script.chmod(0o755)
    assert "broken" in nb.add("words", code=1).stderr
    assert not nb.file.exists()


def test_the_workspace_s_own_scan_is_preferred(nb):
    script = nb.ws.home / ".glassine/org/scripts/safety-scan.sh"
    script.parent.mkdir(parents=True)
    script.write_text('#!/bin/sh\necho "FLAG  x  (the home copy ran)"\nexit 1\n')
    script.chmod(0o755)
    assert "the home copy ran" in nb.add("words", code=1).stderr


# The hook


@pytest.mark.parametrize("flagged", [ADDRESS, HOME_PATH])
def test_the_hook_refuses_flagged_hand_edits(nb, flagged):
    nb.add("start")
    commits = nb.commits()
    nb.hand_commit(page(log=f"### {TODAY}\n- at {flagged}\n"), code=1)
    assert nb.commits() == commits


def test_the_hook_finds_a_real_address_behind_an_allowed_one(nb):
    nb.add("start")
    said = nb.hand_commit(page(log=f"### {TODAY}\n- {ZERO_ADDRESS}\n- {ADDRESS}\n"), code=1)
    assert "an address at line" in said


def test_the_hook_refuses_a_hand_staged_binary_in_words(nb):
    nb.add("start")
    (nb.root / "a" / "dump.bin").write_bytes(b"\xff\xfe\x00\xff")
    nb.git("add", "a/dump.bin")
    done = subprocess.run(["git", "commit", "-q", "-m", "x"], cwd=nb.root, capture_output=True, text=True)
    assert done.returncode == 1 and "dump.bin" in done.stderr and "Traceback" not in done.stderr


def test_the_hook_lints_hand_edits_and_lets_clean_ones_through(nb):
    nb.add("start")
    commits = nb.commits()
    assert "stays in Open or Log" in nb.hand_commit(page("- c1 [guess] x\n"), code=1)
    assert nb.commits() == commits
    nb.hand_commit(page("- c1 [user] x\n"))
    assert nb.commits() == commits + 1


def test_a_global_hooks_path_doesnt_skip_the_hook(nb):
    other = nb.ws.tmp / "other-hooks"
    other.mkdir()
    (nb.ws.user_home / ".gitconfig").write_text(f"[core]\n\thooksPath = {other}\n")
    nb.add("start")
    nb.hand_commit(page(log=f"### {TODAY}\n- at {ADDRESS}\n"), code=1)


def test_a_hook_with_no_tool_refuses(nb):
    nb.add("start")
    hook = nb.root / ".git/hooks/pre-commit"
    hook.write_text(hook.read_text().replace(str(TOOLS / "notes"), "/nowhere/notes"))
    assert "nothing can scan" in nb.hand_commit(page(), code=1)


# The lock


def test_sessions_cant_race(nb):
    workers = [subprocess.Popen([sys.executable, str(TOOLS / "notes"), "add", "a/t", f"claim {n}", "--guess"],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for n in range(8)]
    said = [w.communicate() for w in workers]
    assert all(w.returncode == 0 for w in workers), said
    ids = sorted(out.split()[1] for out, _ in said)
    assert ids == sorted(f"c{n}" for n in range(1, 9))
    assert all(f"claim {n}" in nb.text() for n in range(8))
    assert nb.commits() == 9
    nb.run("lint")


# The search


def test_every_row_says_what_it_is_and_current_ranks_first(nb):
    nb.add("kumquat theory", "--guess")
    nb.seen("kumquat disproof")
    nb.add("kumquat question", "--guess", "--open")
    nb.add("kumquat old claim", "--archive")
    nb.add("kumquat tried")
    nb.set("--retract", "c1", "--by", "c2", "--why", "gone")
    rows = nb.find("kumquat")
    assert [r["state"] for r in rows] == ["current", "open", "history", "retracted", "archive, unreviewed"]
    assert rows[0] == {"state": "current", "label": f"c2 [seen: {EVIDENCE}]", "where": "a/t:4",
                       "text": "kumquat disproof"}
    assert "RETRACTED" in rows[3]["text"]


def test_plain_output_shows_the_state_in_words(nb):
    nb.seen("kumquat fact")
    done = nb.run("find", "kumquat")
    assert done.stdout.startswith("current  a/t:4  c1 ")
    assert "1 rows from notes" in done.stderr


def test_a_current_row_outranks_a_better_textual_match_from_the_archive(nb, tmp_path):
    (tmp_path / "old").mkdir(exist_ok=True)
    (tmp_path / "old" / "d.md").write_text("kumquat kumquat kumquat\n")
    nb.sources(("docs", str(tmp_path / "old"), "text"))
    nb.seen("a long sentence with many words that mentions kumquat once among all the others here")
    assert [r["state"] for r in nb.find("kumquat")] == ["current", "archive, unreviewed"]


def test_retracted_rows_come_before_the_archive_sources(nb, tmp_path):
    (tmp_path / "old").mkdir(exist_ok=True)
    (tmp_path / "old" / "d.md").write_text("\n\n".join(["kumquat"] * 30))
    nb.sources(("docs", str(tmp_path / "old"), "text"))
    nb.add("kumquat theory", "--guess")
    nb.seen("Fact")
    nb.set("--retract", "c1", "--by", "c2", "--why", "gone")
    done = nb.run("find", "kumquat", "--limit", "3", "--json")
    assert json.loads(done.stdout.splitlines()[0])["state"] == "retracted"
    assert "31 rows" in done.stderr and "--limit" in done.stderr


def test_a_hand_edited_guess_in_current_doesnt_read_as_current(nb):
    nb.add("start")
    nb.file.write_text(page("- c1 [guess] kumquat\n"))
    assert nb.find("kumquat")[0]["state"] == "open"


def test_text_sources_are_found_labeled_with_file_and_line(nb, tmp_path):
    docs = tmp_path / "old" / "docs"
    (docs / "sub").mkdir(parents=True)
    (docs / "sub" / "doc.md").write_text("# Heading\n\nfirst para\n\nThe kumquat crashes\nsecond line\n")
    (docs / "skip.txt").write_text("kumquat in the wrong kind of file\n")
    nb.sources(("old-docs", str(docs), "text"))
    [row] = nb.find("kumquat")
    assert row["state"] == "archive, unreviewed"
    assert row["where"] == f"{docs / 'sub' / 'doc.md'}:5"
    assert "The kumquat crashes second line" in row["text"]


def test_a_source_can_set_its_own_glob_and_a_relative_path(nb):
    (nb.ws.home / "old").mkdir()
    (nb.ws.home / "old" / "x.txt").write_text("kumquat\n")
    with open(nb.ws.home / "workspace.toml", "a") as f:
        f.write('\n[[notes.source]]\nname = "o"\npath = "old"\nkind = "text"\nglob = "*.txt"\n')
    assert len(nb.find("kumquat")) == 1


def test_stemming_finds_other_forms_and_words_needn_t_be_adjacent(nb):
    nb.seen("The kernel crashed on resume")
    assert nb.find("crash", "kernel")
    assert not nb.find("crash", "missingword")


def transcript(*lines):
    return "\n".join(json.dumps(line) if not isinstance(line, str) else line for line in lines) + "\n"


def test_transcripts_are_read_by_message(nb, tmp_path):
    folder = tmp_path / "chats" / "proj"
    folder.mkdir(parents=True)
    (folder / "s.jsonl").write_text(transcript(
        {"type": "user", "message": {"role": "user", "content": "plain kumquat question"}},
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "answer lychee"},
            {"type": "thinking", "thinking": "private durian"},
            {"type": "tool_use", "name": "Bash", "input": {"command": "run mango"}},
            {"type": "image", "source": {"data": "feijoa"}}]}},
        {"type": "user", "message": {"content": [{"type": "tool_result", "content": [{"type": "text", "text": "got papaya"}]}]}},
        {"type": "summary", "summary": "session about guava"},
        {"type": "progress", "data": "tamarind"},
        "not json at all pomelo",
        "[1, 2]",
    ))
    nb.sources(("chats", str(tmp_path / "chats"), "transcripts"))
    for word, line in [("kumquat", 1), ("lychee", 2), ("mango", 2), ("papaya", 3), ("guava", 4), ("pomelo", 6)]:
        [row] = nb.find(word)
        assert row["state"] == "archive, unreviewed" and row["where"].endswith(f"s.jsonl:{line}"), word
    for word in ("durian", "feijoa", "tamarind"):
        assert nb.find(word) == [], word


def test_a_long_message_is_found_past_the_first_chunk_with_a_short_row(nb, tmp_path):
    (tmp_path / "c").mkdir()
    (tmp_path / "c" / "s.jsonl").write_text(transcript({"message": {"content": "filler " * 2000 + "kumquat"}}))
    nb.sources(("c", str(tmp_path / "c"), "transcripts"))
    [row] = nb.find("kumquat")
    assert len(row["text"]) < 1000


def test_the_index_follows_changes_and_removals(nb, tmp_path):
    old = tmp_path / "old"
    old.mkdir(exist_ok=True)
    doc = old / "d.md"
    doc.write_text("kumquat\n")
    nb.sources(("docs", str(old), "text"))
    assert len(nb.find("kumquat")) == 1
    doc.write_text("lychee now\n")
    assert nb.find("kumquat") == [] and len(nb.find("lychee")) == 1
    doc.unlink()
    assert nb.find("lychee") == []
    nb.seen("A durian fact")
    assert nb.find("durian")[0]["state"] == "current"
    nb.set("--retract", "c1", "--by", "c1", "--why", "x", code=1)
    nb.seen("Other")
    nb.set("--retract", "c1", "--by", "c2", "--why", "x")
    assert nb.find("durian")[0]["state"] == "retracted"


def test_a_source_dropped_from_the_config_leaves_the_index(nb, tmp_path):
    (tmp_path / "old").mkdir(exist_ok=True)
    (tmp_path / "old" / "d.md").write_text("kumquat\n")
    config = nb.ws.home / "workspace.toml"
    plain = config.read_text()
    nb.sources(("docs", str(tmp_path / "old"), "text"))
    assert nb.find("kumquat")
    config.write_text(plain)
    assert nb.find("kumquat") == []


def test_only_configured_sources_are_read(nb):
    (nb.ws.user_home / ".cache").mkdir()
    (nb.ws.home / "stray.md").write_text("kumquat\n")
    (nb.ws.user_home / "stray.md").write_text("kumquat\n")
    assert nb.find("kumquat") == []
    assert "rows from notes\n" in nb.run("find", "kumquat").stderr


def test_a_missing_source_says_so(nb, tmp_path):
    nb.sources(("gone", str(tmp_path / "nowhere"), "text"))
    done = nb.run("find", "kumquat")
    assert "gone:" in done.stderr and "wasn't searched" in done.stderr


def test_a_fifo_in_a_source_doesnt_block(nb, tmp_path):
    (tmp_path / "old").mkdir(exist_ok=True)
    os.mkfifo(tmp_path / "old" / "x.md")
    (tmp_path / "old" / "d.md").write_text("kumquat\n")
    nb.sources(("docs", str(tmp_path / "old"), "text"))
    assert len(nb.find("kumquat")) == 1


@pytest.mark.parametrize("entry", ['name = "x"', 'name = "notes"\npath = "p"\nkind = "text"',
                                   'name = "x"\npath = "p"\nkind = "pdf"'])
def test_a_wrong_source_stops_the_search(nb, entry):
    with open(nb.ws.home / "workspace.toml", "a") as f:
        f.write(f"\n[[notes.source]]\n{entry}\n")
    assert "[[notes.source]]" in nb.run("find", "kumquat", code=1).stderr


def test_broken_config_is_words(nb):
    (nb.ws.home / "workspace.toml").write_text("not toml [")
    assert "workspace.toml" in nb.run("find", "kumquat", code=1).stderr
    assert "Traceback" not in nb.run("find", "kumquat", code=1).stderr


@pytest.mark.parametrize("words", [["AND"], ["NEAR(a"], ['a"b'], ["fo*"], ["text:x"], ["(", "x"], ["c1"]])
def test_search_words_arent_query_syntax(nb, words):
    nb.seen("plain")
    nb.find(*words)


def test_no_usable_words_is_said(nb):
    assert "no words" in nb.run("find", "...", code=1).stderr


def test_the_index_is_private(nb):
    nb.seen("A fact")
    nb.find("fact")
    folder = nb.ws.user_home / ".cache" / "glassine" / "notes-index"
    assert stat.S_IMODE(folder.stat().st_mode) == 0o700
    assert stat.S_IMODE((folder / "index.db").stat().st_mode) == 0o600


def test_a_damaged_index_is_rebuilt(nb):
    nb.seen("A fact")
    nb.find("fact")
    db = nb.ws.user_home / ".cache" / "glassine" / "notes-index" / "index.db"
    db.write_bytes(b"not a database" * 1000)
    done = nb.run("find", "fact")
    assert done.stdout.startswith("current") and "being rebuilt" in done.stderr


def test_no_fts5_is_said_in_words(nb, monkeypatch, capsys):
    real = sqlite3.connect

    class NoFts:
        def __init__(self, *args):
            self.db = real(*args)

        def execute(self, sql, *args):
            if "fts5" in sql:
                raise sqlite3.OperationalError("no such module: fts5")
            return self.db.execute(sql, *args)

    monkeypatch.setattr(sqlite3, "connect", NoFts)
    with pytest.raises(wslib.ToolError, match="needs SQLite with FTS5"):
        notesindex.find(["word"], 5, print)


def test_help_names_every_command(nb):
    said = nb.run("--help").stdout
    assert all(word in said for word in ("notes add", "notes set", "notes lint", "notes find"))
