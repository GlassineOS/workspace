# SPDX-License-Identifier: GPL-2.0-or-later
"""dev against a fake device: a folder, a stand-in for ssh that runs the script there with real mkdir
and real sh, and its own clock, boot id and kernel log. Nothing here goes near a device."""
import ast
import json
import os
import re
import signal
import socket
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

import devlib
import wslib

TOOLS = Path(__file__).parents[1] / "tools"
SECONDS = 20  # every run is bounded, so a read of a FIFO fails the test instead of hanging the suite
DAY_ON_THE_DEVICE = 1_000_000_000  # far from this host's clock, so using the wrong clock shows


class FakeDevice:
    def __init__(self, tmp, env):
        self.root = tmp / "device"
        self.store = self.root / "data" / "dev"
        self.bin = self.root / "bin"
        self.bin.mkdir(parents=True)
        self.log = self.root / "commands.log"
        self.env = env
        for name, text in {"clock": str(DAY_ON_THE_DEVICE), "boot_id": "boot-1", "hangchecks": "0", "renderer": "gles2",
                           "mountinfo": "20 1 0:20 / / rw - ext4 /dev/root rw\n", "settings": "@a=1\n@b=2\n", "seed": "@a=1\n@b=2\n",
                           "commit": "abc123", "kit_id": "kit-3"}.items():
            (self.root / name).write_text(text + ("" if text.endswith("\n") else "\n"))
        self._script(self.bin / "date", f'cat "{self.root}/clock"')
        self._script(self.root / "fake-ssh", f"""root="{self.root}"
script=$(cat)
printf '%s\\n----\\n' "$script" >> "$root/commands.log"
[ -e "$root/unreachable" ] && exit 255
cd "$root"
export PATH="$root/bin:$PATH"
printf '%s\\n' "$script" | sh -s""")
        self.config()
        env["GLASSINE_DEV_SSH"] = str(self.root / "fake-ssh")
        env["GLASSINE_DEV_STORE"] = str(self.store)

    def _script(self, path, body):
        path.write_text(f"#!/bin/sh\n{body}\n")
        path.chmod(0o755)

    def config(self, **more):
        r = self.root
        values = {"renderer_expected": "gles2", "fallback_flag": str(r / "fallback"), "settings": str(r / "settings"),
                  "seed": str(r / "seed"), "binds_watch": ["/usr", "/etc"], "release_tries": 1, "release_gap_seconds": 0,
                  "compositor": "nosuchprocess", **more}
        lines = [f"{k} = {json.dumps(v)}" for k, v in values.items()] + ["[probes]"]
        probes = {"boot_id": f"cat {r}/boot_id", "hangchecks": f"cat {r}/hangchecks", "binds": f"cat {r}/mountinfo", "renderer": f"cat {r}/renderer",
                  "kit_id": f"cat {r}/kit_id", "base": "echo base-7", "deployment": "echo dep-7", "boot_image": "echo boot-img", "bases": "echo base-6 base-7",
                  "compositor_commit": f"cat {r}/commit", "battery_temp": "echo 300", "charging": "echo Discharging",
                  "gpu_min_clock": "echo 430000000", "open_apps": "echo foot"}
        lines += [f"{k} = {json.dumps(v)}" for k, v in probes.items()]
        (Path(self.env["GLASSINE_WORKSPACE"]) / "dev.toml").write_text("\n".join(lines) + "\n")

    def set(self, name, text):
        (self.root / name).write_text(text + "\n")

    def tick(self, seconds):
        self.set("clock", str(int((self.root / "clock").read_text()) + seconds))

    @property
    def lock(self):
        return self.store / "lock.d"

    def field(self, name):
        return (self.lock / name).read_text().strip()

    def queue(self):
        return sorted(p.name for p in (self.store / "queue.d").iterdir())

    def override(self, name, **fields):
        path = self.store / "overrides.d"
        path.mkdir(parents=True, exist_ok=True)
        (path / name).write_text("".join(f"{k}: {v}\n" for k, v in fields.items()))


class Rig:
    def __init__(self, tmp_path):
        home = tmp_path / "GlassineOS"
        home.mkdir()
        self.tmp = tmp_path
        self.env = {**os.environ, "GLASSINE_WORKSPACE": str(home), "GLASSINE_HUB_SOCKET": str(tmp_path / "no-hub.sock"),
                    "GLASSINE_HUB_SECONDS": "0.5", "GLASSINE_HUB_MCP_TOKEN": "tool-token"}
        self.device = FakeDevice(tmp_path, self.env)

    def dev(self, *args, code=0):
        done = subprocess.run([sys.executable, str(TOOLS / "dev"), *map(str, args)], capture_output=True, text=True,
                              env=self.env, cwd=self.tmp, timeout=SECONDS)
        assert done.returncode == code, done.stdout + done.stderr
        return done

    def take(self, name="a", *more, code=0, cmd=()):
        tail = ["--", *cmd] if cmd else []
        return self.dev("take", "--as", name, "--purpose", "testing", "--expect", "30m", *more, *tail, code=code)


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path)


class FakeHub:
    """hubd's socket: a line of JSON in, a line out, as the real one does."""

    def __init__(self, path, answers):
        self.answers = answers
        self.seen = []
        self.server = socket.socket(socket.AF_UNIX)
        self.server.bind(str(path))
        self.server.listen()
        threading.Thread(target=self.serve, daemon=True).start()

    def serve(self):
        while True:
            try:
                conn, _ = self.server.accept()
            except OSError:
                return
            with conn:
                request = json.loads(conn.makefile().readline())
                self.seen.append(request)
                reply = self.answers.get(request["op"], {"ok": False, "error": "unknown op"})
                if reply != "silent":
                    conn.sendall(json.dumps(reply).encode() + b"\n")
                else:
                    time.sleep(2)

    def reports(self):
        return [r["report"] for r in self.seen if r["op"] == "device.report"]


@pytest.fixture
def hub(rig):
    def start(**answers):
        answers = {"device.report": {"ok": True}, "decision.holding": {"ok": True, "holding": False}, **answers}
        path = rig.tmp / "hub.sock"
        rig.env["GLASSINE_HUB_SOCKET"] = str(path)
        return FakeHub(path, {k.replace("_", "."): v for k, v in answers.items()})
    return start


# --- taking and releasing

def test_take_is_exit_status_only_and_the_record_is_plain_files(rig):
    done = rig.take("a", "--go-said", "go on", "--does", "reboot,suspend")
    assert done.stdout == ""
    d = rig.device
    assert (d.field("owner"), d.field("purpose"), d.field("does")) == ("a", "testing", "reboot,suspend")
    assert int(d.field("since")) == DAY_ON_THE_DEVICE and int(d.field("expected_end")) == DAY_ON_THE_DEVICE + 1800
    assert "go on" in d.field("go") and "reboot,suspend" in d.field("go")


def test_held_by_another_refuses_and_doesnt_run_the_command(rig):
    rig.take("a")
    flag = rig.tmp / "ran"
    done = rig.take("b", code=1, cmd=["touch", flag])
    assert "Held by a: testing" in done.stderr
    assert not flag.exists() and rig.device.field("owner") == "a"


def test_only_one_of_many_takes_wins_mkdir(rig):
    runs = [subprocess.Popen([sys.executable, str(TOOLS / "dev"), "take", "--as", f"s{i}", "--purpose", "race", "--expect", "5m"],
                             env=rig.env, cwd=rig.tmp, stderr=subprocess.DEVNULL) for i in range(8)]
    codes = [p.wait(SECONDS) for p in runs]
    assert codes.count(0) == 1
    assert rig.device.field("owner") == f"s{codes.index(0)}"


def test_command_is_chained_and_the_lock_released_when_it_ends(rig):
    seen = rig.tmp / "seen"
    done = rig.take("a", cmd=["sh", "-c", f'echo out; cat "{rig.device.lock}/owner" > "{seen}"; exit 7'], code=7)
    assert done.stdout == "out\n" and seen.read_text() == "a\n"
    assert not rig.device.lock.exists()


def test_keep_holds_past_the_command_until_release(rig):
    rig.take("a", "--keep", cmd=["true"])
    assert rig.device.field("owner") == "a" and not (rig.device.lock / "running").exists()
    rig.dev("release", "--as", "a")
    assert not rig.device.lock.exists()


def test_without_a_command_later_shells_release_by_name(rig):
    rig.take("a")
    assert "Only the holder" in rig.dev("release", "--as", "b", code=1).stderr
    assert rig.device.lock.exists()
    rig.dev("release", "--as", "a")
    assert not rig.device.lock.exists()
    assert list((rig.device.store / "history.d").iterdir())


def test_a_signal_during_the_command_still_releases(rig):
    p = subprocess.Popen([sys.executable, str(TOOLS / "dev"), "take", "--as", "a", "--purpose", "p", "--expect", "5m", "--", "sleep", "30"],
                         env=rig.env, cwd=rig.tmp)
    deadline = time.time() + SECONDS
    while not (rig.device.lock / "check").exists() and time.time() < deadline:
        time.sleep(0.05)
    time.sleep(0.3)
    p.send_signal(signal.SIGTERM)
    assert p.wait(SECONDS) == 128 + signal.SIGTERM
    assert not rig.device.lock.exists()


def test_a_device_gone_after_the_command_leaves_the_lock_and_says_so(rig):
    done = rig.take("a", cmd=["touch", rig.device.root / "unreachable"], code=1)
    assert "dev release --as a" in done.stderr
    assert rig.device.lock.exists()  # it survives a reboot: nothing but the holder may free it


def test_a_reboot_in_the_command_gets_a_while_before_the_release(rig):
    rig.device.config(release_tries=4, release_gap_seconds=1)
    gone = rig.device.root / "unreachable"
    rig.take("a", cmd=["sh", "-c", f'touch "{gone}"; (sleep 1; rm "{gone}") >/dev/null 2>&1 &'])
    assert not rig.device.lock.exists()


def test_by_hand_mkdir_blocks_and_status_says_owner_not_written(rig):
    rig.device.lock.mkdir(parents=True)
    assert "owner not written yet" in rig.take("a", code=1).stderr
    assert "owner not written yet" in rig.dev("status").stdout
    (rig.device.lock / "owner").write_text("hand\n")
    assert "Held by hand" in rig.take("a", code=1).stderr
    rig.dev("release", "--as", "hand")  # a lock made by hand, with no store folders around it
    rig.take("a")


def test_bad_names_and_durations_are_refused_before_anything_happens(rig):
    for args in (("take", "--as", "../x", "--purpose", "p", "--expect", "5m"), ("take", "--as", "a", "--purpose", "p", "--expect", "soon")):
        rig.dev(*args, code=1)
    assert not rig.device.store.exists()


# --- joins: the spec's three, and what follows from them

def test_a_loop_under_a_take_keeps_the_lock_until_it_has_restored(rig):
    d = rig.device
    inner = f'"{sys.executable}" "{TOOLS}/dev" take --as a --purpose loop --does bind --go-said words -- true'
    after = f'cat "{d.lock}/owner" > "{rig.tmp}/after-join"'
    rig.take("a", cmd=["sh", "-c", f"{inner} && {after}"])
    assert (rig.tmp / "after-join").read_text() == "a\n"  # the join's end freed nothing
    assert not d.lock.exists()  # the take that made it did


def test_a_joins_failed_command_doesnt_give_the_lock_back(rig):
    rig.take("a")
    made = rig.device.field("made")
    rig.dev("take", "--as", "a", "--purpose", "step", "--", "false", code=1)
    assert rig.device.field("made") == made


def test_later_commands_under_a_held_lock_stay_under_it(rig):
    rig.take("a")
    made = rig.device.field("made")
    for _ in range(3):
        rig.take("a", cmd=["true"])
        assert rig.device.field("made") == made
    rig.dev("take", "--as", "a", "--purpose", "no expect, it's a join")
    rig.dev("release", "--as", "a")
    assert not rig.device.lock.exists()


def test_a_hang_tests_own_hangcheck_doesnt_free_the_lock_mid_run(rig):
    rig.take("a")
    made = rig.device.field("made")
    rig.device.set("hangchecks", "1")
    done = rig.take("a", cmd=["touch", rig.tmp / "ran"], code=1)
    assert "the lock stays with you" in done.stderr and "hangchecks" in done.stderr
    assert not (rig.tmp / "ran").exists()
    assert rig.device.field("made") == made
    rig.take("a", "--dirty", "my own hang test", cmd=["true"])
    assert rig.device.field("dirty") == "my own hang test" and rig.device.field("made") == made


def test_a_release_inside_the_command_is_refused(rig):
    release = f'"{sys.executable}" "{TOOLS}/dev" release --as a'
    rig.take("a", cmd=["sh", "-c", f'! {release} && [ -d "{rig.device.lock}" ]'])
    assert not rig.device.lock.exists()


def test_a_lock_broken_and_retaken_under_a_command_is_left_alone(rig):
    dev = f'"{sys.executable}" "{TOOLS}/dev"'
    rig.take("a", cmd=["sh", "-c", f'{dev} break --why gone && {dev} take --as a --purpose new --expect 5m'])
    assert rig.device.field("purpose") == "new"  # same name, another take: the first one's end doesn't free it


# --- the device check

def refused(rig, words):
    done = rig.take("a", cmd=["touch", rig.tmp / "ran"], code=1)
    assert words in done.stderr
    assert not (rig.tmp / "ran").exists()
    assert not rig.device.lock.exists(), "a refusal gives the lock back"


def test_a_clean_device_passes(rig):
    rig.take("a", cmd=["true"])
    assert not rig.device.lock.exists()


def test_renderer_not_as_expected_refuses(rig):
    rig.device.set("renderer", "pixman")
    refused(rig, "renderer: fail")


def test_fallback_flag_refuses(rig):
    rig.device.set("fallback", "")
    refused(rig, "fallback: fail")


def test_hangchecks_since_the_last_release_refuse_and_dirty_moves_on(rig):
    rig.device.set("hangchecks", "2")
    refused(rig, "2 since the last release")
    rig.take("a", "--dirty", "a dead session's hang", cmd=["true"])
    rig.take("a", cmd=["true"])


def test_hangchecks_in_a_hold_stop_the_next_take_unless_a_state_was_named(rig):
    rig.take("a")
    rig.device.set("hangchecks", "1")
    assert "next take stops" in rig.dev("release", "--as", "a").stderr
    refused(rig, "hangchecks: fail")
    assert "hangchecks" in rig.dev("status").stdout  # the release recorded it

    rig.take("a", "--dirty", "seen")
    rig.device.set("hangchecks", "2")
    rig.device.set("renderer", "pixman")
    rig.dev("release", "--as", "a", "--left", "cpu-renderer", "a hang run")
    rig.take("b", cmd=["true"])


def test_a_reboot_starts_the_hangcheck_count_again(rig):
    rig.device.set("hangchecks", "5")
    rig.take("a", "--dirty", "old", cmd=["true"])
    rig.device.set("boot_id", "boot-2")
    rig.device.set("hangchecks", "0")
    rig.take("a", cmd=["true"])
    rig.device.set("hangchecks", "1")
    refused(rig, "1 since")


def test_left_state_passes_stays_until_the_device_leaves_it_and_stamp_shows_it(rig):
    d = rig.device
    rig.take("a")
    d.set("renderer", "pixman")
    rig.dev("release", "--as", "a", "--left", "cpu-renderer", "after a hang run")
    rig.take("b", cmd=["true"])
    rig.take("b", cmd=["true"])
    assert "after a hang run" in rig.dev("stamp").stdout
    d.set("renderer", "gles2")
    rig.take("b", cmd=["true"])
    assert "after a hang run" not in rig.dev("stamp").stdout
    d.set("renderer", "pixman")
    refused(rig, "renderer: fail")


def test_left_only_takes_the_fixed_names(rig):
    rig.take("a")
    assert "cpu-renderer" in rig.dev("release", "--as", "a", "--left", "odd", "why", code=1).stderr
    assert rig.device.lock.exists()


MOUNTS = "20 1 0:20 / / rw - ext4 /dev/root rw\n31 20 0:21 /data/x/sway /usr/bin/sway rw - ext4 /dev/data rw\n"


def test_a_bind_with_no_record_refuses_and_a_recorded_one_passes(rig):
    d = rig.device
    d.set("mountinfo", MOUNTS)
    refused(rig, "no override record: /usr/bin/sway")
    d.override("sway", owner="a", boot_id="boot-1", target="/usr/bin/sway", value="branch build", remove="umount")
    rig.take("a", cmd=["true"])
    assert (d.store / "overrides.d" / "sway").exists()


def test_a_reboot_clears_dead_records_and_keeps_what_survives(rig):
    d = rig.device
    d.override("old", owner="a", boot_id="boot-0", target="/usr/bin/sway")
    d.override("kept", owner="a", boot_id="boot-0", target="/etc/x", survives_reboot="yes")
    d.set("mountinfo", MOUNTS)
    refused(rig, "no override record: /usr/bin/sway")  # a dead record vouches for nothing
    rig.take("a", "--dirty", "seen")
    assert not (d.store / "overrides.d" / "old").exists() and (d.store / "overrides.d" / "kept").exists()


def test_mountinfo_finds_binds_only_over_watched_folders():
    text = ("1 0 0:1 /deploy/7 / rw - btrfs /dev/x rw\n2 1 0:2 /media/root /root rw - ext4 /dev/y rw\n"
            "3 1 0:2 /a\\040b /usr/bin/my\\040tool rw - ext4 /dev/y rw\n4 1 0:3 / /usr/share rw - tmpfs t rw\n")
    assert devlib.mountinfo_binds(text, ["/usr", "/etc"]) == ["/usr/bin/my tool"]


def test_a_setting_off_the_seed_refuses_until_recorded(rig):
    d = rig.device
    d.set("settings", "@a=1\n@b=3")
    refused(rig, "differ from the seed, no override record: @b")
    d.override("tune", owner="a", boot_id="boot-1", setting="@b", value="3")
    done = rig.take("a", cmd=["true"])
    assert "tune=3: recorded, unconfirmed" in done.stderr


def test_what_cant_be_checked_says_so_and_a_probe_that_fails_stops(rig):
    d = rig.device
    d.config(seed="", fallback_flag="", renderer_expected="")
    done = rig.take("a", cmd=["true"])
    assert all(f"{c}: unknown" in done.stderr for c in ("renderer", "fallback", "settings"))
    d.config()
    (d.root / "renderer").unlink()
    refused(rig, "couldn't read it")


def test_dirty_runs_anyway_and_stamp_prints_why(rig):
    rig.device.set("renderer", "pixman")
    rig.take("a", "--dirty", "known, testing the fallback")
    assert rig.device.field("dirty") == "known, testing the fallback"
    assert "known, testing the fallback" in rig.dev("stamp").stdout


# --- never /run

def run_everything(rig):
    rig.dev("wait", "--as", "w", "--on", "words", "--until", "1h")
    rig.dev("ready", "--as", "w")
    rig.dev("wait", "--as", "w", "--drop")
    rig.take("a", cmd=["true"])
    rig.take("a")
    rig.dev("notice", "soon")
    rig.dev("status")
    rig.dev("stamp")
    rig.dev("release", "--as", "a")


def test_no_script_ever_names_run_even_with_a_real_fifo_there(rig):
    fifo = rig.device.root / "run"
    fifo.mkdir()
    os.mkfifo(fifo / "daemon.ctl")
    run_everything(rig)  # each run is bounded: a read of the FIFO would fail on the timeout
    for script in rig.device.log.read_text().split("\n----\n"):
        assert not re.search(r"(^|[\s'\"=:(])(/var)?/run\b", script), script


def test_a_fifo_in_the_store_doesnt_block_a_read(rig):
    d = rig.device
    rig.take("a")
    os.mkfifo(d.store / "queue.d" / "1-fifo")
    os.mkfifo(d.lock / "stray")
    rig.dev("status")
    rig.dev("stamp")
    rig.dev("release", "--as", "a")


@pytest.mark.parametrize("key,value", [("settings", "/run/x"), ("seed", "/var/run/y"), ("fallback_flag", "/run/z"), ("store", "/run/lock")])
def test_config_pointing_at_run_is_refused(rig, key, value):
    rig.device.config(**{key: value})
    if key == "store":
        rig.env.pop("GLASSINE_DEV_STORE")
    assert "never read or glob /run" in rig.dev("status", code=1).stderr
    assert not rig.device.log.exists()


def test_a_probe_pointing_at_run_is_refused(rig):
    path = Path(rig.env["GLASSINE_WORKSPACE"]) / "dev.toml"
    path.write_text(path.read_text() + 'daemon_commit = "cat /run/daemon.ctl"\n')
    assert "never read or glob /run" in rig.dev("stamp", code=1).stderr
    assert not rig.device.log.exists()


def test_the_guard_lets_lookalikes_through():
    assert all(devlib.touches_run(t) for t in ("/run/x", "cat /run/a", "/var/run/a", "ls /run/*", "/run"))
    assert not any(devlib.touches_run(t) for t in ("/usr/bin/run", "/tmp/x/run/y", "$root/run/y", "/running", "/runtime/x"))


def test_the_tools_strings_name_run_only_for_the_hosts_socket_and_the_refusal():
    for path in (TOOLS / "dev", TOOLS / "devlib.py"):
        tree = ast.parse(path.read_text())
        docs = {id(n.body[0].value) for n in ast.walk(tree)
                if isinstance(n, (ast.Module, ast.FunctionDef, ast.ClassDef)) and n.body
                and isinstance(n.body[0], ast.Expr) and isinstance(n.body[0].value, ast.Constant)}
        strings = [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docs]
        named = [t for t in strings if devlib.touches_run(t) and "/run/user/" not in t and "never read or glob /run" not in t and t != devlib._RUN.pattern]
        assert not named, (path, named)


# --- the queue

def test_a_ready_entry_blocks_a_session_with_no_entry_and_a_waiting_one_blocks_nobody(rig):
    rig.dev("wait", "--as", "w", "--on", "the kernel build is done", "--until", "1h", "--for", "a scroll bench")
    assert "waiting on the kernel build is done" in rig.dev("status").stdout
    rig.take("a", cmd=["true"])
    rig.dev("ready", "--as", "w")
    done = rig.take("a", code=1)
    assert "ready ahead of you: w" in done.stderr
    assert not rig.device.lock.exists()
    rig.take("a", "--ahead", "one quick reboot")
    assert rig.device.field("ahead") == "one quick reboot"


def test_earlier_means_the_devices_order(rig):
    rig.dev("wait", "--as", "first", "--on", "x", "--until", "1h")
    rig.device.tick(10)
    rig.dev("wait", "--as", "second", "--on", "x", "--until", "1h")
    rig.dev("ready", "--as", "first")
    rig.dev("ready", "--as", "second")
    assert "ready ahead of you: first" in rig.take("second", code=1).stderr
    assert "ready ahead of you: first, second" in rig.take("third", code=1).stderr
    rig.take("first", cmd=["true"])  # second is ready, but later
    rig.take("second", cmd=["true"])
    assert rig.device.queue() == []


def test_the_queue_file_is_named_and_filled_by_the_devices_clock(rig):
    rig.dev("wait", "--as", "w", "--on", "words", "--until", "1h")
    entry = rig.device.store / "queue.d" / f"{DAY_ON_THE_DEVICE}-w"
    fields = devlib.parse_fields(entry.read_text())
    assert (fields["who"], fields["on"], fields["until"]) == ("w", "words", str(DAY_ON_THE_DEVICE + 3600))


def test_an_entry_leaves_at_its_owners_take(rig):
    rig.dev("wait", "--as", "a", "--on", "x", "--until", "1h")
    rig.dev("ready", "--as", "a")
    rig.take("a", cmd=["true"])
    assert rig.device.queue() == []


def test_an_entry_stays_when_its_owners_take_is_refused(rig):
    rig.dev("wait", "--as", "a", "--on", "x", "--until", "1h")
    rig.device.set("renderer", "pixman")
    rig.take("a", code=1)
    assert len(rig.device.queue()) == 1


def test_an_entry_leaves_at_drop(rig):
    rig.dev("wait", "--as", "a", "--on", "x", "--until", "1h")
    rig.dev("wait", "--as", "a", "--drop")
    assert rig.device.queue() == []
    rig.dev("wait", "--as", "a", "--drop", code=1)


def test_a_dead_waiter_stops_blocking_at_until_on_the_devices_clock(rig):
    rig.dev("wait", "--as", "w", "--on", "x", "--until", "1h")
    rig.dev("ready", "--as", "w")
    rig.take("a", code=1)
    rig.device.tick(3601)
    assert "w:" not in rig.dev("status").stdout
    rig.dev("ready", "--as", "w", code=1)
    rig.take("a", cmd=["true"])
    assert rig.device.queue() == []


def test_until_is_kept_to_a_few_hours_and_must_be_ahead(rig):
    rig.dev("wait", "--as", "w", "--on", "x", "--until", "5h", code=1)
    rig.dev("wait", "--as", "w", "--on", "x", "--until", "0s", code=1)
    rig.dev("wait", "--as", "w", "--on", "x", "--until", wslib.utc(DAY_ON_THE_DEVICE + 7200))
    assert devlib.parse_fields((rig.device.store / "queue.d" / f"{DAY_ON_THE_DEVICE}-w").read_text())["until"] == str(DAY_ON_THE_DEVICE + 7200)


def test_waiting_again_keeps_your_place(rig):
    rig.dev("wait", "--as", "w", "--on", "x", "--until", "1h")
    rig.dev("ready", "--as", "w")
    rig.device.tick(60)
    rig.dev("wait", "--as", "w", "--on", "y", "--until", "1h")
    [name] = rig.device.queue()
    assert name == f"{DAY_ON_THE_DEVICE}-w"
    assert "waiting on y" in rig.dev("status").stdout


def test_by_hand_an_entry_and_a_bare_ready_line_count(rig):
    q = rig.device.store / "queue.d"
    q.mkdir(parents=True)
    (q / f"{DAY_ON_THE_DEVICE}-hand").write_text(f"who: hand\non: x\nuntil: {DAY_ON_THE_DEVICE + 60}\nready\n")
    assert "ready ahead of you: hand" in rig.take("a", code=1).stderr


def test_notice_replaces_the_message(rig):
    rig.dev("notice", "nothing held", code=1)
    rig.take("a")
    rig.dev("notice", "rebooting\nsoon")
    rig.dev("notice", "rebooting now")
    assert rig.device.field("notice") == "rebooting now"
    assert "notice: rebooting now" in rig.dev("status").stdout
    rig.dev("notice", "--as", "b", "not yours", code=1)


# --- the take table, up and down

HUB_YES = {"ok": True, "answer": "yes", "words": "Go 7: reboot, until 15:00"}
HUB_NO = {"ok": True, "answer": "no", "words": "Go 7 doesn't cover suspend"}


def test_no_does_runs_with_no_question(rig, hub):
    h = hub()
    rig.take("a", cmd=["true"])
    assert not [r for r in h.seen if r["op"].startswith("decision")]


def test_no_does_runs_with_the_hub_down(rig):
    rig.take("a", cmd=["true"])


@pytest.mark.parametrize("up", [True, False])
def test_does_alone_is_refused_up_or_down(rig, hub, up):
    h = hub() if up else None
    assert "name a go" in rig.take("a", "--does", "reboot", code=1).stderr
    assert not rig.device.lock.exists()
    assert not (h and h.seen)


def test_a_go_the_hub_says_yes_to_runs_and_is_asked_with_the_token(rig, hub):
    h = hub(decision_use=HUB_YES)
    rig.take("a", "--does", "reboot", "--go", "7", cmd=["true"])
    [ask] = [r for r in h.seen if r["op"] == "decision.use"]  # a take that gets through spends a run
    assert not [r for r in h.seen if r["op"] == "decision.check"]
    assert (ask["id"], ask["kind"], ask["actions"], ask["agent_token"]) == ("7", "go", ["reboot"], "tool-token")


def test_a_refused_take_spends_nothing(rig, hub):
    h = hub(decision_use=HUB_YES, decision_holding={"ok": True, "holding": True})
    rig.take("a", "--does", "reboot", "--go", "7", code=1)
    assert not [r for r in h.seen if r["op"] == "decision.use"] and not rig.device.lock.exists()


def test_a_go_the_hub_says_no_to_gives_the_lock_back(rig, hub):
    hub(decision_use=HUB_NO)
    assert "doesn't cover suspend" in rig.take("a", "--does", "suspend", "--go", "7", code=1).stderr
    assert not rig.device.lock.exists()


def test_a_go_with_the_hub_down_answers_unknown(rig):
    done = rig.take("a", "--does", "reboot", "--go", "7", code=1)
    assert "unknown" in done.stderr
    assert not rig.device.lock.exists()


def test_a_hub_without_decisions_yet_answers_unknown(rig, hub):
    hub()  # no decision.check: this is today's hub
    assert "unknown" in rig.take("a", "--does", "reboot", "--go", "7", code=1).stderr
    assert not rig.device.lock.exists()


def test_a_hub_past_its_limit_counts_as_down(rig, hub):
    hub(decision_check="silent")
    assert "unknown" in rig.take("a", "--does", "reboot", "--go", "7", code=1).stderr


def test_holding_the_device_refuses_a_go_yes(rig, hub):
    hub(decision_check=HUB_YES, decision_holding={"ok": True, "holding": True})
    assert "has the device" in rig.take("a", "--does", "reboot", "--go", "7", code=1).stderr
    assert not rig.device.lock.exists()


def test_holding_the_device_refuses_go_said(rig, hub):
    hub(decision_holding={"ok": True, "holding": True})
    assert "has the device" in rig.take("a", "--does", "reboot", "--go-said", "reboot it", code=1).stderr


def test_holding_the_device_doesnt_refuse_an_eye_test(rig, hub):
    hub(decision_use=HUB_YES, decision_holding={"ok": True, "holding": True})
    rig.take("a", "--does", "eye-test", "--go", "7", cmd=["true"])
    assert "has the device" in rig.take("a", "--does", "eye-test,reboot", "--go", "7", code=1).stderr


def test_go_said_runs_with_the_hub_up(rig, hub):
    hub()
    rig.take("a", "--does", "reboot", "--go-said", "go ahead")
    assert "go ahead" in rig.device.field("go") and "reboot" in rig.device.field("go")


def test_go_said_runs_with_the_hub_down_with_words_and_actions_in_the_record(rig):
    rig.take("a", "--does", "reboot", "--go-said", "go ahead")
    assert "go ahead" in rig.device.field("go") and "reboot" in rig.device.field("go")


def test_the_hub_is_asked_after_the_device_check_so_a_dirty_device_spends_no_go(rig, hub):
    h = hub(decision_check=HUB_YES)
    rig.device.set("renderer", "pixman")
    rig.take("a", "--does", "reboot", "--go", "7", code=1)
    assert not [r for r in h.seen if r["op"].startswith("decision")]


def test_a_join_with_new_actions_goes_through_the_table_keeping_the_lock(rig, hub):
    hub(decision_check=HUB_NO)
    rig.take("a", "--does", "reboot", "--go-said", "reboot it")
    made = rig.device.field("made")
    assert "name a go" in rig.dev("take", "--as", "a", "--purpose", "more", "--does", "suspend", code=1).stderr
    assert "doesn't cover" in rig.dev("take", "--as", "a", "--purpose", "more", "--does", "suspend", "--go", "7", code=1).stderr
    assert rig.device.field("made") == made and rig.device.field("does") == "reboot"
    rig.dev("take", "--as", "a", "--purpose", "more", "--does", "suspend", "--go-said", "suspend too")
    assert rig.device.field("does") == "reboot,suspend" and "suspend too" in rig.device.field("go")
    assert rig.device.field("made") == made


# --- reports

def test_a_report_goes_before_and_after_the_command_and_is_the_whole_state(rig, hub):
    h = hub()
    rig.take("a", cmd=["true"])
    held, free = h.reports()
    assert held["lock"]["owner"] == "a" and free["lock"] is None
    assert (held["device"], held["base"], held["deployment"], held["boot_image"], held["renderer"]) == ("note-air-1", "base-7", "dep-7", "boot-img", "gles2")
    assert held["bases"] == ["base-6", "base-7"] and held["free_kb"] > 0 and held["overrides"] == {} and held["queue"] == []
    assert held["device_time"] == DAY_ON_THE_DEVICE
    assert "made" not in held["lock"]


def test_every_state_change_reports(rig, hub):
    h = hub()
    for args in (("wait", "--as", "w", "--on", "x", "--until", "1h"), ("ready", "--as", "w"), ("wait", "--as", "w", "--drop")):
        rig.dev(*args)
    rig.take("a")
    rig.dev("notice", "soon")
    rig.dev("release", "--as", "a")
    rig.take("b")
    rig.dev("break", "--why", "dead, the person said so")
    rig.dev("status")
    assert len(h.reports()) == 9


@pytest.mark.parametrize("answers", [{"device_report": "silent"}, {"device_report": {"ok": False, "error": "no"}}])
def test_a_failed_report_never_fails_the_command(rig, hub, answers):
    hub(**answers)
    rig.take("a", cmd=["true"])
    rig.take("a")
    rig.dev("release", "--as", "a")


def test_a_device_gone_before_the_report_doesnt_fail_the_command(rig, hub):
    hub()
    rig.take("a")
    done = rig.dev("take", "--as", "a", "--purpose", "step", "--", "touch", rig.device.root / "unreachable")
    assert "no report sent" in done.stderr


def test_no_hub_at_all_fails_nothing_and_costs_no_device_trip(rig):
    rig.take("a")
    before = rig.device.log.read_text().count("----")
    rig.dev("notice", "soon")
    assert rig.device.log.read_text().count("----") == before + 2  # the read and the write


def test_garbage_from_the_hub_is_no_answer(rig, tmp_path):
    path = tmp_path / "bad.sock"
    rig.env["GLASSINE_HUB_SOCKET"] = str(path)
    server = socket.socket(socket.AF_UNIX)
    server.bind(str(path))
    server.listen()

    def serve():
        while True:
            conn, _ = server.accept()
            with conn:
                conn.recv(4096)
                conn.sendall(b"not json\n")
    threading.Thread(target=serve, daemon=True).start()
    assert "unknown" in rig.take("a", "--does", "reboot", "--go", "7", code=1).stderr
    rig.take("a", cmd=["true"])


# --- stamp, status, break

def test_stamp_has_the_fields_and_says_what_it_doesnt_know(rig):
    d = rig.device
    d.override("tune", owner="a", boot_id="boot-1", setting="@x", value="9")
    d.set("settings", "@a=1\n@b=2\n@x=9")
    out = json.loads(rig.dev("stamp", "--json").stdout)
    assert out["kit_id"] == "kit-3" and out["base"] == "base-7" and out["boot_image"] == "boot-img" and out["renderer"] == "gles2"
    assert out["compositor_commit"] == "abc123" and out["fallback_flag"] == "clear" and out["charging"] == "Discharging"
    assert out["open_apps"] == "foot" and out["gpu_min_clock"] == "430000000" and out["battery_temp"] == "300"
    assert out["live_settings"] == {"tune": "9 (recorded, unconfirmed)"}
    assert out["compositor_sha256"] == "unknown (probe failed)"
    assert out["daemon_sha256"] == "unknown (no probe set)" and out["dirty"] is None


def test_stamp_hashes_the_running_binary(rig):
    (rig.device.bin / "pidof").write_text("#!/bin/sh\necho self\n")
    (rig.device.bin / "pidof").chmod(stat.S_IRWXU)
    rig.device.config(compositor="anything")
    assert len(json.loads(rig.dev("stamp", "--json").stdout)["compositor_sha256"]) == 64


def test_status_reads_only_the_device_in_words_with_overdue(rig):
    d = rig.device
    assert "Free right now" in rig.dev("status").stdout
    assert "Only mkdir on the device decides" in rig.dev("status").stdout
    rig.take("a")
    d.tick(1860)
    out = rig.dev("status").stdout
    assert "Held by a: testing." in out and "overdue by 1 min" in out
    assert json.loads(rig.dev("status", "--json").stdout)["lock"]["owner"] == "a"


def test_status_says_when_the_device_cant_be_reached(rig):
    rig.device.set("unreachable", "")
    assert "can't reach the device" in rig.dev("status", code=1).stderr


def test_break_keeps_the_old_record_and_frees_the_lock(rig):
    d = rig.device
    rig.dev("break", "--why", "nothing held", code=1)
    rig.take("a")
    rig.dev("break", "--why", "dead, the person said so", "--as", "b")
    assert not d.lock.exists()
    [old] = [p for p in (d.store / "history.d").iterdir() if "broken" in p.name]
    assert (old / "owner").read_text() == "a\n"
    assert "dead, the person said so" in (old / "broken").read_text()
    rig.take("b", cmd=["true"])


def test_a_session_that_died_mid_test_cant_leave_a_bad_device_for_the_next_run(rig):
    rig.take("dead")
    rig.device.set("renderer", "pixman")
    rig.device.set("hangchecks", "1")
    rig.dev("break", "--why", "dead, the person said so")
    refused(rig, "renderer: fail")


def test_free_text_is_one_capped_line(rig):
    rig.dev("take", "--as", "a", "--purpose", "x\ny" + "z" * 500, "--expect", "5m")
    purpose = rig.device.field("purpose")
    assert "\n" not in purpose and len(purpose) == devlib.TEXT_CAP


def test_awkward_text_stays_data_on_the_device(rig):
    nasty = """$(touch pwned) `touch pwned` ';touch pwned;' "x" """
    rig.dev("take", "--as", "a", "--purpose", nasty, "--expect", "5m", "--dirty", nasty)
    rig.dev("notice", nasty)
    rig.dev("wait", "--as", "w", "--on", nasty, "--until", "1h")
    assert not list(rig.device.root.rglob("pwned"))
    assert rig.device.field("purpose") == " ".join(nasty.split())


def test_the_store_is_configurable(rig):
    other = rig.tmp / "elsewhere"
    rig.env["GLASSINE_DEV_STORE"] = str(other)
    rig.take("a")
    assert (other / "lock.d" / "owner").exists() and not rig.device.store.exists()


def test_history_is_kept_short(rig):
    h = rig.device.store / "history.d"
    h.mkdir(parents=True)
    for i in range(devlib.HISTORY_KEPT + 5):
        (h / f"{i:010d}-old-released-1").mkdir()
    rig.take("a", cmd=["true"])
    names = sorted(p.name for p in h.iterdir())
    assert len(names) == devlib.HISTORY_KEPT and "released" in names[-1] and names[0].startswith("0000000006")
