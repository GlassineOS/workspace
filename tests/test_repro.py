# SPDX-License-Identifier: GPL-2.0-or-later
"""repro against dev's fake device. The reproducers are made up here: the repository holds none of them."""
import json
import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from test_dev import Rig, TOOLS

import artstore


class Kit(Rig):
    def __init__(self, tmp_path):
        super().__init__(tmp_path)
        self.kit = tmp_path / "kit"
        self.kit.mkdir()
        self.env["GLASSINE_KIT"] = str(self.kit)
        self.home = Path(self.env["GLASSINE_WORKSPACE"])

    def tool(self, name, *args, code=0, seconds=30):
        done = subprocess.run([sys.executable, str(TOOLS / name), *map(str, args)], capture_output=True, text=True,
                              env=self.env, cwd=self.tmp, timeout=seconds)
        assert done.returncode == code, done.stdout + done.stderr
        return done

    def repro(self, *args, code=0, seconds=30):
        return self.tool("repro", *args, code=code, seconds=seconds)

    def define(self, name, toml, **files):
        folder = self.kit / "repro" / name
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "repro.toml").write_text(textwrap.dedent(toml))
        for file, text in files.items():
            (folder / file.replace("_", ".")).write_text(text)
        return folder

    def simple(self, name="widget-hang", script="echo fine\n", more="", top="", binary="compositor"):
        return self.define(name, f"""\
            what = "a made-up reproducer"
            {top}
            [source]
            file = "run.sh"
            [run]
            where = "device"
            timeout = "1m"
            binary = "{binary}"
            {more}
            """, run_sh=script)

    def results(self):
        path = self.home / "artifacts" / "repro" / "manifest.jsonl"
        lines = path.read_text().splitlines() if path.exists() else []
        return [e for e in map(json.loads, lines) if "repro" in e]

    def record(self, entry):
        return json.loads((self.home / "artifacts" / entry["path"]).read_text())


@pytest.fixture
def kit(tmp_path):
    return Kit(tmp_path)


def set_probe(kit, name, command):
    toml = kit.home / "dev.toml"
    toml.write_text(toml.read_text() + f"{name} = {json.dumps(command)}\n")


# --- running under the lock


def test_inside_runs_only_under_the_lock(kit):
    kit.simple(script=f'touch "{kit.tmp}/ran"\n', top='does = ["close-apps"]')
    set_probe(kit, "compositor_sha256", "echo c0ffee")
    assert "isn't held by nobody" in kit.repro("_inside", "widget-hang", "--as", "nobody", code=1).stderr
    assert "--as" in kit.repro("_inside", "widget-hang", code=1).stderr
    kit.take("a")
    assert "doesn't cover close-apps" in kit.repro("_inside", "widget-hang", "--as", "a", code=1).stderr
    assert not (kit.tmp / "ran").exists() and not kit.results()


def test_a_run_stores_the_result_with_stamp_and_hashes_and_frees_the_lock(kit):
    kit.simple(more='[pass]\noutput_has = "fine"')
    set_probe(kit, "compositor_sha256", "echo c0ffee")
    done = kit.repro("run", "widget-hang", "--as", "a")
    assert "widget-hang: pass" in done.stdout and "fine" not in done.stdout  # output goes to the log
    assert not kit.device.lock.exists()
    entry, = kit.results()
    record = kit.record(entry)
    assert record["base"] == "base-7" and record["result"] == "pass" and record["binary_sha256"] == "c0ffee"
    assert record["stamp"]["base"] == "base-7" and record["stamp"]["device_time"]  # dev's stamp, as dev prints it
    assert len(record["source_sha256"]) == 64 and record["stamp"]["kit_id"] == "kit-3"
    log = artstore_log(kit, record)
    assert log.read_text() == "fine\n"


def artstore_log(kit, record):
    lines = (kit.home / "artifacts" / "repro" / "manifest.jsonl").read_text().splitlines()
    entry = next(e for e in map(json.loads, lines) if e["id"] == record["log"])
    return kit.home / "artifacts" / entry["path"]


def test_results_land_in_the_store_and_nowhere_near_a_checkout(kit):
    kit.simple()
    kit.repro("run", "widget-hang", "--as", "a")
    store = kit.home / "artifacts" / "repro"
    assert store.stat().st_mode & 0o077 == 0
    assert not list(kit.kit.rglob("*.json")) and not list(kit.kit.rglob("*.log"))
    assert not [p for p in store.iterdir() if p.name.startswith(".new-")]
    assert kit.tool("art", "verify").stdout == ""  # indexed, every file, in a folder art knows


def test_does_comes_from_the_toml_and_lands_in_the_lock(kit):
    probe = f'cp "{kit.device.lock}/does" "{kit.tmp}/does-seen"; cp "{kit.device.lock}/go" "{kit.tmp}/go-seen"\n'
    kit.simple(top='does = ["close-apps", "reboot"]', script=probe)
    kit.repro("run", "widget-hang", "--as", "a", "--go-said", "go ahead")
    assert (kit.tmp / "does-seen").read_text() == "close-apps,reboot\n"
    assert "go ahead" in (kit.tmp / "go-seen").read_text()
    assert kit.record(kit.results()[0])["does"] == ["close-apps", "reboot"]


def test_does_without_a_go_is_refused_before_anything_runs(kit):
    kit.simple(top='does = ["close-apps"]', script=f'touch "{kit.tmp}/ran"\n')
    done = kit.repro("run", "widget-hang", "--as", "a", code=1)
    assert "needs" not in done.stdout and "name a go" in done.stderr
    assert not (kit.tmp / "ran").exists() and not kit.device.lock.exists() and not kit.results()


def test_the_hub_refusing_the_go_stops_the_run(kit):
    kit.simple(top='does = ["close-apps"]', script=f'touch "{kit.tmp}/ran"\n')
    done = kit.repro("run", "widget-hang", "--as", "a", "--go", "go-1", code=1)  # no hub: the answer is "unknown"
    assert "unknown" in done.stderr
    assert not (kit.tmp / "ran").exists() and not kit.device.lock.exists()


def test_a_held_lock_is_joined_and_stays(kit):
    kit.simple()
    kit.take("a")
    made = kit.device.field("made")
    kit.repro("run", "widget-hang", "--as", "a")
    assert kit.device.field("made") == made
    kit.repro("run", "widget-hang", "--as", "a")
    assert kit.device.field("made") == made


def test_another_holder_refuses_and_nothing_runs(kit):
    kit.simple(script=f'touch "{kit.tmp}/ran"\n')
    kit.take("other")
    kit.repro("run", "widget-hang", "--as", "a", code=1)
    assert not (kit.tmp / "ran").exists() and not kit.results()
    assert kit.device.field("owner") == "other"


def test_a_dirty_device_refuses_unless_said(kit):
    kit.simple()
    kit.device.set("renderer", "softpipe")
    kit.repro("run", "widget-hang", "--as", "a", code=1)
    assert not kit.results()
    kit.repro("run", "widget-hang", "--as", "a", "--dirty", "testing the fallback")
    record = kit.record(kit.results()[0])
    assert record["stamp"]["dirty"] == "testing the fallback"
    assert any("dirty" in w for w in record["warnings"])


def test_the_lock_is_held_while_it_runs(kit):
    kit.simple(script=f'cat "{kit.device.lock}/purpose" > "{kit.tmp}/seen"\n')
    kit.repro("run", "widget-hang", "--as", "a")
    assert (kit.tmp / "seen").read_text() == "repro widget-hang\n"


def test_no_stamp_means_no_run(kit):
    kit.simple()
    kit.take("a")
    (kit.device.root / "unreachable").touch()
    done = kit.repro("run", "widget-hang", "--as", "a", code=1)
    assert not kit.results() and "can't reach" in done.stderr


# --- what counts as a pass


@pytest.mark.parametrize("more, script, result", [
    ("", "exit 3\n", "fail"),
    ("[pass]\nexit = 3", "exit 3\n", "pass"),
    ('[pass]\noutput_has = "^ok$"', "echo nope\n", "fail"),
    ('[pass]\noutput_has = "^ok$"', "echo ok\n", "pass"),
    ('[pass]\noutput_lacks = "hangcheck"', "echo hangcheck fired\n", "fail"),
])
def test_the_pass_criterion_decides(kit, more, script, result):
    kit.simple(more=more, script=script)
    kit.repro("run", "widget-hang", "--as", "a", code=0 if result == "pass" else 1)
    assert kit.results()[0]["repro"]["result"] == result


def test_a_timeout_is_a_fail_and_the_lock_goes_back(kit):
    kit.define("slow", """\
        what = "sleeps"
        [source]
        file = "s.sh"
        [run]
        where = "host"
        command = ["sh", "{source}"]
        timeout = "1s"
        """, s_sh="sleep 60\n")
    done = kit.repro("run", "slow", "--as", "a", code=1)
    assert "timed out" in done.stdout and not kit.device.lock.exists()
    assert kit.results()[0]["repro"]["result"] == "fail"


def test_a_signal_is_stored_as_stopped_and_the_lock_goes_back(kit):
    kit.define("slow", """\
        what = "sleeps"
        [source]
        file = "s.sh"
        [run]
        where = "host"
        command = ["sh", "{source}"]
        timeout = "1m"
        """, s_sh=f'touch "{kit.tmp}/started"; sleep 60\n')
    child = subprocess.Popen([sys.executable, str(TOOLS / "repro"), "run", "slow", "--as", "a"], env=kit.env, cwd=kit.tmp,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
    deadline = time.monotonic() + 15
    while not (kit.tmp / "started").exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    os.killpg(child.pid, signal.SIGTERM)
    child.communicate(timeout=20)
    assert kit.results()[0]["repro"]["result"] == "stopped"
    assert not kit.device.lock.exists()


def test_a_host_command_gets_its_source_folder_and_the_ssh_command(kit):
    folder = kit.define("hosty", """\
        what = "runs here"
        [source]
        file = "s.sh"
        [run]
        where = "host"
        command = ["sh", "{source}", "{dir}"]
        timeout = "1m"
        [pass]
        output_has = "ssh=.*fake-ssh"
        """, s_sh='echo "dir=$1 ssh=$GLASSINE_DEV_SSH"\n')
    kit.repro("run", "hosty", "--as", "a")
    assert str(folder) in artstore_log(kit, kit.record(kit.results()[0])).read_text()


def test_a_host_only_reproducer_takes_no_lock_and_says_its_base(kit):
    kit.define("offline", """\
        what = "never near the device"
        device = false
        [source]
        file = "s.sh"
        [run]
        where = "host"
        command = ["sh", "{source}"]
        timeout = "1m"
        """, s_sh="true\n")
    assert "--base" in kit.repro("run", "offline", code=1).stderr
    kit.device.log.unlink(missing_ok=True)
    binary = kit.tmp / "widget"
    binary.write_text("a build\n")
    kit.repro("run", "offline", "--base", "base-9", "--binary-file", binary)
    assert not kit.device.log.exists()  # nothing went near the device
    record = kit.record(kit.results()[0])
    assert record["base"] == "base-9" and record["binary_sha256"] == artstore.digest(binary)


def host_only(kit, name="offline"):
    return kit.define(name, """\
        what = "never near the device"
        device = false
        [source]
        file = "s.sh"
        [run]
        where = "host"
        command = ["sh", "{source}"]
        timeout = "1m"
        """)


def test_a_source_that_is_a_link_out_of_the_folder_is_refused(kit):
    outside = kit.tmp / "outside.sh"
    outside.write_text(f'touch "{kit.tmp}/ran"\n')
    folder = host_only(kit)
    (folder / "s.sh").symlink_to(outside)
    assert "leaves" in kit.repro("check", code=1).stderr
    assert "leaves" in kit.repro("run", "offline", "--base", "b", code=1).stderr
    assert "leaves" in kit.repro("_inside", "offline", "--base", "b", code=1).stderr  # not only run's own check
    assert not (kit.tmp / "ran").exists() and not kit.results()


def test_a_source_that_is_a_link_inside_the_folder_is_fine(kit):
    folder = host_only(kit)
    (folder / "real.sh").write_text("echo fine\n")
    (folder / "s.sh").symlink_to("real.sh")
    kit.repro("check")
    kit.repro("run", "offline", "--base", "b")
    assert kit.record(kit.results()[0])["result"] == "pass"


def test_a_definition_folder_or_toml_that_is_a_link_out_is_refused(kit):
    elsewhere = kit.tmp / "elsewhere"
    elsewhere.mkdir()
    (kit.kit / "repro").mkdir()
    (kit.kit / "repro" / "linked").symlink_to(elsewhere)
    (elsewhere / "repro.toml").write_text('what="x"\n[source]\nfile="s"\n[run]\nwhere="host"\ncommand=["true"]\ntimeout="1m"\n')
    assert "leaves" in kit.repro("check", "linked", code=1).stderr
    folder = host_only(kit, "toml-linked")
    (folder / "repro.toml").rename(kit.tmp / "moved.toml")
    (folder / "repro.toml").symlink_to(kit.tmp / "moved.toml")
    assert "leaves" in kit.repro("check", "toml-linked", code=1).stderr


# --- private inputs


def test_a_private_input_thats_not_here_is_skipped_not_a_pass(kit):
    kit.define("private", """\
        what = "from a captured stream"
        [source]
        artifact = "0123456789ab"
        [run]
        where = "host"
        command = ["replay", "{source}"]
        timeout = "1m"
        """)
    done = kit.repro("run", "private", "--as", "a", code=77)
    assert "skipped: no private input" in done.stdout
    assert not kit.device.log.exists()  # no lock, no device
    assert not kit.results()
    assert kit.repro("gate", "base-7", code=1).stdout.startswith("missing")


def test_a_private_input_thats_here_runs_and_is_named_by_id(kit):
    stream = kit.tmp / "stream"
    stream.write_text("captured\n")
    added = kit.tool("art", "add", stream, "--to", "captures", "--class", "user-data", "--what", "a stream", "--keep", "user")
    ident = added.stdout.split()[0]
    kit.define("private", f"""\
        what = "from a captured stream"
        [source]
        artifact = "{ident}"
        [run]
        where = "host"
        command = ["cat", "{{source}}"]
        timeout = "1m"
        [pass]
        output_has = "captured"
        """)
    kit.repro("run", "private", "--as", "a")
    record = kit.record(kit.results()[0])
    assert record["inputs"] == [ident] and "captured" not in json.dumps(record)
    assert kit.results()[0]["from"] == [ident]


def test_a_private_input_by_its_whole_sha256_runs_too(kit):
    stream = kit.tmp / "stream"
    stream.write_text("captured\n")
    sha = artstore.digest(stream)  # art add moves the file
    kit.tool("art", "add", stream, "--to", "captures", "--class", "user-data", "--what", "a stream", "--keep", "user")
    kit.define("private", f"""\
        what = "from a captured stream"
        [source]
        artifact = "{sha}"
        [run]
        where = "host"
        command = ["cat", "{{source}}"]
        timeout = "1m"
        [pass]
        output_has = "captured"
        """)
    kit.repro("check")
    kit.repro("run", "private", "--as", "a")
    assert kit.record(kit.results()[0])["result"] == "pass"


def test_a_private_input_changed_on_disk_is_refused(kit):
    stream = kit.tmp / "stream"
    stream.write_text("captured\n")
    ident = kit.tool("art", "add", stream, "--to", "captures", "--class", "user-data", "--what", "a stream", "--keep", "user").stdout.split()[0]
    kit.define("private", f"""\
        what = "from a captured stream"
        [source]
        artifact = "{ident}"
        [run]
        where = "host"
        command = ["cat", "{{source}}"]
        timeout = "1m"
        """)
    stored = next((kit.home / "artifacts" / "captures").glob(f"{ident}-*"))
    stored.chmod(0o600)
    stored.write_text("something else\n")
    assert "doesn't match its id" in kit.repro("run", "private", "--as", "a", code=1).stderr
    assert not kit.device.log.exists()


# --- warnings


def test_a_base_other_than_said_warns_and_goes_under_what_the_device_says(kit):
    kit.simple()
    done = kit.repro("run", "widget-hang", "--as", "a", "--base", "base-1")
    assert "warning" in done.stderr and "base-7" in done.stderr
    assert kit.results()[0]["repro"]["base"] == "base-7"


def test_a_binary_other_than_earlier_on_this_base_warns(kit):
    kit.simple()
    set_probe(kit, "compositor_sha256", f"cat {kit.device.root}/hash")
    kit.device.set("hash", "a" * 64)
    assert "warning" not in kit.repro("run", "widget-hang", "--as", "a").stderr
    kit.device.set("hash", "b" * 64)
    assert "aaaaaaaaaaaa" in kit.repro("run", "widget-hang", "--as", "a").stderr
    rows = json.loads(kit.repro("gate", "base-7", "--json").stdout)["rows"]
    assert any("2 different binaries" in w for w in rows[0]["warnings"])


def test_a_bound_binary_warns_and_says_so_at_the_gate(kit):
    kit.simple()
    kit.device.override("loop-widget", owner="a", boot_id="boot-1", target="/usr/bin/widget", value="x")
    kit.device.set("mountinfo", "20 1 0:20 /over /usr/bin/widget rw - ext4 /dev/root rw")
    assert "bound" in kit.repro("run", "widget-hang", "--as", "a").stderr
    assert kit.record(kit.results()[0])["stamp"]["bound"] == {"loop-widget": "/usr/bin/widget"}
    done = kit.repro("gate", "base-7", code=1)  # it passed, but not as the base
    assert done.stdout.startswith("bound") and "bound over a live binary" in done.stdout
    assert json.loads(kit.repro("gate", "base-7", "--json", code=1).stdout)["rows"][0]["state"] == "bound"
    kit.device.set("mountinfo", "20 1 0:20 / / rw - ext4 /dev/root rw\n")
    (kit.device.store / "overrides.d" / "loop-widget").unlink()
    kit.repro("run", "widget-hang", "--as", "a")  # a later run without the bind stands for the base
    assert kit.repro("gate", "base-7").stdout.startswith("pass")


def test_no_hash_in_the_stamp_says_so(kit):
    kit.simple()
    assert "no hash from the stamp" in kit.repro("run", "widget-hang", "--as", "a").stderr
    assert kit.results()[0]["repro"]["binary_sha256"] is None


def test_an_unknown_base_says_the_gate_cant_count_it(kit):
    kit.simple()
    toml = kit.home / "dev.toml"
    toml.write_text(toml.read_text().replace('base = "echo base-7"\n', ""))
    assert "can't count" in kit.repro("run", "widget-hang", "--as", "a").stderr
    assert kit.results()[0]["repro"]["base"] == "unknown"


# --- the gate


def test_gate_lists_missing_and_failed_and_old_patches_and_runs_nothing(kit):
    kit.simple("passes", top='fixes = ["0001-covered.patch"]')
    kit.simple("fails", script="exit 1\n")
    kit.simple("never-ran", script=f'touch "{kit.tmp}/never"\n')
    kit.repro("run", "passes", "--as", "a")
    kit.repro("run", "fails", "--as", "a", code=1)
    before = kit.device.log.read_text()
    patches = kit.tmp / "patches"
    patches.mkdir()
    (patches / "0001-covered.patch").touch()
    (patches / "0002-old.patch").touch()
    done = kit.repro("gate", "base-7", "--patches", patches, code=1)
    lines = done.stdout.splitlines()
    state = {line.split(":")[0].split()[-1]: line.split()[0] for line in lines}
    assert state["passes"] == "pass" and state["fails"] == "failed" and state["never-ran"] == "missing"
    assert any(line.startswith("no reproducer") and "0002-old.patch" in line for line in lines)
    assert not any("0001-covered.patch" in line for line in lines)
    assert kit.device.log.read_text() == before  # no take, no stamp, nothing on the device
    assert not (kit.tmp / "never").exists()


def test_gate_is_per_base_and_the_last_result_counts(kit):
    kit.simple(script=f'[ -e "{kit.tmp}/good" ]\n')
    kit.repro("run", "widget-hang", "--as", "a", code=1)
    assert "failed" in kit.repro("gate", "base-7", code=1).stdout
    assert "missing" in kit.repro("gate", "base-8", code=1).stdout
    (kit.tmp / "good").touch()
    kit.repro("run", "widget-hang", "--as", "a")
    assert kit.repro("gate", "base-7").stdout.startswith("pass")


def test_a_no_reproducer_row_alone_doesnt_fail_the_gate(kit):
    done = kit.repro("gate", "base-7", "--patch", "0009-old.patch")
    assert done.stdout.startswith("no reproducer") and "0009-old.patch" in done.stdout


def test_gate_with_a_patch_list_file(kit):
    listing = kit.tmp / "list"
    listing.write_text("# old ones\n0003-x.patch\n\n")
    assert "0003-x.patch" in kit.repro("gate", "base-7", "--patches", listing).stdout
    assert "neither" in kit.repro("gate", "base-7", "--patches", kit.tmp / "nope", code=1).stderr


# --- definitions


@pytest.mark.parametrize("toml, words", [
    ('[source]\nfile="r"\n[run]\nwhere="device"\ntimeout="1m"', "what says"),
    ('what="x"\n[run]\nwhere="device"\ntimeout="1m"', "needs [source]"),
    ('what="x"\n[source]\nfile="r"\nartifact="0123456789ab"\n[run]\nwhere="device"\ntimeout="1m"', "one of"),
    ('what="x"\n[source]\nartifact="a-private-name"\n[run]\nwhere="device"\ntimeout="1m"', "goes by id"),
    ('what="x"\n[source]\nartifact="0123456789abcdef"\n[run]\nwhere="device"\ntimeout="1m"', "goes by id"),  # a prefix resolve doesn't match
    ('what="x"\n[source]\nfile="../up"\n[run]\nwhere="device"\ntimeout="1m"', "inside"),
    ('what="x"\n[source]\nfile="r"\n[run]\nwhere="device"', "every run is bounded"),
    ('what="x"\n[source]\nfile="r"\n[run]\nwhere="moon"\ntimeout="1m"', "where is"),
    ('what="x"\n[source]\nfile="r"\n[run]\nwhere="host"\ntimeout="1m"', "needs command"),
    ('what="x"\n[source]\nfile="r"\n[run]\nwhere="device"\ntimeout="1m"\ncommand=["x"]', "for the host"),
    ('what="x"\n[source]\nfile="r"\n[run]\nwhere="device"\ntimeout="1m"\n[pass]\noutput_hs="x"', "unknown keys"),
    ('what="x"\n[source]\nfile="r"\n[run]\nwhere="device"\ntimeout="1m"\n[pass]\noutput_has="("', "output_has"),
    ('what="x"\nfixs=["y"]\n[source]\nfile="r"\n[run]\nwhere="device"\ntimeout="1m"', "unknown keys"),
    ('what="x"\ndoes=["Close Apps"]\n[source]\nfile="r"\n[run]\nwhere="device"\ntimeout="1m"', "action name"),
    ('what="x"\ndevice=false\ndoes=["a"]\n[source]\nfile="r"\n[run]\nwhere="host"\ncommand=["x"]\ntimeout="1m"', "device can't"),
    ('what="x"\nexpect="soon"\n[source]\nfile="r"\n[run]\nwhere="device"\ntimeout="1m"', "duration"),
    ('what="x"\n[source]\nfile="r"\n[run]\nwhere="device"\ntimeout="1m"\nbinary="thing"', "binary is"),
])
def test_bad_definitions_are_refused_in_words(kit, toml, words):
    kit.define("bad", toml)
    assert words in kit.repro("check", code=1).stderr
    kit.device.log.unlink(missing_ok=True)
    assert words in kit.repro("run", "bad", "--as", "a", code=1).stderr
    assert not kit.device.log.exists()


@pytest.mark.parametrize("script, bad", [
    ("cat /run/widget.ctl\n", True),
    ("ls /run/widget*\n", True),
    ("echo set > /run/widget*\n", True),
    ("echo set > /run/widget.ctl\n", False),
    ("echo set >> '/run/widget.ctl'\n", False),
    ("cat /proc/self/mountinfo\n", False),
])
def test_a_device_script_never_reads_run(kit, script, bad):
    kit.simple(script=script)
    done = kit.repro("check", code=1 if bad else 0)
    assert ("FIFO" in done.stderr) == bad


def test_list_and_the_absent_cases(kit):
    assert kit.repro("list").stdout == ""
    assert "no repro/nope" in kit.repro("run", "nope", "--as", "a", code=1).stderr
    kit.simple(top='does = ["close-apps"]')
    assert "widget-hang  a made-up reproducer  (does: close-apps)" in kit.repro("list").stdout
    assert "--as" in kit.repro("run", "widget-hang", code=1).stderr


def test_this_repository_ships_only_sound_definitions():
    env = {k: v for k, v in os.environ.items() if k != "GLASSINE_KIT"}
    done = subprocess.run([sys.executable, str(TOOLS / "repro"), "check"], env=env, capture_output=True, text=True, timeout=20)
    assert done.returncode == 0, done.stderr


def test_the_output_never_reaches_the_terminal(kit):
    kit.simple(script="echo private-looking-output\n")
    done = kit.repro("run", "widget-hang", "--as", "a")
    assert "private-looking-output" not in done.stdout + done.stderr
