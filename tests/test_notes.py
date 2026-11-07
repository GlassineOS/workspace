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
DOC_RANGE_IN_THE_MIDDLE = ":".join(["12", "34", "00", "00", "5e", "ab"])  # built here so the file passes the scan
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


def test_a_global_git_identity_doesnt_replace_the_one_in_workspace_toml(nb):
    (nb.ws.user_home / ".gitconfig").write_text("[user]\n\tname = Somebody Else\n\temail = else@example.org\n")
    nb.add("words")
    assert nb.git("config", "--local", "user.email").strip() == "n@example.org"
    assert nb.git("log", "-1", "--format=%an <%ae>").strip() == "N <n@example.org>"


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


@pytest.mark.parametrize("words", ["", "   ", "\t\n"])
def test_empty_words_are_refused_and_nothing_changes(nb, words):
    nb.add("start")
    before, commits = nb.text(), nb.commits()
    assert "words are empty" in nb.add(words, code=1).stderr
    assert "words are empty" in nb.add(words, "--guess", code=1).stderr
    assert ("words are empty" in nb.set(words, "--user", code=1).stderr
            or "needs words" in nb.set(words, "--user", code=1).stderr)  # "" never gets as far as the library
    assert nb.text() == before and nb.commits() == commits


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
    assert "no artifact missing.bin" in nb.set("X", "--seen", "base=b1, art=missing.bin", code=1).stderr
    nb.ws.add("real.bin")
    nb.set("X", "--seen", "base=b1, art=real.bin")


def remove_artifact(nb, name="real.bin"):
    nb.ws.art("rm", name, "--why", "room", "--retention-said", name, "typed by the person")


def test_a_removed_artifact_doesnt_lock_the_topics_that_name_it(nb):
    """A claim that was seen while the artifact was there stays, so the tool can still repair or extend the note."""
    nb.ws.add("real.bin")
    nb.set("Seen in it", "--seen", "base=b1, art=real.bin")
    nb.seen("Other fact")
    remove_artifact(nb)
    nb.add("a Log line still goes in")
    nb.set("An unrelated claim", "--user")
    nb.set("Replaced", "--user", "--replaces", "c2")
    nb.set("--retract", "c1", "--by", "c4", "--why", "the artifact was wrong")
    assert "RETRACTED" in nb.section("Log")


def test_lint_reports_a_removed_artifact_in_the_removal_s_own_words(nb):
    nb.ws.add("real.bin")
    nb.set("Seen in it", "--seen", "base=b1, art=real.bin")
    nb.run("lint")
    remove_artifact(nb)
    done = nb.run("lint", code=1)
    assert "c1 names art=real.bin" in done.stdout and "was removed" in done.stdout and "room" in done.stdout


def test_a_missing_file_is_reported_by_lint_and_blocks_no_write(nb):
    nb.ws.add("real.bin")
    nb.set("Seen in it", "--seen", "base=b1, art=real.bin")
    Path(nb.ws.art("path", "real.bin").stdout.strip()).unlink()
    assert "its file is missing" in nb.run("lint", code=1).stdout
    nb.add("still writes")


def test_a_claim_entering_current_must_still_name_an_artifact_that_is_there(nb):
    nb.ws.add("real.bin")
    nb.set("Seen in it", "--seen", "base=b1, art=real.bin")
    remove_artifact(nb)
    done = nb.set("Seen again", "--seen", "base=b1, art=real.bin", code=1)
    assert "was removed" in done.stderr
    assert "no artifact" in nb.set("Replaces it", "--seen", "base=b1, art=gone.bin", "--replaces", "c1", code=1).stderr


def test_the_hook_lets_a_hand_edit_through_a_note_whose_artifact_is_gone(nb):
    nb.ws.add("real.bin")
    nb.set("Seen in it", "--seen", "base=b1, art=real.bin")
    remove_artifact(nb)
    nb.hand_commit(nb.text() + "### 2000-01-01\n- a hand-written log line\n")
    nb.hand_commit(nb.text().replace("[seen: base=b1, art=real.bin]", "[seen: base=b1, art=other.bin]"), code=1)


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
    (page(log="- not under a date\n"), "dated heading"),
    (page(log=f"### {TODAY}\n- \n"), "nothing in it"),
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


@pytest.mark.parametrize("words", [
    "the word RETRACTED is how lint marks it",
    "old c1 was RETRACTED 2026-01-01 by c1 per the doc",
    "REPLACED is also a word, and so is OUT",
    "c5 is broken",
    "c5",
    "[wip] trying it",
    "[wip: x] trying it",
    "[user x",
])
def test_plain_log_lines_may_look_like_structure(nb, words):
    """Archive processing will put old text through this in bulk."""
    nb.add(words)
    nb.run("lint")
    assert nb.section("Log").endswith(f"- {words}\n")
    [row] = nb.find("trying" if "trying" in words else words.split()[0].strip("[]"))
    assert row["state"] == "history"
    nb.seen("a claim after it")  # the plain line takes no id


@pytest.mark.parametrize("words", [
    f"x RETRACTED {TODAY} by c1: y",
    f"RETRACTED {TODAY} by c1: y",
    f"x REPLACED {TODAY} by c2.",
])
def test_words_that_fake_a_mark_are_refused_so_a_mark_only_comes_from_the_tool(nb, words):
    nb.seen("A fact")
    before = nb.text()
    assert "shape of a mark" in nb.add(words, code=1).stderr
    assert "shape of a mark" in nb.set(words, "--user", code=1).stderr
    assert "shape of a mark" in nb.set("--retract", "c1", "--by", "c1", "--why", words, code=1).stderr
    nb.add("an open question", "--guess", "--open")
    assert "shape of a mark" in nb.set("--close", "c2", "--why", words, code=1).stderr
    assert nb.text() != before and "fake" not in nb.text()
    assert nb.find("fact")[0]["state"] == "current"


def test_a_real_mark_is_still_read_and_a_missing_provenance_still_refused(nb):
    problems = noteslib.lint(page(log=f"### {TODAY}\n- c1 [user] x RETRACTED {TODAY} by c2: z\n"))
    assert [said for _, said in problems] == ["c1 was retracted by c2, and no claim here has that id"]
    assert any("no provenance" in said for _, said in noteslib.lint(page(log=f"### {TODAY}\n- c1 [vibes] x\n")) + noteslib.lint(page("- c1 x\n")))
    assert noteslib.lint(page(log=f"### {TODAY}\n- [vibes] x\n")) == []


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


# Replacing, retracting, closing: the text stays, in Log


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


@pytest.mark.parametrize("args, dropped", [
    (("the answer", "--user", "--close", "c2", "--why", "settled"), "words, provenance"),
    (("the answer", "--close", "c2", "--why", "settled"), "words"),
    (("--user", "--close", "c2", "--why", "settled"), "provenance"),
    (("--close", "c2", "--by", "c1", "--why", "settled"), "--by"),
    (("--close", "c2", "--replaces", "c1", "--why", "settled"), "--replaces"),
    (("the answer", "--retract", "c1", "--by", "c3", "--why", "x"), "words"),
    (("--seen", EVIDENCE, "--retract", "c1", "--by", "c3", "--why", "x"), "provenance"),
    (("--open", "--retract", "c1", "--by", "c3", "--why", "x"), "--open"),
    (("--retract", "c1", "--close", "c2", "--by", "c3", "--why", "x"), "one at a time"),
    (("--delete", "c1", "--close", "c2", "--why", "x"), "one at a time"),
    (("--delete", "c1", "--by", "c3", "--close", "c2", "--why", "x"), "one at a time"),
    (("--seen", EVIDENCE, "--delete", "c1", "--why", "x"), "provenance"),
    (("the answer", "--user", "--why", "settled"), "--by and --why go with"),
    (("the answer", "--user", "--by", "c1"), "--by and --why go with"),
])
def test_words_that_would_be_dropped_refuse_the_whole_write(nb, args, dropped):
    nb.seen("Fact")
    nb.add("an open question", "--guess", "--open")
    nb.seen("Other")
    before, commits = nb.text(), nb.commits()
    assert dropped in nb.set(*args, code=1).stderr
    assert nb.text() == before and nb.commits() == commits


def test_retracted_twice_or_replaced_after_refuses(nb):
    nb.seen("Fact")
    nb.seen("Other")
    nb.set("--retract", "c1", "--by", "c2", "--why", "x")
    assert "already retracted" in nb.set("--retract", "c1", "--by", "c2", "--why", "x", code=1).stderr
    assert "already retracted" in nb.set("New", "--user", "--replaces", "c1", code=1).stderr


@pytest.mark.parametrize("basis", [",", " , ", ",,"])
def test_an_inference_must_name_something_to_rest_on(nb, basis):
    """An empty shell variable in "$A,$B" gives exactly this."""
    before = nb.add("start").stdout and nb.text()
    assert "not from what" in nb.set("Rests on nothing", "--inferred", basis, code=1).stderr
    assert "not from what" in nb.add("Rests on nothing", "--inferred", basis, code=1).stderr
    assert nb.text() == before
    assert noteslib.lint(page(current=f"- c1 [inferred: {basis}] x\n"))


def test_a_claim_cant_retract_the_claim_it_rests_on_nor_one_further_down(nb):
    nb.seen("Base")
    nb.set("Rests on it", "--inferred", "c1")
    nb.set("Rests on that", "--inferred", "c2")
    before = nb.text()
    for witness in ("c2", "c3"):
        done = nb.set("--retract", "c1", "--by", witness, "--why", "x", code=1)
        assert f"{witness} rests on c1" in done.stderr
    assert nb.text() == before


def test_an_inferred_claim_resting_on_something_else_may_still_be_the_witness(nb):
    nb.seen("Wrong")
    nb.seen("Seen")
    nb.set("From the seen one", "--inferred", "c2")
    nb.set("--retract", "c1", "--by", "c3", "--why", "follows from the seen one")
    assert "RETRACTED" in nb.section("Log") and "c2" in nb.section("Current")


def test_a_claim_that_was_replaced_cant_be_replaced_again(nb):
    """--replaces c1 twice left c2 and c3 both in Current, and c1 saying it was replaced by each."""
    nb.seen("First")
    nb.set("Second", "--user", "--replaces", "c1")
    before, commits = nb.text(), nb.commits()
    assert "c1 is already replaced, by c2" in nb.set("Third", "--user", "--replaces", "c1", code=1).stderr
    assert nb.text() == before and nb.commits() == commits
    nb.set("Third", "--user", "--replaces", "c2")  # the one that's current can be
    assert "c3" in nb.section("Current") and "c2" not in nb.section("Current")


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


# Deleting: the text goes, its id and why stay in Log


def test_a_delete_takes_the_text_out_and_what_rests_on_it_out_of_current(nb):
    nb.seen("Base")
    nb.set("Direct", "--inferred", "c1")
    nb.seen("Shows it wrong")
    done = nb.set("--delete", "c1", "--by", "c3", "--why", "contradicted by the bench")
    assert "a/t c1 deleted" in done.stdout and "out of Current with it: c2" in done.stdout
    assert "Base" not in nb.text()
    assert nb.section("Current") == f"- c3 [seen: {EVIDENCE}] Shows it wrong\n"
    log = nb.section("Log")
    assert f"- c1 DELETED {TODAY} by c3: contradicted by the bench\n" in log
    assert f"- c2 [inferred: c1] Direct OUT {TODAY}: rested on c1.\n" in log
    nb.run("lint")
    assert "Base" in nb.git("log", "-p")  # the notes' own history keeps the text


def test_the_maintainer_deletes_without_a_witness_and_the_id_never_comes_back(nb):
    nb.seen("One")
    nb.seen("Two")
    nb.set("--delete", "c2", "--why", "wrong")
    assert nb.seen("Three") == "c3"
    nb.run("lint")


@pytest.mark.parametrize("args, words", [
    (("--delete", "c1"), "needs --why"),
    (("--delete", "c9", "--why", "x"), "no claim c9"),
    (("--delete", "c1", "--by", "c1", "--why", "x"), "can't show itself wrong"),
    (("--delete", "c1", "--by", "c2", "--why", "x"), "rests on c1"),
    (("--delete", "c1", "--retract", "c1", "--why", "x"), "one at a time"),
    (("--delete", "c1", "words", "--why", "x"), "would be dropped"),
])
def test_delete_refuses(nb, args, words):
    nb.seen("Fact")
    nb.set("Rests", "--inferred", "c1")
    before = nb.text()
    assert words in nb.set(*args, code=1).stderr
    assert nb.text() == before


def test_the_tool_deletes_a_user_claim_without_a_witness_because_it_cant_tell_who_asks(nb):
    """Pinned as it is today, not as a verdict: CLAUDE.md shows `--by cM`, and the tool takes it as optional.
    Whether a session must name one, with the person's own way of leaving it out, is for the maintainer."""
    nb.set("Said by the person", "--user")
    nb.set("--delete", "c1", "--why", "wrong")
    assert nb.section("Current") == ""
    assert f"- c1 DELETED {TODAY}: wrong\n" in nb.section("Log")


def test_lint_checks_the_claim_a_deleted_line_names(nb):
    dangling = page(log=f"### {TODAY}\n- c1 DELETED {TODAY} by c99: hand\n")
    assert [said for _, said in noteslib.lint(dangling)] == ["c1 was deleted by c99, and no claim here has that id"]
    assert noteslib.lint(page("- c2 [user] x\n", log=f"### {TODAY}\n- c1 DELETED {TODAY} by c2: hand\n")) == []
    assert any("belongs in Log" in said for _, said in noteslib.lint(page(f"- c1 DELETED {TODAY}: hand\n")))
    nb.seen("Fact")
    good = nb.text()
    nb.hand_commit(good.replace("- c1 [", f"- c2 DELETED {TODAY}: x\n- c1 [", 1), code=1)  # in Current: the hook refuses it
    day = f"### {TODAY}\n"
    nb.hand_commit(good + day + f"- c2 DELETED {TODAY} by c99: hand\n", code=1)  # dangling by: the hook refuses it too
    nb.hand_commit(good + day + f"- c2 DELETED {TODAY} by c1: hand\n")


def test_a_claim_that_showed_another_wrong_cant_be_deleted_only_retracted(nb):
    nb.seen("Wrong")
    nb.seen("Shows it wrong")
    nb.seen("Other")
    nb.set("--retract", "c1", "--by", "c2", "--why", "bench")
    nb.seen("Also wrong")
    nb.set("--delete", "c4", "--by", "c3", "--why", "bench")
    before = nb.text()
    for witness in ("c2", "c3"):  # one retracted a claim, the other deleted one
        said = nb.set("--delete", witness, "--why", "wrong too", code=1).stderr
        assert f"retract {witness} instead" in said
    assert nb.text() == before
    nb.set("--retract", "c2", "--by", "c3", "--why", "wrong too")  # its words stay, so the reasons still read
    nb.run("lint")


def test_deleting_the_claim_that_was_retracted_drops_the_retraction_record_too(nb):
    """Intended: a delete takes the claim out whatever state it's in, and its line says why."""
    nb.seen("Wrong")
    nb.seen("Shows it wrong")
    nb.set("--retract", "c1", "--by", "c2", "--why", "bench")
    nb.set("--delete", "c1", "--why", "should never have been written")
    log = nb.section("Log")
    assert f"- c1 DELETED {TODAY}: should never have been written\n" in log
    assert "RETRACTED" not in log and "Wrong" not in nb.text()
    nb.run("lint")
    assert "c1 was deleted" in nb.set("--retract", "c2", "--by", "c1", "--why", "x", code=1).stderr


def test_deleting_a_replacement_leaves_the_old_claim_replaced(nb):
    """Intended, and pinned: the old claim keeps its REPLACED line, which now points at a DELETED one, and it
    can't be replaced again. The topic has no current state until a new claim is made."""
    nb.seen("Old")
    nb.set("New", "--user", "--replaces", "c1")
    nb.set("--delete", "c2", "--why", "the new one was wrong")
    assert nb.section("Current") == ""
    log = nb.section("Log")
    assert f"REPLACED {TODAY} by c2." in log and f"- c2 DELETED {TODAY}: the new one was wrong\n" in log
    nb.run("lint")
    assert "already replaced, by c2" in nb.set("Newer", "--user", "--replaces", "c1", code=1).stderr
    assert nb.set("Newer", "--user").stdout.split()[1] == "c3"


def test_deleting_the_replaced_claim_leaves_its_replacement_alone(nb):
    nb.seen("Old")
    nb.set("New", "--user", "--replaces", "c1")
    nb.set("--delete", "c1", "--why", "never was true")
    assert nb.section("Current") == "- c2 [user] New\n"
    assert f"- c1 DELETED {TODAY}: never was true\n" in nb.section("Log")
    nb.run("lint")


def test_deleting_an_open_claim_leaves_an_open_item_resting_on_it(nb):
    """Intended, as for a retraction: only Current is held to resting on Current, and the id still resolves."""
    nb.add("A theory", "--guess", "--open")
    nb.add("Follows from it", "--inferred", "c1", "--open")
    done = nb.set("--delete", "c1", "--why", "wrong question")
    assert "out of Current" not in done.stdout
    assert nb.section("Open") == "- c2 [inferred: c1] Follows from it\n"
    assert f"- c1 DELETED {TODAY}: wrong question\n" in nb.section("Log")
    nb.run("lint")


def test_a_deleted_claim_cant_be_touched_again_nor_faked(nb):
    nb.seen("Fact")
    nb.seen("Other")
    nb.set("--delete", "c1", "--why", "wrong")
    assert "c1 was deleted" in nb.set("--delete", "c1", "--why", "again", code=1).stderr
    assert "c1 was deleted" in nb.set("--retract", "c2", "--by", "c1", "--why", "x", code=1).stderr
    assert "shape of a mark" in nb.add(f"c2 DELETED {TODAY}: gone", code=1).stderr


# The scan


@pytest.mark.parametrize("flagged", [ADDRESS, HOME_PATH])
def test_the_scan_refuses_a_new_note_and_leaves_no_trace(nb, flagged):
    done = nb.add(f"found at {flagged} today", code=1)
    assert "nothing was written" in done.stderr and "safety scan refused" in done.stderr
    assert flagged not in done.stderr
    assert not nb.file.exists()
    assert nb.git("status", "--porcelain") == ""
    assert nb.git("fsck", "--unreachable", "--no-reflogs") == ""


def test_a_refusal_removes_the_flagged_blob_and_nothing_else(nb):
    """prune --expire=now took every dangling object in notes/.git, a stash or a hand-made blob."""
    nb.add("clean")
    other = subprocess.run(["git", "hash-object", "-w", "--stdin"], cwd=nb.root, input="made by hand\n", text=True,
                           capture_output=True, check=True).stdout.strip()
    nb.add(f"at {ADDRESS}", code=1)
    assert nb.git("fsck", "--unreachable", "--no-reflogs").split() == ["unreachable", "blob", other]
    nb.add(f"at {HOME_PATH}", code=1)
    nb.git("cat-file", "-e", other)


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


@pytest.mark.parametrize("form", [
    "addr:{a}", "mac:{a}", "{a}: connected", "wlan0: {a}: auth", "({a})", "{d}", "mac={d};",
    DOC_RANGE_IN_THE_MIDDLE,  # 00:00:5e in the middle isn't the documentation range
])
def test_an_address_touching_a_colon_or_with_dashes_is_refused(nb, form):
    """The org scan at the pin lets every one of these through, so the tool's own check has to."""
    words = form.format(a=ADDRESS, d=ADDRESS.replace(":", "-"))
    done = nb.add(f"seen: {words}", code=1)
    assert "an address at line" in done.stderr and ADDRESS not in done.stderr
    assert not nb.file.exists()


@pytest.mark.parametrize("allowed", [ZERO_ADDRESS, ZERO_ADDRESS.replace(":", "-"), "ff:ff:ff:ff:ff:ff", "00:00:5e:00:53:01",
                                     "12:34:56", "1234-5678-9abc", "12345678-1234-1234"])
def test_the_allowed_ranges_and_other_colon_numbers_still_go_in(nb, allowed):
    nb.add(f"words {allowed} words")


def test_the_hook_refuses_a_hand_edit_with_an_address_next_to_a_colon(nb):
    nb.add("start")
    nb.hand_commit(page(log=f"### 2000-01-01\n- link mac:{ADDRESS}: up\n"), code=1)


def test_a_real_address_on_a_line_with_an_allowed_one_is_refused(nb):
    assert "an address" in nb.add(f"{ZERO_ADDRESS} and {ADDRESS}", code=1).stderr


def log_line(words):
    """A write in this process, so a test can make the disk fill up or the user press Ctrl-C."""
    noteslib.write("a/t", lambda text: noteslib.add(text, words, None)[0])


def nothing_is_left(nb, before):
    assert nb.text() == before
    assert nb.git("status", "--porcelain") == ""
    assert nb.git("diff", "--cached", "--name-only") == ""
    assert nb.git("fsck", "--unreachable", "--no-reflogs") == ""
    assert not [p.name for p in nb.file.parent.iterdir() if p.name.endswith(".new")]


def test_a_full_disk_while_the_new_text_is_written_changes_nothing(nb, monkeypatch):
    """ENOSPC, simulated: the old write_text truncated the note before it wrote."""
    nb.add("clean")
    before, write_text = nb.text(), Path.write_text

    def full(self, *args, **kwargs):
        if self.name.endswith(".new"):
            raise OSError(28, "No space left on device")
        return write_text(self, *args, **kwargs)

    with monkeypatch.context() as patched:
        patched.setattr(Path, "write_text", full)
        with pytest.raises(wslib.ToolError, match="nothing was written"):
            log_line(f"at {ADDRESS}")
    nothing_is_left(nb, before)
    nb.add("the next write works")


def test_a_full_disk_at_the_commit_puts_the_old_note_back(nb, monkeypatch):
    nb.add("clean")
    before, git = nb.text(), noteslib._git

    def full(*args, **kwargs):
        if args[0] == "commit":
            raise OSError(28, "No space left on device")
        return git(*args, **kwargs)

    with monkeypatch.context() as patched:
        patched.setattr(noteslib, "_git", full)
        with pytest.raises(wslib.ToolError, match="nothing was written"):
            log_line("a second line")
    nothing_is_left(nb, before)
    nb.add("the next write works")


def test_an_interrupt_during_the_scan_leaves_the_note_and_git_clean(nb, monkeypatch):
    """KeyboardInterrupt, simulated from scan(): the flagged text stayed in the note and in the index."""
    nb.add("clean")
    before = nb.text()

    def interrupted():
        raise KeyboardInterrupt

    with monkeypatch.context() as patched:
        patched.setattr(noteslib, "scan", interrupted)
        with pytest.raises(KeyboardInterrupt):
            log_line(f"at {ADDRESS}")
    nothing_is_left(nb, before)
    nb.add("the next write works")
    assert ADDRESS not in nb.text()


def test_an_interrupt_after_the_new_note_is_in_puts_the_old_one_back(nb, monkeypatch):
    nb.add("clean")
    before, git = nb.text(), noteslib._git

    def interrupted(*args, **kwargs):
        if args[0] == "commit":
            assert "second" in nb.text()  # the new file really was in place
            raise KeyboardInterrupt
        return git(*args, **kwargs)

    with monkeypatch.context() as patched:
        patched.setattr(noteslib, "_git", interrupted)
        with pytest.raises(KeyboardInterrupt):
            log_line("a second line")
    nothing_is_left(nb, before)


def test_the_note_is_untouched_and_the_new_text_is_in_the_index_while_the_scan_runs(nb, monkeypatch):
    nb.add("clean")
    before, scan, seen = nb.text(), noteslib.scan, []

    def watching():
        seen.append((nb.text(), nb.git("show", ":a/t.md")))
        scan()

    with monkeypatch.context() as patched:
        patched.setattr(noteslib, "scan", watching)
        log_line("the new words")
    (working, staged), = seen
    assert working == before and "the new words" in staged


def test_a_write_that_was_killed_is_undone_by_the_next_one(nb, monkeypatch):
    """A kill skips every handler. What it leaves is a staged blob, a temporary file or a backup."""
    nb.add("clean")
    git = noteslib._git

    def killed_in_the_scan():
        raise KeyboardInterrupt

    def killed_at_the_commit(*args, **kwargs):
        if args[0] == "commit":
            raise KeyboardInterrupt
        return git(*args, **kwargs)

    with monkeypatch.context() as killed:
        killed.setattr(noteslib, "_undo", lambda *args: None)
        with monkeypatch.context() as in_the_scan:
            in_the_scan.setattr(noteslib, "scan", killed_in_the_scan)
            with pytest.raises(KeyboardInterrupt):
                log_line(f"at {ADDRESS}")
        assert nb.git("diff", "--cached", "--name-only").strip() == "a/t.md"  # the flagged text is staged
        with monkeypatch.context() as at_the_commit:
            at_the_commit.setattr(noteslib, "_git", killed_at_the_commit)
            with pytest.raises(KeyboardInterrupt):
                log_line("killed after the file went in")
        assert "killed after" in nb.text()  # in place, and not committed
    nb.add("the next write")
    assert "killed after" not in nb.text() and ADDRESS not in nb.text() and "the next write" in nb.text()
    assert nb.git("status", "--porcelain") == ""
    assert nb.git("fsck", "--unreachable", "--no-reflogs") == ""


def test_a_new_topic_killed_before_its_commit_is_gone_at_the_next_write(nb, monkeypatch):
    nb.add("start another topic here", "--guess")  # the repository exists now
    git = noteslib._git

    def killed_at_the_commit(*args, **kwargs):
        if args[0] == "commit":
            raise KeyboardInterrupt
        return git(*args, **kwargs)

    with monkeypatch.context() as killed:
        killed.setattr(noteslib, "_undo", lambda *args: None)
        killed.setattr(noteslib, "_git", killed_at_the_commit)
        with pytest.raises(KeyboardInterrupt):
            noteslib.write("b/new", lambda text: noteslib.add(text, "never committed", None)[0])
    assert (nb.root / "b" / "new.md").exists()
    noteslib.write("b/new", lambda text: noteslib.add(text, "the next one", None)[0])
    text = (nb.root / "b" / "new.md").read_text()
    assert "never committed" not in text and "the next one" in text
    assert nb.git("status", "--porcelain") == ""


# Words, not a traceback


def no_traceback(done):
    assert "Traceback" not in done.stderr, done.stderr
    return done.stderr


def test_a_heading_with_a_space_after_it_doesnt_end_in_a_traceback(nb):
    nb.add("start")
    nb.hand_commit(f"# a/t\n\n## Current \n## Open\n## Log \n### {TODAY} \n- earlier\n")
    nb.run("lint")
    nb.set("A claim", "--user")
    nb.add("A line")
    text = nb.text()
    assert text.index("## Current") < text.index("A claim") < text.index("## Open") < text.index("## Log") < text.index("A line")
    assert nb.text().count(f"### {TODAY}") == 1 and nb.text().count("## Current") == 1
    nb.run("lint")


def test_a_note_that_isnt_utf8_is_said_in_words_and_left_alone(nb):
    nb.add("start")
    raw = nb.file.read_bytes() + b"- caf\xe9\n"
    nb.file.write_bytes(raw)
    for done in (nb.add("more", code=1), nb.set("claim", "--user", code=1)):
        assert "isn't UTF-8" in no_traceback(done)
    assert nb.file.read_bytes() == raw
    nb.run("lint")


def test_an_identity_without_an_email_is_said_in_words(nb):
    (nb.ws.home / "workspace.toml").write_text('[identity]\nname = "N"\n')
    assert "needs a name and an email" in no_traceback(nb.add("words", code=1))
    assert not nb.file.exists()


def test_an_absolute_or_empty_glob_is_said_in_words(nb, tmp_path):
    for glob in ('"/etc/*"', '""'):
        with open(nb.ws.home / "workspace.toml", "a") as f:
            f.write(f'\n[[notes.source]]\nname = "x"\npath = "{tmp_path}"\nkind = "text"\nglob = {glob}\n')
        assert "glob" in no_traceback(nb.run("find", "word", code=1))
        (nb.ws.home / "workspace.toml").write_text('[identity]\nname = "N"\nemail = "n@example.org"\n')


def test_a_command_that_times_out_is_said_in_words(nb, monkeypatch, capsys):
    monkeypatch.setattr(noteslib, "_SECONDS", 0.001)
    assert wslib.run("notes", lambda argv: log_line("words")) == 1
    err = capsys.readouterr().err
    assert "did not finish" in err and "Traceback" not in err


def test_a_backup_left_after_the_commit_doesnt_put_old_text_back(nb):
    nb.add("one")
    (nb.file.parent / ".t.md.back.new").write_text("stale text from before")
    nb.add("two")
    assert "stale" not in nb.text() and "one" in nb.text() and "two" in nb.text()
    assert not (nb.file.parent / ".t.md.back.new").exists()


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


def test_a_tools_path_with_shell_characters_reaches_the_hook_as_text(nb, tmp_path):
    """The path went into a double-quoted string: a $, a quote or a backtick in it was the shell's."""
    odd = tmp_path / 'we"ird$HOME`touch pwned`\\x'
    shutil.copytree(TOOLS, odd / "tools", ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copytree(TOOLS.parent / ".glassine", odd / ".glassine")
    done = subprocess.run([sys.executable, str(odd / "tools" / "notes"), "add", "a/t", "words"], capture_output=True,
                          text=True, cwd=tmp_path)
    assert done.returncode == 0, done.stderr
    nb.hand_commit(nb.text() + "### 2000-01-01\n- a clean hand edit\n")
    assert "safety scan refused" in nb.hand_commit(page(log=f"### 2000-01-01\n- at {ADDRESS}\n"), code=1)
    assert not (tmp_path / "pwned").exists() and not (nb.root / "pwned").exists() and not Path("pwned").exists()


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


@pytest.mark.parametrize("lead", [1996, 1999, 2000, 2001, 3999, 4000, 4001])
def test_a_word_across_a_chunk_boundary_is_found(nb, tmp_path, lead):
    """The old cut every 2000 characters: 1996 characters, a space and the word found nothing."""
    (tmp_path / "c").mkdir()
    (tmp_path / "c" / "s.jsonl").write_text(transcript({"message": {"content": "x" * lead + " kumquat after"}}))
    nb.sources(("c", str(tmp_path / "c"), "transcripts"))
    assert len(nb.find("kumquat")) == 1


def test_the_chunks_lose_no_word_and_stay_short():
    words = " ".join(f"w{n}" for n in range(3000)) + " " + "y" * 5000 + "\t tail\n end"
    chunks = list(notesindex._chunks(words))
    assert all(0 < len(chunk) <= notesindex._CHUNK for chunk in chunks)
    assert " ".join(chunks).split()[:3000] == words.split()[:3000]
    assert "".join(chunks).count("y") == 5000 and chunks[-1].endswith("end")


def test_a_lone_surrogate_in_a_transcript_breaks_no_search(nb, tmp_path):
    """JSON can say \\ud83d on its own, as text cut in the middle of an emoji does: SQLite can't store it."""
    nb.add("note about papaya")
    (tmp_path / "c").mkdir()
    (tmp_path / "c" / "s.jsonl").write_text(transcript(
        {"message": {"content": "kumquat \ud83d cut"}},
        {"message": {"content": [{"type": "tool_use", "input": {"command": "run lychee \ud83d"}}]}}))
    nb.sources(("c", str(tmp_path / "c"), "transcripts"))
    assert len(nb.find("kumquat")) == 1 and len(nb.find("lychee")) == 1
    assert [row["state"] for row in nb.find("papaya")] == ["history"]


def test_a_file_that_cant_be_indexed_is_that_file_s_warning_and_the_rest_is_searched(nb, tmp_path):
    nb.add("note about papaya")
    (tmp_path / "c").mkdir()
    (tmp_path / "c" / "bad.jsonl").write_text("[" * 400000 + "\n")  # deeper than the JSON reader goes
    (tmp_path / "c" / "good.jsonl").write_text(transcript({"message": {"content": "kumquat"}}))
    nb.sources(("c", str(tmp_path / "c"), "transcripts"))
    done = nb.run("find", "kumquat", "--json")
    assert len(done.stdout.splitlines()) == 1
    assert "bad.jsonl couldn't be read, so it wasn't searched" in done.stderr
    assert len(nb.find("papaya")) == 1


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


def test_a_source_covering_the_workspace_reads_the_notes_once_as_notes(nb, tmp_path):
    nb.seen("kumquat fact")
    (nb.ws.home / "readme.md").write_text("kumquat in a readme\n")
    nb.sources(("everything", str(nb.ws.home), "text"))
    rows = nb.find("kumquat")
    assert sorted(row["state"] for row in rows) == ["archive, unreviewed", "current"]
    assert [row for row in rows if row["state"] == "current"][0]["where"].startswith("a/t:")


def test_a_source_inside_another_is_read_once(nb, tmp_path):
    (tmp_path / "docs" / "inner").mkdir(parents=True)
    (tmp_path / "docs" / "inner" / "d.md").write_text("kumquat\n")
    nb.sources(("outer", str(tmp_path / "docs"), "text"), ("inner", str(tmp_path / "docs" / "inner"), "text"))
    [row] = nb.find("kumquat")
    assert row["where"].endswith("inner/d.md:1")
    assert len(nb.find("kumquat")) == 1  # and again, from the index


def test_a_busy_index_is_not_called_damaged_and_is_not_replaced(nb):
    nb.seen("A fact")
    nb.find("fact")
    db = nb.ws.user_home / ".cache" / "glassine" / "notes-index" / "index.db"
    inode = db.stat().st_ino
    holder = sqlite3.connect(db, isolation_level=None)
    holder.execute("BEGIN EXCLUSIVE")
    try:
        done = nb.run("find", "fact", code=1)
        assert "damaged" not in done.stderr and "can't be used just now" in done.stderr
        assert db.stat().st_ino == inode
    finally:
        holder.execute("COMMIT")
        holder.close()
    assert nb.run("find", "fact").stdout.startswith("current")


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
