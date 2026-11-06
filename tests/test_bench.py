# SPDX-License-Identifier: GPL-2.0-or-later
"""bench against dev's fake device. The benchmarks are made up here: the repository holds none of them."""
import json
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from test_repro import Kit

SCRIPT = 'echo "arm=$GLASSINE_BENCH_ARM settings=$GLASSINE_BENCH_SETTINGS"\necho "bench: frame_ms 1$(echo "$GLASSINE_BENCH_ARM" | tr -d a-z)"\n'
ARMS = """
[[arm]]
name = "a1"
settings = { speed = 1 }
[[arm]]
name = "b2"
settings = { speed = 2, mode = "quick one" }
"""
RUN = '[run]\nwhere = "device"\ntimeout = "1m"\n'
SOURCE = '[source]\nfile = "run.sh"\n'


class Bench(Kit):
    def __init__(self, tmp_path):
        super().__init__(tmp_path)
        self.git("init", "-q")
        self.commit()

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.kit), "-c", "user.name=t", "-c", "user.email=t@example.org", *args],
                              capture_output=True, text=True, check=True).stdout.strip()

    def commit(self):
        self.git("add", "-A")
        self.git("commit", "-q", "--allow-empty", "-m", "kit")
        return self.git("rev-parse", "HEAD")

    def bench(self, *args, code=0, seconds=30):
        return self.tool("bench", *args, code=code, seconds=seconds)

    def write(self, name, toml, script=SCRIPT, commit=True):
        folder = self.kit / "bench" / name
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "bench.toml").write_text(toml)
        (folder / "run.sh").write_text(script)
        return self.commit() if commit else None

    def plain(self, name="widget-speed", script=SCRIPT, top="", rounds=2, commit=True):
        return self.write(name, f'what = "a made-up bench"\nkind = "plain"\nrounds = {rounds}\n{top}\n{SOURCE}{RUN}{ARMS}', script, commit)

    def scroll(self, name="widget-scroll", script=SCRIPT, at_least=10, rounds=1):
        read = f"cat {self.device.root}/counter"
        self.device.set("counter", "0")
        toml = f'what = "scrolls"\nkind = "scroll"\nrounds = {rounds}\n{SOURCE}{RUN}[motion]\nread = "{read}"\nat_least = {at_least}\n{ARMS}'
        return self.write(name, toml, script)

    def stored(self):
        path = self.home / "artifacts" / "bench" / "manifest.jsonl"
        lines = path.read_text().splitlines() if path.exists() else []
        return [e for e in map(json.loads, lines) if "bench" in e]

    def only(self):
        entry, = self.stored()
        return json.loads((self.home / "artifacts" / entry["path"]).read_text())


@pytest.fixture
def b(tmp_path):
    return Bench(tmp_path)


# --- a result


def test_arms_interleave_and_each_gets_its_settings(b):
    b.plain(rounds=3)
    b.bench("run", "widget-speed", "--as", "a")
    record = b.only()
    assert [(r["arm"], r["round"]) for r in record["runs"]] == [("a1", 0), ("b2", 0), ("b2", 1), ("a1", 1), ("a1", 2), ("b2", 2)]
    assert [r["settings"] for r in record["runs"]][:2] == [{"speed": 1}, {"speed": 2, "mode": "quick one"}]
    assert record["valid"] and record["summary"]["a1"]["frame_ms"]["n"] == 3
    assert record["summary"]["b2"]["frame_ms"]["median"] == 12


def test_settings_reach_the_run(b):
    b.plain(rounds=1)
    b.bench("run", "widget-speed", "--as", "a")
    entry = next(e for e in map(json.loads, (b.home / "artifacts/bench/manifest.jsonl").read_text().splitlines()) if e["id"] == b.only()["log"])
    log = (b.home / "artifacts" / entry["path"]).read_text()
    assert 'arm=b2 settings={"speed": 2, "mode": "quick one"}' in log


def test_every_result_records_the_kit_the_tool_and_the_stamp(b):
    commit = b.plain()
    done = b.bench("run", "widget-speed", "--as", "a")
    assert "kit kit-3 on base base-7" in done.stdout
    record = b.only()
    assert record["kit_id"] == "kit-3" and record["base"] == "base-7"
    assert record["tool_commit"] == commit and record["tool_changed"] is False
    assert record["stamp"]["kit_id"] == "kit-3" and record["stamp_after"]["kit_id"] == "kit-3"
    assert b.stored()[0]["bench"]["kit_id"] == "kit-3"


def test_a_checkout_with_changes_on_top_says_so(b):
    b.plain(commit=False)
    assert "changes on top" in b.bench("run", "widget-speed", "--as", "a").stderr
    assert b.only()["tool_changed"] is True


def test_no_tool_commit_means_no_run(b):
    b.plain()
    shutil.rmtree(b.kit / ".git")
    assert "definition's commit" in b.bench("run", "widget-speed", "--as", "a", code=1).stderr
    assert not b.device.log.exists() and not b.stored()


def test_no_kit_id_means_nothing_runs(b):
    b.plain(script=f'touch "{b.tmp}/ran"\necho "bench: x 1"\n')
    (b.device.root / "kit_id").unlink()
    assert "no Kit ID" in b.bench("run", "widget-speed", "--as", "a", code=1).stderr
    assert not (b.tmp / "ran").exists() and not b.stored() and not b.device.lock.exists()


def test_an_unknown_binary_means_nothing_runs(b):
    b.write("widget-speed", f'what = "x"\nkind = "plain"\nrounds = 1\n{SOURCE}{RUN}binary = "compositor"\n{ARMS}',
            f'touch "{b.tmp}/ran"\necho "bench: x 1"\n')
    assert "no compositor_sha256" in b.bench("run", "widget-speed", "--as", "a", code=1).stderr
    assert not (b.tmp / "ran").exists() and not b.stored()
    toml = b.home / "dev.toml"
    toml.write_text(toml.read_text() + 'compositor_sha256 = "echo c0ffee"\n')
    b.bench("run", "widget-speed", "--as", "a")
    assert b.only()["binary_sha256"] == "c0ffee"


def test_results_go_to_the_store_and_the_kit_stays_clean(b):
    b.plain()
    b.bench("run", "widget-speed", "--as", "a")
    assert b.git("status", "--porcelain") == ""
    assert (b.home / "artifacts" / "bench").stat().st_mode & 0o077 == 0
    assert b.tool("art", "verify").stdout == ""


def test_the_lock_comes_from_dev_with_does_from_the_toml(b):
    b.plain(top='does = ["close-apps"]', script=f'cp "{b.device.lock}/does" "{b.tmp}/does"\necho "bench: x 1"\n')
    assert "name a go" in b.bench("run", "widget-speed", "--as", "a", code=1).stderr
    assert not b.device.lock.exists() and not b.stored()
    b.bench("run", "widget-speed", "--as", "a", "--go-said", "go ahead")
    assert (b.tmp / "does").read_text() == "close-apps\n" and not b.device.lock.exists()


def test_inside_runs_only_under_the_lock(b):
    b.plain(top='does = ["close-apps"]', script=f'touch "{b.tmp}/ran"\necho "bench: x 1"\n')
    assert "isn't held by nobody" in b.bench("_inside", "widget-speed", "--as", "nobody", code=1).stderr
    assert "--as" in b.bench("_inside", "widget-speed", code=1).stderr
    b.take("a")
    assert "doesn't cover close-apps" in b.bench("_inside", "widget-speed", "--as", "a", code=1).stderr
    assert not (b.tmp / "ran").exists() and not b.stored()


def test_a_held_lock_stays(b):
    b.plain()
    b.take("a")
    made = b.device.field("made")
    b.bench("run", "widget-speed", "--as", "a")
    assert b.device.field("made") == made


# --- one invalid arm invalidates the comparison


@pytest.mark.parametrize("script, words", [
    ('case "$GLASSINE_BENCH_ARM" in b2) exit 3;; esac\necho "bench: x 1"\n', "exit 3"),
    ('case "$GLASSINE_BENCH_ARM" in b2) echo nothing; exit 0;; esac\necho "bench: x 1"\n', "printed no metric"),
    ('case "$GLASSINE_BENCH_ARM" in b2) echo "bench: y 1"; exit 0;; esac\necho "bench: x 1"\n', "metrics differ"),
])
def test_one_invalid_arm_invalidates_the_comparison_and_prints_no_number(b, script, words):
    b.plain(script=script)
    done = b.bench("run", "widget-speed", "--as", "a", code=1)
    assert "INVALID" in done.stdout and words in done.stdout and "median" not in done.stdout
    record = b.only()
    assert record["valid"] is False and record["summary"] is None
    assert all(r["metrics"] is None for r in record["runs"])  # the good arm's number isn't kept either
    assert record["stamp_after"] is None  # never read, so not shown as if it were
    assert [r["arm"] for r in record["runs"]] == ["a1", "b2"]  # it stopped there
    shown = b.bench("show", b.stored()[0]["id"]).stdout
    assert "INVALID" in shown and "median" not in shown


@pytest.mark.parametrize("lines, words", [
    ('echo "bench: frame_ms 100"\necho "bench: frame_ms 1"', "printed twice"),
    ('echo "bench: frame_ms 1e999"', "isn't a finite number"),
    ('echo "bench: frame_ms 1"\necho "bench: dropped nan"', "doesn't parse"),
    ('echo "bench: frame_ms 1"\necho "bench: dropped"', "doesn't parse"),
])
def test_bad_metric_lines_invalidate_the_arm(b, lines, words):
    b.plain(script=lines + "\n", rounds=1)
    done = b.bench("run", "widget-speed", "--as", "a", code=1)
    assert words in done.stdout and "median" not in done.stdout
    assert b.only()["runs"][0]["arm"] == "a1"  # the first arm, and it stopped there


def test_a_device_script_reading_stdin_doesnt_eat_the_rest(b):
    b.plain(script='cat >/dev/null\necho "bench: x 1"\n', rounds=1)
    b.bench("run", "widget-speed", "--as", "a")


def test_a_timeout_is_invalid(b):
    b.write("slow", f'what = "slow"\nkind = "plain"\nrounds = 1\n{SOURCE}[run]\nwhere = "host"\ncommand = ["sh", "{{source}}"]\ntimeout = "1s"\n{ARMS}', "sleep 60\n")
    assert "timed out" in b.bench("run", "slow", "--as", "a", code=1).stdout


def test_a_renderer_changed_under_the_run_invalidates_it(b):
    b.plain(script=f'echo softpipe > "{b.device.root}/renderer"\necho "bench: x 1"\n')
    # the release after this run sees the wrong renderer too, and only says so
    done = b.bench("run", "widget-speed", "--as", "a", code=1)
    assert "renderer changed during the run" in done.stdout and "median" not in done.stdout


def test_a_binary_changed_under_the_run_invalidates_it(b):
    toml = b.home / "dev.toml"
    toml.write_text(toml.read_text() + f'compositor_sha256 = "cat {b.device.root}/hash"\n')
    b.device.set("hash", "a" * 64)
    b.plain(script=f'echo {"b" * 64} > "{b.device.root}/hash"\necho "bench: x 1"\n', rounds=1)
    assert "compositor_sha256 changed" in b.bench("run", "widget-speed", "--as", "a", code=1).stdout


def test_a_kit_changed_under_the_run_invalidates_it(b):
    b.plain(script=f'echo kit-4 > "{b.device.root}/kit_id"\necho "bench: x 1"\n', rounds=1)
    assert "kit_id changed during the run" in b.bench("run", "widget-speed", "--as", "a", code=1).stdout


def test_a_device_gone_after_the_run_keeps_an_invalid_result(b):
    b.plain(script=f'case "$GLASSINE_BENCH_ARM" in b2) touch "{b.device.root}/unreachable";; esac\necho "bench: x 1"\n', rounds=1)
    done = b.bench("run", "widget-speed", "--as", "a", code=1)
    assert "no stamp after the run" in done.stdout and "median" not in done.stdout
    assert b.only()["valid"] is False


# --- motion


def test_scroll_with_motion_proved_is_valid(b):
    b.scroll(script=f'n=$(cat "{b.device.root}/counter"); echo $((n + 50)) > "{b.device.root}/counter"\necho "bench: frame_ms 9"\n')
    done = b.bench("run", "widget-scroll", "--as", "a")
    assert "median 9" in done.stdout
    record = b.only()
    assert record["motion_proved"] is True and record["runs"][0]["counter_after"] == 50


def test_scroll_without_motion_is_stored_invalid_and_prints_no_number(b):
    b.scroll(script='echo "bench: frame_ms 123.5"\n')
    done = b.bench("run", "widget-scroll", "--as", "a", code=1)
    assert "motion not proved" in done.stdout and "123" not in done.stdout + done.stderr
    record = b.only()
    assert record["valid"] is False and record["motion_proved"] is False
    assert "123" not in json.dumps(record["runs"]) and record["summary"] is None
    assert "123" not in b.bench("show", b.stored()[0]["id"]).stdout


def test_a_script_saying_it_moved_doesnt_count(b):
    b.scroll(script='echo "bench: motion_proved 1"\necho "motion_proved=true"\necho "bench: frame_ms 9"\n')
    b.bench("run", "widget-scroll", "--as", "a", code=1)
    assert b.only()["motion_proved"] is False


def test_too_little_motion_is_not_motion(b):
    b.scroll(at_least=100, script=f'n=$(cat "{b.device.root}/counter"); echo $((n + 99)) > "{b.device.root}/counter"\necho "bench: frame_ms 9"\n')
    b.bench("run", "widget-scroll", "--as", "a", code=1)
    assert b.only()["runs"][0]["motion_proved"] is False


def test_an_unreadable_counter_proves_nothing(b):
    b.scroll(script='echo "bench: frame_ms 9"\n')
    (b.device.root / "counter").write_text("not a number\n")
    b.bench("run", "widget-scroll", "--as", "a", code=1)
    assert b.only()["motion_proved"] is False


def test_a_counter_unreadable_after_the_arm_proves_nothing(b):
    b.scroll(script=f'echo gone > "{b.device.root}/counter"\necho "bench: frame_ms 9"\n')
    done = b.bench("run", "widget-scroll", "--as", "a", code=1)
    assert "went 0 to None" in done.stdout
    assert b.only()["motion_proved"] is False


def test_every_arm_must_prove_it(b):
    script = f'case "$GLASSINE_BENCH_ARM" in a1) n=$(cat "{b.device.root}/counter"); echo $((n + 50)) > "{b.device.root}/counter";; esac\necho "bench: frame_ms 9"\n'
    b.scroll(script=script)
    done = b.bench("run", "widget-scroll", "--as", "a", code=1)
    assert "b2" in done.stdout and "median" not in done.stdout


# --- host runs and private inputs


def test_gpu_host_records_what_else_was_running(b):
    b.write("gpu", f'what = "on this host"\nkind = "gpu-host"\ndevice = false\nrounds = 1\n{SOURCE}[run]\nwhere = "host"\ncommand = ["sh", "{{source}}"]\ntimeout = "1m"\n{ARMS}', 'echo "bench: x 1"\n')
    b.bench("run", "gpu")
    run = b.only()["runs"][0]
    assert run["others"] and len(run["load"]) == 3
    assert not b.device.log.exists()  # no lock, nothing near the device


def test_gpu_host_with_no_process_list_is_invalid(b):
    b.write("gpu", f'what = "on this host"\nkind = "gpu-host"\ndevice = false\nrounds = 1\n{SOURCE}[run]\nwhere = "host"\ncommand = ["/bin/sh", "{{source}}"]\ntimeout = "1m"\n{ARMS}', 'echo "bench: x 1"\n')
    fake = b.tmp / "nops"
    fake.mkdir()
    (fake / "ps").write_text("#!/bin/sh\nexit 1\n")
    (fake / "ps").chmod(0o755)
    b.env["PATH"] = f"{fake}:{b.env['PATH']}"
    assert "isn't known" in b.bench("run", "gpu", code=1).stdout


def test_a_host_run_off_the_device_isnt_told_how_to_reach_it(b):
    b.write("host", f'what = "x"\nkind = "plain"\ndevice = false\nrounds = 1\n{SOURCE}[run]\nwhere = "host"\ncommand = ["sh", "{{source}}"]\ntimeout = "1m"\n{ARMS}',
            '[ -z "$GLASSINE_DEV_SSH$GLASSINE_DEV_STORE" ] && echo "bench: x 1"\n')
    b.bench("run", "host")
    assert b.only()["kit_id"] is None and "on this host" in b.bench("show", b.stored()[0]["id"]).stdout


def test_source_and_dir_are_filled_in_within_an_argument(b):
    command = '["sh", "-c", "sh \\"${1#--src=}\\"", "x", "--src={source}"]'
    b.write("host", f'what = "x"\nkind = "plain"\ndevice = false\nrounds = 1\n{SOURCE}[run]\nwhere = "host"\ncommand = {command}\ntimeout = "1m"\n{ARMS}',
            'echo "bench: x 1"\n')
    b.bench("run", "host")


def test_a_plan_longer_than_the_lock_is_warned_of(b):
    b.plain(top='expect = "1m"')
    assert "longer than expect" in b.bench("run", "widget-speed", "--as", "a").stderr


def input_bench(b, ident):
    b.write("private", f'what = "x"\nkind = "plain"\ndevice = false\nrounds = 1\ninputs = ["{ident}"]\n{SOURCE}[run]\nwhere = "host"\ncommand = ["sh", "{{source}}"]\ntimeout = "1m"\n{ARMS}',
            'wc -c "$GLASSINE_BENCH_INPUTS" >/dev/null && echo "bench: x 1"\n')


def test_private_input_not_here_is_skipped_never_a_pass(b):
    input_bench(b, "0123456789ab")
    assert "skipped: no private input" in b.bench("run", "private", code=77).stdout
    assert not b.stored()


def test_private_input_goes_by_id(b):
    stream = b.tmp / "stream"
    stream.write_text("captured\n")
    ident = b.tool("art", "add", stream, "--to", "captures", "--class", "user-data", "--what", "a stream", "--keep", "user").stdout.split()[0]
    input_bench(b, ident)
    b.bench("run", "private")
    assert b.only()["inputs"] == [ident]
    assert b.stored()[0]["from"] == [ident]


def test_a_private_input_needs_a_host_run(b):
    b.write("bad", f'what = "x"\nkind = "plain"\nrounds = 1\ninputs = ["0123456789ab"]\n{SOURCE}{RUN}{ARMS}')
    assert "host run" in b.bench("check", code=1).stderr


# --- eye tests


def test_an_eye_test_is_a_result_with_user_provenance(b):
    binary = b.tmp / "widget"
    binary.write_text("a build\n")
    done = b.bench("eye", "--binary", binary, "--setting", "speed=2", "--setting", "mode=quick one", "--content", "a made-up page",
                   "--said", "ghosting is gone", "--label", "ghost-check")
    assert "ghosting is gone" in done.stdout and not b.device.lock.exists()
    record = b.only()
    assert record["provenance"] == "user" and record["settings"] == {"speed": "2", "mode": "quick one"}
    assert record["content"] == "a made-up page" and len(record["binary_sha256"]) == 64 and record["stamp"]["base"] == "base-7"
    assert record["kit_id"] == "kit-3"
    entry = b.stored()[0]
    assert entry["bench"]["provenance"] == "user"
    assert "eye test" in b.bench("results").stdout


def test_an_eye_test_needs_binary_settings_content_and_words(b):
    good = ["--binary", "a" * 64, "--setting", "k=v", "--content", "a page", "--said", "fine"]
    for missing in range(0, len(good), 2):
        b.bench("eye", *good[:missing], *good[missing + 2:], code=2)
    assert "KEY=VALUE" in b.bench("eye", "--binary", "a" * 64, "--setting", "nokey", "--content", "a", "--said", "b", code=1).stderr
    assert "an eye test says it" in b.bench("eye", "--binary", "a" * 64, "--setting", "k=v", "--content", " ", "--said", "b", code=1).stderr
    assert "a sha256 or a file" in b.bench("eye", "--binary", "nothing", "--setting", "k=v", "--content", "a", "--said", "b", code=1).stderr
    assert not b.stored()


def test_an_eye_test_takes_the_binary_from_the_stamp_if_it_can(b):
    args = ["--binary", "compositor", "--setting", "k=v", "--content", "a page", "--said", "fine"]
    assert "doesn't say its hash" in b.bench("eye", *args, code=1).stderr
    toml = b.home / "dev.toml"
    toml.write_text(toml.read_text() + 'compositor_sha256 = "echo c0ffee"\n')
    b.bench("eye", *args)
    assert b.only()["binary_sha256"] == "c0ffee"


def test_an_eye_test_after_the_device_has_gone_says_so(b):
    (b.device.root / "unreachable").touch()
    done = b.bench("eye", "--binary", "a" * 64, "--setting", "k=v", "--content", "a page", "--said", "fine")
    assert "no stamp" in done.stderr and "unknown" in b.only()["stamp"]


def test_an_eye_tests_private_content_goes_by_id(b):
    b.bench("eye", "--binary", "a" * 64, "--setting", "k=v", "--content", "a page", "--said", "ok", "--input", "not-an-id", code=1)
    b.bench("eye", "--binary", "a" * 64, "--setting", "k=v", "--content", "a page", "--said", "ok", "--input", "0123456789ab")
    assert b.stored()[0]["from"] == ["0123456789ab"]


# --- reading results


def test_results_list_ids_and_validity_without_numbers(b):
    b.plain(script='echo "bench: frame_ms 123.5"\n', rounds=1)
    b.bench("run", "widget-speed", "--as", "a")
    listed = b.bench("results", "widget-speed").stdout
    assert " valid " in listed and "kit kit-3" in listed and "123.5" not in listed
    assert b.bench("results", "other").stdout == ""
    assert "123.5" in b.bench("show", b.stored()[0]["id"]).stdout


def test_show_refuses_what_isnt_a_bench_result(b):
    stream = b.tmp / "stream"
    stream.write_text("x\n")
    ident = b.tool("art", "add", stream, "--to", "captures", "--class", "derived", "--what", "x", "--keep", "user").stdout.split()[0]
    assert "isn't a bench result" in b.bench("show", ident, code=1).stderr


# --- definitions


@pytest.mark.parametrize("toml, words", [
    (f'kind = "plain"\nrounds = 1\n{SOURCE}{RUN}{ARMS}', "what says"),
    (f'what = "x"\nrounds = 1\n{SOURCE}{RUN}{ARMS}', "kind is"),
    (f'what = "x"\nkind = "plain"\n{SOURCE}{RUN}{ARMS}', "rounds"),
    (f'what = "x"\nkind = "plain"\nrounds = 1\n{SOURCE}{RUN}', "at least one"),
    (f'what = "x"\nkind = "plain"\nrounds = 1\n{SOURCE}{RUN}[[arm]]\nname = "a"\n[[arm]]\nname = "a"\n', "two arms"),
    (f'what = "x"\nkind = "plain"\nrounds = 1\n{SOURCE}{RUN}[[arm]]\nname = "a"\nsetings = {{}}\n', "unknown keys"),
    (f'what = "x"\nkind = "scroll"\nrounds = 1\n{SOURCE}{RUN}{ARMS}', "must prove motion"),
    (f'what = "x"\nkind = "plain"\nrounds = 1\n{SOURCE}{RUN}[motion]\nread = "x"\nat_least = 1\n{ARMS}', "is for kind = scroll"),
    (f'what = "x"\nkind = "scroll"\nrounds = 1\n{SOURCE}{RUN}[motion]\nread = "cat /run/widget.count"\nat_least = 1\n{ARMS}', "FIFO"),
    (f'what = "x"\nkind = "scroll"\nrounds = 1\n{SOURCE}{RUN}[motion]\nread = "x"\nat_least = 0\n{ARMS}', "at_least"),
    (f'what = "x"\nkind = "plain"\nrounds = 1\n[source]\nfile = "nope.sh"\n{RUN}{ARMS}', "file nope.sh isn't there"),
    (f'what = "x"\nkind = "gpu-host"\nrounds = 1\n{SOURCE}{RUN}{ARMS}', "where = host"),
    (f'what = "x"\nkind = "scroll"\ndevice = false\nrounds = 1\n{SOURCE}[run]\nwhere = "host"\ncommand = ["x"]\ntimeout = "1m"\n[motion]\nread = "x"\nat_least = 1\n{ARMS}', "device can't"),
])
def test_bad_definitions_are_refused_in_words(b, toml, words):
    b.write("bad", textwrap.dedent(toml))
    assert words in b.bench("check", code=1).stderr
    assert words in b.bench("run", "bad", "--as", "a", code=1).stderr
    assert not b.device.log.exists()


def test_a_device_script_that_reads_run_is_refused(b):
    b.plain(script="cat /run/glassine/ctl\n")
    assert "FIFO" in b.bench("check", code=1).stderr
    assert not b.device.log.exists()


def test_list(b):
    assert b.bench("list").stdout == ""
    b.plain(top='does = ["close-apps"]')
    assert "widget-speed  plain  a made-up bench  (does: close-apps)" in b.bench("list").stdout


def test_this_repository_ships_only_sound_definitions():
    tools = Path(__file__).parents[1] / "tools"
    env = {k: v for k, v in os.environ.items() if k != "GLASSINE_KIT"}
    done = subprocess.run([sys.executable, str(tools / "bench"), "check"], env=env, capture_output=True, text=True, timeout=20)
    assert done.returncode == 0, done.stderr
