# SPDX-License-Identifier: GPL-2.0-or-later
import errno
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

import artstore
import wslib


def entries(ws, folder="bases"):
    return [json.loads(line) for line in (ws.artifacts / folder / "manifest.jsonl").read_text().split("\n") if line]


def test_add_moves_the_file_in_with_a_full_entry(ws):
    src = ws.file("a.bin", "hello\n")
    ws.art("add", src, "--to", "bases", "--class", "ours", "--what", "the base", "--keep", "always", "--by", "me",
           "--from", "commit abc")
    assert not src.exists()
    [e] = entries(ws)
    sha = hashlib.sha256(b"hello\n").hexdigest()
    assert e["id"] == sha[:12] and e["sha256"] == sha and e["size"] == 6 and e["name"] == "a.bin"
    assert (e["class"], e["what"], e["by"], e["from"], e["keep"], e["tombstone"]) == ("ours", "the base", "me", ["commit abc"], "always", None)
    assert e["made"].endswith("Z")
    path = Path(ws.art("path", "a.bin").stdout.strip())
    assert path.read_text() == "hello\n"
    assert path.is_relative_to(ws.artifacts / "bases")
    assert ws.art("path", e["id"]).stdout.strip() == str(path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o400


def test_folders_are_private_and_an_executable_stays_one(ws):
    src = ws.file("tool")
    src.chmod(0o755)
    ws.art("add", src, "--to", "dumps", "--class", "derived", "--what", "x", "--keep", "user")
    assert stat.S_IMODE(ws.artifacts.stat().st_mode) == 0o700
    assert stat.S_IMODE((ws.artifacts / "dumps").stat().st_mode) == 0o700
    assert stat.S_IMODE((ws.artifacts / "dumps" / "manifest.jsonl").stat().st_mode) == 0o600
    assert os.access(ws.art("path", "tool").stdout.strip(), os.X_OK)


def test_in_place_doesnt_move_and_copy_keeps_the_source(ws):
    src = ws.old_tree / "kept.bin"
    src.write_text("old\n")
    ws.art("add", src, "--in-place", "--to", "vendor", "--class", "vendor", "--what", "x", "--keep", "user")
    assert src.exists() and ws.art("path", "kept.bin").stdout.strip() == str(src.resolve())
    assert not [p for p in (ws.artifacts / "vendor").iterdir() if p.name != "manifest.jsonl"]
    other = ws.file("copied.bin")
    ws.art("add", other, "--copy", "--to", "bases", "--class", "ours", "--what", "x", "--keep", "user")
    assert other.exists() and Path(ws.art("path", "copied.bin").stdout.strip()) != other


def test_add_refuses_what_it_shouldnt_index(ws):
    src = ws.file("a.bin")
    good = ["--to", "bases", "--class", "ours", "--what", "x", "--keep", "user"]
    ws.art("add", ws.tmp, *good, code=1)
    ws.art("add", src, "--to", "bases", "--class", "ours", "--what", " ", "--keep", "user", code=1)
    ws.art("add", src, "--to", "bases", "--class", "ours", "--what", "x", "--keep", "someday", code=1)
    ws.art("add", src, "--name", "../up", *good, code=1)
    link = ws.tmp / "link"
    link.symlink_to(src)
    ws.art("add", link, *good, code=1)
    cached = ws.user_home / ".cache" / "glassine" / "x"
    cached.parent.mkdir(parents=True)
    cached.write_text("x")
    assert "build cache" in ws.art("add", cached, "--in-place", *good, code=1).stderr
    assert src.exists() and link.is_symlink()


def test_a_name_means_one_content_and_adding_twice_is_harmless(ws):
    ws.add("a.bin", content="one\n")
    again = ws.file("a.bin", "one\n")
    good = ["--to", "bases", "--class", "ours", "--what", "x", "--keep", "user"]
    assert "already indexed" in ws.art("add", again, *good).stdout
    assert again.exists() and len(entries(ws)) == 1
    different = ws.file("a.bin", "two\n")
    assert "taken" in ws.art("add", different, *good, code=1).stderr
    assert different.exists() and len(entries(ws)) == 1


def test_same_content_under_two_names_needs_a_name_to_resolve(ws):
    ws.add("a.bin", content="same\n")
    ws.add("b.bin", content="same\n")
    id_ = entries(ws)[0]["id"]
    assert "several names" in ws.art("path", id_, code=1).stderr
    assert ws.art("path", "b.bin").stdout
    ws.art("rm", "a.bin", "--why", "twin")  # the other one isn't a reference to it
    assert ws.art("path", id_).stdout


def test_find_by_word_folder_and_class(ws):
    ws.add("a.bin")
    ws.art("add", ws.file("b.bin"), "--to", "vendor", "--class", "vendor", "--what", "Blob from the maker", "--keep", "user")
    assert "b.bin" in ws.art("find", "blob", "maker").stdout
    assert ws.art("find", "blob", "nothing").stdout == ""
    assert "a.bin" not in ws.art("find", "--folder", "vendor").stdout
    assert "b.bin" not in ws.art("find", "--class", "derived").stdout
    row = json.loads(ws.art("find", "--json", "blob").stdout)
    assert row["folder"] == "vendor" and row["name"] == "b.bin"


def test_find_expired_lists_only_live_passed_dates(ws):
    ws.add("old.bin", keep="2000-01-01")
    ws.add("later.bin", keep="2999-01-01")
    ws.add("always.bin", keep="always")
    out = ws.art("find", "--expired").stdout
    assert "old.bin" in out and "later.bin" not in out and "always.bin" not in out


def test_verify_says_whats_wrong(ws):
    ws.add("a.bin")
    ws.add("b.bin")
    assert ws.art("verify").stdout == ""
    a, b = Path(ws.art("path", "a.bin").stdout.strip()), Path(ws.art("path", "b.bin").stdout.strip())
    a.chmod(0o600)
    a.write_text("changed")
    b.unlink()
    (ws.artifacts / "bases" / "stray").write_text("x")
    out = ws.art("verify", code=1).stdout
    assert "changed  a.bin" in out and "missing  b.bin" in out and "not indexed" in out and "stray" in out
    assert "not indexed" not in ws.art("verify", "a.bin", code=1).stdout
    (ws.artifacts / "bases").chmod(0o755)
    assert "too open" in ws.art("verify", code=1).stdout


def test_rm_deletes_and_leaves_a_tombstone(ws):
    ws.add("a.bin")
    path = Path(ws.art("path", "a.bin").stdout.strip())
    out = ws.art("rm", "a.bin", "--why", "made room", "--by", "me").stdout
    assert "removed" in out and not path.exists()
    t = entries(ws)[0]["tombstone"]
    assert (t["why"], t["by"], t["file"], t["retention"]) == ("made room", "me", "deleted", None)
    assert "made room" in ws.art("path", "a.bin", code=1).stderr
    assert ws.art("find", "a.bin").stdout == ""
    assert "REMOVED" in ws.art("find", "--all", "a.bin").stdout
    ws.art("rm", "a.bin", "--why", "again", code=1)
    ws.add("a.bin", content="a new one\n")  # the name is free again


def test_rm_dry_run_changes_nothing(ws):
    ws.add("a.bin")
    assert "nothing was changed" in ws.art("rm", "a.bin", "--why", "x", "--dry-run").stdout
    assert ws.art("path", "a.bin").stdout and entries(ws)[0]["tombstone"] is None


def test_rm_of_in_place_keeps_the_file_unless_told(ws):
    for name in ("one.bin", "two.bin"):
        (ws.old_tree / name).write_text(name)
        ws.art("add", ws.old_tree / name, "--in-place", "--to", "vendor", "--class", "vendor", "--what", "x", "--keep", "2000-01-01")
    ws.art("rm", "one.bin", "--why", "x")
    assert (ws.old_tree / "one.bin").exists()
    assert entries(ws, "vendor")[0]["tombstone"]["file"] == "kept in place"
    ws.art("rm", "two.bin", "--why", "x", "--delete-in-place")
    assert not (ws.old_tree / "two.bin").exists()


def refuse(ws, name, why):
    err = ws.art("rm", name, "--why", "x", code=1).stderr
    assert why in err and f"--retention-said {name}" in err, err
    assert ws.art("path", name).stdout


def test_rm_refuses_keep_user_always_and_an_unpassed_date(ws):
    ws.add("u.bin", keep="user")
    ws.add("a.bin", keep="always")
    ws.add("d.bin", keep="2999-01-01")
    refuse(ws, "u.bin", "keep: user")
    refuse(ws, "a.bin", "keep: always")
    refuse(ws, "d.bin", "keep: until 2999-01-01")


def test_rm_finds_the_id_name_and_path_in_each_kind_of_place(ws):
    ws.add("a.bin")
    e = entries(ws)[0]
    for where, text in [(ws.checkout / "pins.toml", f"sha = \"{e['sha256']}\"\n"),
                        (ws.checkout / "short.txt", f"id {e['id']}\n"),
                        (ws.old_tree / "docs" / "diary.md", "we flashed a.bin.\n"),
                        (ws.old_tree / "script.sh", f"cp ~/GlassineOS/artifacts/{e['path']} .\n"),
                        (ws.artifacts / "images" / "x-manifest.json", f'{{"input": "{e["id"]}"}}\n')]:
        where.parent.mkdir(parents=True, exist_ok=True)
        where.write_text(text)
        refuse(ws, "a.bin", str(where))
        where.unlink()
    ws.art("rm", "a.bin", "--why", "nothing names it now")


def test_rm_finds_a_full_in_place_path_and_a_tilde_path(ws):
    ws.user_home.joinpath("tree").mkdir()
    src = ws.user_home / "tree" / "f.bin"
    src.write_text("x")
    ws.art("add", src, "--in-place", "--name", "other-name", "--to", "vendor", "--class", "vendor", "--what", "x", "--keep", "2000-01-01")
    note = ws.old_tree / "note.txt"
    for text in ("see ~/tree/f.bin there", f"see {src}", "basename f.bin"):
        note.write_text(text)
        refuse(ws, "other-name", str(note))


def test_rm_doesnt_take_a_longer_name_for_this_one(ws):
    ws.add("a.bin")
    (ws.old_tree / "n.txt").write_text("data.bin a.bin.gz a.bin-old\n")
    ws.art("rm", "a.bin", "--why", "x")


def test_rm_counts_the_parent_base_and_lets_go_once_the_delta_has_gone(ws):
    ws.add("base.img")
    ws.add("delta.img", "--parent-base", "base.img")
    delta = entries(ws)[1]
    assert delta["parent_base"] == entries(ws)[0]["id"]
    refuse(ws, "base.img", "artifact delta.img: parent_base")
    ws.art("rm", "delta.img", "--why", "superseded")
    ws.art("rm", "base.img", "--why", "no delta needs it")


def test_a_delta_needs_a_known_parent(ws):
    path = ws.file("delta.img")
    ws.art("add", path, "--to", "bases", "--class", "derived", "--what", "x", "--keep", "user", "--parent-base", "nope", code=1)
    assert path.exists()


def test_parent_base_holds_even_when_no_file_in_the_places_names_it(ws, monkeypatch):
    monkeypatch.setenv("GLASSINE_ART_PLACES", str(ws.old_tree))
    ws.add("base.img")
    ws.add("delta.img", "--parent-base", "base.img")
    refuse(ws, "base.img", "parent_base")


def test_rm_counts_a_from_reference_in_another_entry(ws):
    ws.add("src.bin")
    ws.add("out.bin", "--from", "src.bin")
    refuse(ws, "src.bin", "artifact out.bin")


def test_retention_flag_must_name_the_file_and_carry_words_and_is_recorded(ws):
    ws.add("a.bin", keep="user")
    (ws.old_tree / "ref.txt").write_text("a.bin\n")
    path = Path(ws.art("path", "a.bin").stdout.strip())
    assert "names other.bin" in ws.art("rm", "a.bin", "--why", "x", "--retention-said", "other.bin", "yes", code=1).stderr
    assert "words" in ws.art("rm", "a.bin", "--why", "x", "--retention-said", "a.bin", "  ", code=1).stderr
    assert path.exists()
    assert "overriding" in ws.art("rm", "a.bin", "--why", "room", "--retention-said", "a.bin", "delete a.bin, it's old").stdout
    assert not path.exists()
    t = entries(ws)[0]["tombstone"]
    assert t["retention"] == {"named": "a.bin", "words": "delete a.bin, it's old"}
    assert any("keep: user" in r for r in t["overrode"]) and any("ref.txt" in r for r in t["overrode"])


def test_rm_refuses_changed_content_without_a_retention(ws):
    ws.add("a.bin")
    path = Path(ws.art("path", "a.bin").stdout.strip())
    path.chmod(0o600)
    path.write_text("edited")
    refuse(ws, "a.bin", "no longer matches")


def test_rm_records_a_file_that_was_already_gone(ws):
    ws.add("a.bin")
    Path(ws.art("path", "a.bin").stdout.strip()).unlink()
    ws.art("rm", "a.bin", "--why", "x")
    assert entries(ws)[0]["tombstone"]["file"] == "was missing"


def test_rm_refuses_when_a_required_place_is_missing_even_with_a_retention(ws, monkeypatch):
    ws.add("a.bin")
    monkeypatch.setenv("GLASSINE_ART_PLACES", str(ws.tmp / "gone"))
    assert "can't search" in ws.art("rm", "a.bin", "--why", "x", "--retention-said", "a.bin", "yes", code=1).stderr


def test_search_skips_binaries_and_git_dirs_and_never_opens_a_fifo(ws):
    ws.add("a.bin")
    (ws.old_tree / "blob").write_bytes(b"\0\0 a.bin")
    (ws.old_tree / ".git").mkdir()
    (ws.old_tree / ".git" / "x").write_text("a.bin")
    os.mkfifo(ws.old_tree / "pipe")
    done = subprocess.run(["timeout", "20", "python3", str(Path(artstore.__file__).parent / "art"), "rm", "a.bin", "--why", "x", "--dry-run"],
                          capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    assert "1 of them binary" in done.stdout


def test_search_sees_a_reference_past_the_first_chunk_and_across_a_chunk_edge(ws):
    ws.add("a.bin")
    big = ws.old_tree / "log.txt"
    big.write_text("x" * ((1 << 20) + 8192 - 3) + " a.bin \n")
    refuse(ws, "a.bin", str(big))


def test_places_come_from_the_config_and_default_to_the_old_tree(ws, monkeypatch):
    monkeypatch.delenv("GLASSINE_ART_PLACES")
    monkeypatch.setenv("GLASSINE_WORKSPACE", str(ws.home))
    monkeypatch.setattr(Path, "home", classmethod(lambda _: ws.user_home))
    assert "~/BooxNoteAirHacking" in artstore.DEFAULT_PLACES
    (ws.user_home / "BooxNoteAirHacking").mkdir()
    (ws.home / "repo").mkdir()
    (ws.home / "repo" / ".git").mkdir()
    (ws.home / "not-a-checkout").mkdir()
    found = artstore.places()
    assert ws.home in found and ws.home / "repo" in found and ws.checkout in found
    assert ws.user_home / "BooxNoteAirHacking" in found and ws.home / "not-a-checkout" not in found
    (ws.home / "workspace.toml").write_text(f'[art]\nsearch = ["{ws.old_tree}"]\n')
    assert artstore.places() == [ws.old_tree]


def test_a_corrupt_manifest_stops_everything_rather_than_dropping_entries(ws):
    ws.add("a.bin")
    manifest = ws.artifacts / "bases" / "manifest.jsonl"
    manifest.write_text(manifest.read_text() + "not json\n")
    before = manifest.read_text()
    assert "isn't JSON" in ws.art("add", ws.file("b.bin"), "--to", "bases", "--class", "ours", "--what", "x", "--keep", "user", code=1).stderr
    assert manifest.read_text() == before


def test_the_tools_find_the_store_from_the_home_folder_not_their_own_path(ws, monkeypatch):
    monkeypatch.delenv("GLASSINE_WORKSPACE")
    assert wslib.home() == ws.user_home / "GlassineOS"
    assert wslib.artifacts() == ws.user_home / "GlassineOS" / "artifacts"
    assert wslib.notes() == ws.user_home / "GlassineOS" / "notes"


def test_the_repository_tracks_tools_and_ignores_results():
    repo = Path(artstore.__file__).parents[1]
    def ignored(path):
        return subprocess.run(["git", "check-ignore", "-q", path], cwd=repo).returncode == 0
    assert not ignored("tools/art") and not ignored("tests/test_art.py")
    assert ignored("artifacts/dumps/x") and ignored("notes/x.md") and ignored("work/x/y")


def test_line_separator_characters_survive_the_manifest(ws):
    words = "pasted text and\x85more"
    ws.art("add", ws.file("a.bin"), "--to", "bases", "--class", "ours", "--what", words, "--keep", "user")
    ws.add("b.bin")  # a second command still reads the manifest
    assert entries(ws)[0]["what"] == words
    assert "a.bin" in ws.art("find", "pasted").stdout
    ws.art("rm", "b.bin", "--why", "room\x85now")
    assert ws.art("verify").stdout == ""
    assert entries(ws)[1]["tombstone"]["why"] == "room\x85now"


needs_permissions = pytest.mark.skipif(os.geteuid() == 0, reason="root reads what mode 000 hides")


@needs_permissions
def test_rm_refuses_when_a_folder_cant_be_listed_even_with_a_retention(ws):
    ws.add("a.bin")
    (ws.old_tree / "sub").mkdir()
    (ws.old_tree / "sub" / "ref.txt").write_text("a.bin\n")
    (ws.old_tree / "sub").chmod(0o000)
    try:
        err = ws.art("rm", "a.bin", "--why", "x", code=1).stderr
        assert "couldn't be listed" in err and str(ws.old_tree / "sub") in err
        assert "couldn't be listed" in ws.art("rm", "a.bin", "--why", "x", "--dry-run", code=1).stderr
        assert "couldn't be listed" in ws.art("rm", "a.bin", "--why", "x", "--retention-said", "a.bin", "yes", code=1).stderr
        assert entries(ws)[0]["tombstone"] is None
    finally:
        (ws.old_tree / "sub").chmod(0o700)
    ws.art("rm", "a.bin", "--why", "x", "--dry-run", code=1)  # the reference is found once it can be read


@needs_permissions
def test_rm_refuses_when_a_whole_place_cant_be_listed(ws):
    ws.add("a.bin")
    (ws.old_tree / "ref.txt").write_text("a.bin\n")
    ws.old_tree.chmod(0o000)
    try:
        assert "couldn't be listed" in ws.art("rm", "a.bin", "--why", "x", code=1).stderr
    finally:
        ws.old_tree.chmod(0o700)


@needs_permissions
def test_rm_names_the_files_it_couldnt_read_in_the_output_and_the_tombstone(ws):
    ws.add("a.bin")
    secret = ws.old_tree / "root-owned.log"
    secret.write_text("a.bin\n")
    secret.chmod(0o000)
    try:
        out = ws.art("rm", "a.bin", "--why", "x").stdout
        assert "1 more unreadable" in out and f"not read: {secret}" in out
    finally:
        secret.chmod(0o600)
    t = entries(ws)[0]["tombstone"]
    assert t["search"]["unreadable"] == 1 and t["search"]["unreadable_paths"] == [str(secret)]


def test_a_symlink_to_the_artifact_is_a_reference_and_is_never_opened(ws):
    ws.add("a.bin")
    target = ws.art("path", "a.bin").stdout.strip()
    for link in (ws.old_tree / "current", ws.checkout / "pinned"):
        link.symlink_to(target)
        refuse(ws, "a.bin", f"{link} (symlink)")
        link.unlink()
    relative = ws.old_tree / "relative"
    relative.symlink_to(os.path.relpath(target, ws.old_tree))
    refuse(ws, "a.bin", f"{relative} (symlink)")
    relative.unlink()
    elsewhere = ws.tmp / "elsewhere"  # a link whose text doesn't name the file, but which leads to it
    elsewhere.symlink_to(target)
    (ws.old_tree / "hop").symlink_to(elsewhere)
    refuse(ws, "a.bin", "hop (symlink)")
    (ws.old_tree / "hop").unlink()
    (ws.old_tree / "unrelated").symlink_to(ws.tmp / "nowhere")  # dangling, and nothing to do with it
    ws.art("rm", "a.bin", "--why", "no link left")


def elsewhere_device(ws):
    """A temporary folder on another file system than the workspace, or skip."""
    for candidate in ("/dev/shm", "/run/user/%d" % os.getuid(), "/var/tmp"):
        if os.path.isdir(candidate) and os.access(candidate, os.W_OK) and os.stat(candidate).st_dev != ws.tmp.stat().st_dev:
            return Path(tempfile.mkdtemp(dir=candidate))
    pytest.skip("no writable folder on another file system")


def test_a_move_across_file_systems_leaves_no_source_behind(ws):
    other = elsewhere_device(ws)
    try:
        src = other / "c.bin"
        src.write_text("across\n")
        src.chmod(0o755)
        ws.art("add", src, "--to", "bases", "--class", "ours", "--what", "x", "--keep", "user")
        assert not src.exists()
        stored = Path(ws.art("path", "c.bin").stdout.strip())
        assert stored.read_text() == "across\n" and os.access(stored, os.X_OK)
        assert ws.art("verify").stdout == ""
        copied = other / "d.bin"
        copied.write_text("kept\n")
        ws.art("add", copied, "--copy", "--to", "bases", "--class", "ours", "--what", "x", "--keep", "user")
        assert copied.exists()
    finally:
        shutil.rmtree(other, ignore_errors=True)


@needs_permissions
def test_a_move_that_cant_remove_its_source_stores_nothing(ws):
    other = elsewhere_device(ws)
    try:
        src = other / "c.bin"
        src.write_text("stuck\n")
        other.chmod(0o500)  # the original can be copied but not removed
        err = ws.art("add", src, "--to", "bases", "--class", "ours", "--what", "x", "--keep", "user", code=1).stderr
        assert "can't be removed" in err
        assert src.exists() and entries(ws) == []
        assert ws.art("verify").stdout == ""  # no store copy left behind
    finally:
        other.chmod(0o700)
        shutil.rmtree(other, ignore_errors=True)


def write_entries(ws, folder, rows):
    (ws.artifacts / folder / "manifest.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))


@needs_permissions
def test_a_delete_that_fails_says_so_in_the_tombstone_and_verify_flags_the_file(ws):
    src = ws.old_tree / "stuck.bin"
    src.write_text("stuck")
    ws.art("add", src, "--in-place", "--to", "vendor", "--class", "vendor", "--what", "x", "--keep", "2000-01-01")
    ws.old_tree.chmod(0o500)  # the file can be read but not unlinked
    try:
        err = ws.art("rm", "stuck.bin", "--why", "x", "--delete-in-place", code=1).stderr
        assert "still there" in err
        t = entries(ws, "vendor")[0]["tombstone"]
        assert t["file"] == "delete failed" and t["error"]
        out = ws.art("verify", code=1).stdout
        assert "still there  stuck.bin" in out and "delete failed" in out
    finally:
        ws.old_tree.chmod(0o700)
    src.unlink()
    assert ws.art("verify").stdout == ""


def test_verify_flags_a_tombstone_left_between_the_write_and_the_unlink(ws):
    ws.add("a.bin")
    path = Path(ws.art("path", "a.bin").stdout.strip())
    row = entries(ws)[0]
    row["tombstone"] = {"when": "2000-01-01T00:00:00Z", "by": "me", "why": "x", "retention": None, "overrode": [],
                        "search": {}, "file": "deleting"}
    write_entries(ws, "bases", [row])
    out = ws.art("verify", code=1).stdout
    assert f"still there  a.bin: {path}" in out and "deleting" in out


def test_a_file_that_vanishes_before_the_delete_is_recorded_as_missing(ws, monkeypatch):
    ws.add("a.bin")
    path = Path(ws.art("path", "a.bin").stdout.strip())
    real = Path.unlink

    def vanish(self, *args, **kwargs):
        real(self, *args, **kwargs)
        raise FileNotFoundError(self)

    monkeypatch.setattr(Path, "unlink", vanish)
    report = artstore.remove("a.bin", why="x", by="me")
    monkeypatch.setattr(Path, "unlink", real)
    assert report["entry"]["tombstone"]["file"] == "was missing" and not path.exists()
    assert entries(ws)[0]["tombstone"]["file"] == "was missing"


def test_a_re_added_name_doesnt_make_its_old_tombstone_look_undeleted(ws):
    ws.add("a.bin", content="same\n")
    ws.art("rm", "a.bin", "--why", "x")
    ws.add("a.bin", content="same\n")  # same id and name, so the same path in the store
    assert ws.art("verify").stdout == ""


def during_the_search(monkeypatch, action):
    """Run action once the places have been searched, before rm takes the lock: where a long search leaves a gap."""
    real = artstore.search

    def search(*args, **kwargs):
        found = real(*args, **kwargs)
        monkeypatch.setattr(artstore, "search", real)  # once
        action()
        return found

    monkeypatch.setattr(artstore, "search", search)


def test_a_delta_added_while_rm_searches_still_holds_its_parent(ws, monkeypatch):
    ws.add("base.img")
    delta = ws.file("delta.img", "delta\n")
    during_the_search(monkeypatch, lambda: artstore.add(
        delta, "bases", name=None, cls="derived", what="x", keep="user", by="me", parent_base="base.img"))
    with pytest.raises(wslib.ToolError, match="artifact delta.img: parent_base"):
        artstore.remove("base.img", why="x", by="me")
    assert ws.art("path", "base.img").stdout and entries(ws)[0]["tombstone"] is None


def test_the_same_gap_lets_a_retention_through_and_records_what_it_overrode(ws, monkeypatch):
    ws.add("base.img")
    delta = ws.file("delta.img", "delta\n")
    during_the_search(monkeypatch, lambda: artstore.add(
        delta, "bases", name=None, cls="derived", what="x", keep="user", by="me", parent_base="base.img"))
    report = artstore.remove("base.img", why="x", by="me", retention=("base.img", "yes, that one"))
    assert report["entry"]["tombstone"]["overrode"] == ["referenced by artifact delta.img: parent_base"]


def test_a_name_that_changed_content_while_rm_searched_is_not_removed(ws, monkeypatch):
    ws.add("a.bin", content="one\n")
    path = Path(ws.art("path", "a.bin").stdout.strip())

    def replace():
        artstore.remove("a.bin", why="first", by="other")
        artstore.add(ws.file("a.bin", "two\n"), "bases", name=None, cls="derived", what="x", keep="2000-01-01", by="other")

    during_the_search(monkeypatch, replace)
    with pytest.raises(wslib.ToolError, match="replaced while this ran"):
        artstore.remove("a.bin", why="second", by="me")
    assert entries(ws)[1]["tombstone"] is None and not path.exists()
    assert Path(ws.art("path", "a.bin").stdout.strip()).read_text() == "two\n"


def leftovers(ws):
    return sorted(str(p.relative_to(ws.artifacts)) for p in ws.artifacts.rglob("*.new"))


def test_a_what_that_isnt_utf8_is_refused_before_anything_moves(ws):
    src = ws.file("a.bin")
    done = subprocess.run([sys.executable, str(Path(artstore.__file__).parent / "art"), "add", str(src), "--to", "bases",
                           "--class", "ours", "--keep", "user", "--what", b"caf\xe9"], capture_output=True)
    assert done.returncode == 1 and b"UTF-8" in done.stderr and b"Traceback" not in done.stderr
    assert src.exists() and leftovers(ws) == []
    assert not [p for p in (ws.artifacts / "bases").iterdir() if p.name != "manifest.jsonl"]
    ws.art("verify")


def test_a_full_disk_while_writing_the_entry_moves_nothing(ws, monkeypatch):
    src = ws.file("a.bin")

    def full(_):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(os, "fsync", full)
    with pytest.raises(OSError, match="No space"):
        artstore.add(src, "bases", name=None, cls="ours", what="x", keep="user", by="me")
    assert src.exists() and leftovers(ws) == [] and artstore.read("bases") == []
    assert not [p for p in (ws.artifacts / "bases").iterdir()]


def test_a_failed_move_takes_the_entry_out_again_and_leaves_the_source(ws, monkeypatch):
    ws.add("kept.bin")
    before = (ws.artifacts / "bases" / "manifest.jsonl").read_text()
    src = ws.file("a.bin")

    def full(*_):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(artstore, "_store", full)
    with pytest.raises(OSError, match="No space"):
        artstore.add(src, "bases", name=None, cls="ours", what="x", keep="user", by="me")
    assert src.exists() and (ws.artifacts / "bases" / "manifest.jsonl").read_text() == before


def test_a_copy_that_runs_out_of_room_leaves_no_partial_file_in_the_store(ws, monkeypatch):
    other = elsewhere_device(ws)
    try:
        src = other / "big.bin"
        src.write_text("data\n")

        def partial(source, target):
            Path(target).write_text("da")
            raise OSError(errno.ENOSPC, "No space left on device")

        monkeypatch.setattr(artstore.shutil, "copyfile", partial)
        with pytest.raises(OSError):
            artstore.add(src, "bases", name=None, cls="ours", what="x", keep="user", by="me")
        assert src.exists() and leftovers(ws) == [] and artstore.read("bases") == []
        assert not [p for p in (ws.artifacts / "bases").iterdir() if p.name != "manifest.jsonl"]
    finally:
        shutil.rmtree(other, ignore_errors=True)


def test_a_why_that_isnt_utf8_deletes_nothing(ws):
    ws.add("a.bin")
    path = Path(ws.art("path", "a.bin").stdout.strip())
    done = subprocess.run([sys.executable, str(Path(artstore.__file__).parent / "art"), "rm", "a.bin", "--why", b"caf\xe9"],
                          capture_output=True)
    assert done.returncode == 1 and b"UTF-8" in done.stderr
    assert path.exists() and entries(ws)[0]["tombstone"] is None and leftovers(ws) == []


def test_a_root_doesnt_hide_the_work_folders_and_artifacts_below_it(ws, monkeypatch):
    ws.add("a.bin")
    monkeypatch.setenv("GLASSINE_ART_PLACES", str(ws.home))  # only the workspace itself, as the reviewer ran it
    ref = ws.checkout / "ref.txt"
    ref.write_text("a.bin\n")
    refuse(ws, "a.bin", str(ref))
    ref.unlink()
    (ws.artifacts / "images").mkdir()
    (ws.artifacts / "images" / "x-manifest.json").write_text("a.bin\n")
    refuse(ws, "a.bin", "x-manifest.json")


def test_nothing_to_search_is_a_refusal_not_a_clear_result(ws, monkeypatch):
    ws.add("a.bin")
    monkeypatch.setenv("GLASSINE_ART_PLACES", "")
    assert "no places to search" in ws.art("rm", "a.bin", "--why", "x", code=1).stderr
    assert "no places to search" in ws.art("rm", "a.bin", "--why", "x", "--retention-said", "a.bin", "yes", code=1).stderr
    monkeypatch.delenv("GLASSINE_ART_PLACES")
    config = ws.home / "workspace.toml"
    config.write_text("[art]\nsearch = []\n")
    assert "no places to search" in ws.art("rm", "a.bin", "--why", "x", code=1).stderr
    for text in ('[art]\nsearch = "/tmp"\n', "[art]\nsearch = [1, 2]\n", "art = 3\n"):
        config.write_text(text)
        err = ws.art("rm", "a.bin", "--why", "x", code=1).stderr
        assert "has to be a list" in err and str(config) in err
    assert ws.art("path", "a.bin").stdout
