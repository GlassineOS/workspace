# SPDX-License-Identifier: GPL-2.0-or-later
"""loop against dev's fake device, with stand-ins for mount, umount, pidof and a real process.

Nothing here has met a real device: what's proven is what the commands say and the order they run in.
The binaries and recipes are made up. Only the hazards are real.
"""
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path
from unittest import mock

import pytest

from test_dev import TOOLS
from test_repro import Kit

import devlib
import kitlib
import looplib


class Loop(Kit):
    def __init__(self, tmp_path):
        super().__init__(tmp_path)
        d = self.device
        self.r = d.root
        self.storage = self.r / "storage"
        self.storage.mkdir()
        self.target = self.r / "usr-bin" / "widgetd"
        self.target.parent.mkdir()
        self.target.write_text("the system's own\n")
        self.sources = tmp_path / "sources"
        self.conf = self.r / "widget.conf"
        self.conf.write_text("@a=1\n@b=2\n")
        self.conf.chmod(0o664)
        self.steps = self.r / "steps"
        self.steps.touch()
        self.ctl = self.r / "ctl"
        real_sha = shutil.which("sha256sum")
        r = self.r
        d._script(d.bin / "mount", f"""echo "mount $*" >> {r}/steps
[ -e {r}/mount-fails ] && exit 32
echo "30 1 0:20 /over $3 rw - ext4 /dev/root rw" >> {r}/mountinfo
{real_sha} "$2" | cut -d' ' -f1 > {r}/bound-hash.$(basename "$3")""")
        d._script(d.bin / "umount", f"""echo "umount $*" >> {r}/steps
[ -e {r}/umount-fails ] && exit 32
grep -v " $1 " {r}/mountinfo > {r}/mountinfo.new
cat {r}/mountinfo.new > {r}/mountinfo
rm -f {r}/bound-hash.$(basename "$1")""")
        d._script(d.bin / "pidof", f"""name=$2
[ -f {r}/pid.$name ] || exit 1
pid=$(cat {r}/pid.$name)
[ -d /proc/$pid ] || exit 1
grep -q '^State:.*Z' /proc/$pid/status && exit 1
echo $pid""")
        d._script(d.bin / "sha256sum", f"""case "$1" in
/proc/*/exe) pid=${{1#/proc/}}; pid=${{pid%/exe}}; cat {r}/running-hash.$pid 2>/dev/null || echo original; exit 0;;
esac
exec {real_sha} "$@" """)
        # the recipe's own commands, which say what they were asked to do
        self.tool_script("fake-stop", f"""echo "stop $1" >> {r}/steps
[ -e {r}/wont-stop ] && exit 0
[ -e {r}/stop-fails ] && exit 1
/bin/kill "$(cat {r}/pid.$1)" 2>/dev/null
exit 0""")
        self.tool_script("fake-start", f"""echo "start $1" >> {r}/steps
env > {r}/start-env.$1
[ -e {r}/wont-start ] && exit 0
sleep 300 >/dev/null 2>&1 </dev/null &
cat {r}/bound-hash.$1 > {r}/running-hash.$! 2>/dev/null || echo original > {r}/running-hash.$!
echo $! > {r}/pid.$1""")
        self.tool_script("fake-reexec", f"""echo "reexec $1" >> {r}/steps
cat {r}/bound-hash.$1 > {r}/running-hash.$(cat {r}/pid.$1)
exit 0""")
        self.running = []

    def tool_script(self, name, body):
        self.device._script(self.device.bin / name, body)

    def start_process(self, name="widgetd"):
        env = {"PATH": f"{self.device.bin}:{os.environ['PATH']}", "SESSION_VAR": "from the session", "SESSION_SPACED": "two words"}
        child = subprocess.Popen(["sleep", "300"], env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.running.append(child)
        (self.r / f"pid.{name}").write_text(f"{child.pid}\n")
        return child

    def stop_everything(self):
        for child in self.running:
            child.kill()
            child.wait()
        for pid in self.r.glob("pid.*"):
            try:
                os.kill(int(pid.read_text()), signal.SIGKILL)
            except (ProcessLookupError, ValueError):
                pass

    def recipe(self, name="widgetd", binary=None, role="daemon", top="", restart="", more="", content="a new build\n", repo=None):
        binary = binary or name
        repo = repo or name
        folder = self.kit / "loop" / name
        folder.mkdir(parents=True, exist_ok=True)
        source = self.home / repo
        source.mkdir(parents=True, exist_ok=True)
        (source / "built").write_text(content)
        r = self.r
        target = self.target if name == "widgetd" else r / "usr-bin" / binary
        target.touch()
        if role == "compositor" and "closes_windows" not in top:
            top += "\ncloses_windows = true"
        (folder / "loop.toml").write_text(f"""\
what = "a made-up binary"
binary = "{binary}"
role = "{role}"
target = "{target}"
storage = "{self.storage}"
{top}
[build]
repo = "{repo}"
command = ["sh", "-c", "cp built out; echo built >> {r}/built-log"]
output = "out"
[restart]
stop = "{r}/bin/fake-stop {binary}"
start = "{r}/bin/fake-start {binary}"
stop_seconds = 1
start_seconds = 2
{restart}
{more}
""")
        self.start_process(binary) if not (r / f"pid.{binary}").exists() else None
        return folder

    def loop(self, *args, code=0, seconds=60):
        return self.tool("loop", *args, code=code, seconds=seconds)

    def log(self):
        return self.steps.read_text().splitlines()

    def overrides(self):
        folder = self.device.store / "overrides.d"
        return {p.name: p.read_text() for p in folder.iterdir()} if folder.is_dir() else {}

    def scripts(self):
        return self.device.log.read_text()

    def bound(self):
        return f" {self.target} " in (self.r / "mountinfo").read_text()

    def recipe_obj(self, name="widgetd"):
        with mock.patch.dict(os.environ, GLASSINE_KIT=str(self.kit)):
            return looplib.load(name)


LIVE = """[live]
reapply = 'printf "%s=%s\\\\n" "$name" "$value" >> {r}/ctl; echo "reapply $name" >> {r}/steps'
"""
CONFIG = "[config]\npath = \"{conf}\"\n"


@pytest.fixture
def lp(tmp_path):
    rig = Loop(tmp_path)
    yield rig
    rig.stop_everything()


def live(lp):
    return LIVE.format(r=lp.r)


def conf(lp):
    return CONFIG.format(conf=lp.conf)


# --- the cycle


def test_a_run_builds_copies_binds_records_tests_and_restores(lp):
    lp.recipe()
    lp.take("a")
    seen = lp.tmp / "seen"
    check = f'cat "{lp.device.store}/overrides.d/loop-widgetd" > "{seen}"; cat "{lp.storage}/loop-widgetd/binary" >> "{seen}"; cat "{lp.r}/running-hash.$(cat {lp.r}/pid.widgetd)" >> "{seen}"'
    lp.loop("run", "widgetd", "--as", "a", "--", "sh", "-c", check)
    text = seen.read_text()
    sha = kitlib.artstore.digest(lp.home / "widgetd" / "built")
    assert f"target: {lp.target}" in text and "owner: a" in text and "boot_id: boot-1" in text and f"value: {sha[:12]}" in text
    assert f"remove: sh {lp.storage}/loop-widgetd/restore.sh" in text
    assert "a new build" in text and sha in text  # the copy was there, and the hash of what ran was the copy's
    assert (lp.r / "built-log").read_text() == "built\n"
    assert not lp.bound() and not lp.overrides() and not (lp.storage / "loop-widgetd").exists()
    assert [s.split()[0] for s in lp.log()] == ["stop", "mount", "start", "stop", "umount", "start"]


def test_the_test_commands_exit_status_is_the_runs(lp):
    lp.recipe()
    lp.take("a")
    lp.loop("run", "widgetd", "--as", "a", "--", "sh", "-c", "exit 7", code=7)
    assert not lp.bound()  # restored all the same


def test_up_stays_bound_until_restore(lp):
    lp.recipe()
    lp.take("a")
    lp.loop("up", "widgetd", "--as", "a")
    assert lp.bound() and "loop-widgetd" in lp.overrides()
    assert "already up" in lp.loop("up", "widgetd", "--as", "a", code=1).stderr
    lp.loop("restore", "widgetd", "--as", "a")
    assert not lp.bound() and not lp.overrides()


def test_a_recorded_bind_passes_devs_check_and_a_release_with_it_says_so(lp):
    lp.recipe()
    lp.take("a")
    lp.loop("up", "widgetd", "--as", "a")
    lp.take("a", cmd=["true"])
    assert "still yours: loop-widgetd" in lp.dev("release", "--as", "a").stderr


def test_restore_sh_works_by_hand_with_no_tool(lp):
    lp.recipe()
    lp.take("a")
    lp.loop("up", "widgetd", "--as", "a")
    remove = [x for x in lp.overrides()["loop-widgetd"].splitlines() if x.startswith("remove:")][0].split(": ", 1)[1]
    done = subprocess.run([str(lp.r / "fake-ssh")], input=remove + "\n", text=True, capture_output=True, timeout=20)
    assert done.returncode == 0, done.stderr
    assert not lp.bound() and not lp.overrides()


def test_a_failed_build_changes_nothing_on_the_device(lp):
    lp.recipe()
    lp.take("a")
    toml = lp.kit / "loop" / "widgetd" / "loop.toml"
    toml.write_text(toml.read_text().replace('"cp built out; echo built', '"exit 3; echo built'))
    assert "Nothing on the device changed" in lp.loop("up", "widgetd", "--as", "a", code=1).stderr
    assert not lp.log() and not lp.overrides()


def test_a_run_whose_build_fails_doesnt_restart_anything(lp):
    lp.recipe()
    lp.take("a")
    toml = lp.kit / "loop" / "widgetd" / "loop.toml"
    toml.write_text(toml.read_text().replace('"cp built out; echo built', '"exit 3; echo built'))
    lp.loop("run", "widgetd", "--as", "a", "--", "true", code=1)
    assert not lp.log()


def test_a_bind_that_fails_leaves_no_record(lp):
    lp.recipe()
    lp.take("a")
    (lp.r / "mount-fails").touch()
    done = lp.loop("up", "widgetd", "--as", "a", code=1)
    assert not lp.overrides() and not lp.bound()
    assert running(lp)  # it was stopped for the bind, and nothing is bound, so it came back on the original binary
    assert lp.log() == ["stop widgetd", f"mount --bind {lp.storage}/loop-widgetd/binary {lp.target}", "start widgetd"]
    assert "started again on the original" in done.stderr


def test_a_bind_that_fails_and_cant_start_it_again_says_it_is_stopped(lp):
    lp.recipe()
    lp.take("a")
    (lp.r / "mount-fails").touch()
    (lp.r / "wont-start").touch()
    done = lp.loop("up", "widgetd", "--as", "a", code=1)
    assert not running(lp) and not lp.overrides() and not lp.bound()
    assert "widgetd is stopped" in done.stderr and "reboot" in done.stderr.lower()


def test_the_wrong_binary_running_fails_and_run_restores(lp):
    lp.recipe()
    lp.take("a")
    lp.tool_script("fake-start", f"""echo "start $1" >> {lp.r}/steps
sleep 300 >/dev/null 2>&1 </dev/null &
echo not-the-copy > {lp.r}/running-hash.$!
echo $! > {lp.r}/pid.$1""")
    done = lp.loop("run", "widgetd", "--as", "a", "--", "touch", lp.tmp / "tested", code=1)
    assert "isn't running the copied binary" in done.stderr
    assert not (lp.tmp / "tested").exists() and not lp.bound()


def test_a_copy_with_another_hash_binds_nothing(lp):
    lp.recipe()
    lp.take("a")
    lp.tool_script("sha256sum", "echo wronghash")
    assert "another hash" in lp.loop("up", "widgetd", "--as", "a", code=1).stderr
    assert not lp.log() and not lp.overrides()


# --- the lock: it joins a take, and never releases


def test_with_no_take_it_refuses_and_touches_nothing(lp):
    lp.recipe()
    done = lp.loop("up", "widgetd", "--as", "a", code=1)
    assert "dev take first" in done.stderr
    assert not lp.device.lock.exists() and not lp.log() and not (lp.r / "built-log").exists()


def test_it_joins_a_held_take_and_never_releases_it(lp):
    lp.recipe()
    lp.take("a")
    made = lp.device.field("made")
    for args in (("up", "widgetd"), ("restore", "widgetd"), ("run", "widgetd", "--as", "a", "--", "true")):
        lp.loop(*args, *(() if "--as" in args else ("--as", "a")))
        assert lp.device.field("made") == made
    lp.dev("release", "--as", "a")


def test_under_a_take_with_a_command_the_lock_outlives_the_restore(lp):
    lp.recipe()
    after = f'[ "$(cat "{lp.device.lock}/owner")" = a ] && touch "{lp.tmp}/held"'
    loop = f'"{sys.executable}" "{TOOLS}/loop" run widgetd --as a -- true'
    lp.take("a", cmd=["sh", "-c", f"{loop} && {after}"])
    assert (lp.tmp / "held").exists()  # still held once loop had restored
    assert not lp.bound() and not lp.device.lock.exists()  # and the take that made it released it


def test_expect_makes_a_take_that_stays_until_dev_release(lp):
    lp.recipe()
    lp.loop("run", "widgetd", "--as", "a", "--expect", "20m", "--", "true")
    assert lp.device.field("owner") == "a"
    lp.dev("release", "--as", "a")
    assert not lp.device.lock.exists()


def test_another_holder_refuses_and_nothing_happens(lp):
    lp.recipe()
    lp.take("other")
    lp.loop("run", "widgetd", "--as", "a", "--expect", "20m", "--", "touch", lp.tmp / "tested", code=1)
    assert not lp.log() and not (lp.tmp / "tested").exists() and lp.device.field("owner") == "other"


def test_a_dirty_device_stops_up_but_never_a_restore(lp):
    lp.recipe()
    lp.take("a")
    lp.loop("up", "widgetd", "--as", "a")
    made = lp.device.field("made")
    lp.device.set("hangchecks", "3")  # a hang test's own hang
    lp.recipe("gadgetd")
    assert "hangchecks" in lp.loop("up", "gadgetd", "--as", "a", code=1).stderr
    lp.loop("restore", "widgetd", "--as", "a")
    assert not lp.bound() and lp.device.field("made") == made
    assert "restoring" in lp.device.field("dirty") and "hangchecks" in lp.device.field("dirty")


def test_a_hang_during_the_test_still_restores_and_keeps_the_lock(lp):
    lp.recipe()
    lp.take("a")
    made = lp.device.field("made")
    lp.loop("run", "widgetd", "--as", "a", "--", "sh", "-c", f'echo 2 > "{lp.r}/hangchecks"')
    assert not lp.bound() and lp.device.field("made") == made


def test_restore_refuses_without_the_lock(lp):
    lp.recipe()
    lp.take("a")
    lp.loop("up", "widgetd", "--as", "a")
    lp.dev("release", "--as", "a")
    assert "restoring needs the lock" in lp.loop("restore", "widgetd", "--as", "a", code=1).stderr
    assert lp.bound()
    lp.take("b")
    assert "restoring needs the lock" in lp.loop("restore", "widgetd", "--as", "a", code=1).stderr
    assert lp.bound()


def test_a_signal_during_the_test_still_restores(lp):
    lp.recipe()
    lp.take("a")
    started = lp.tmp / "started"
    child = subprocess.Popen([sys.executable, str(TOOLS / "loop"), "run", "widgetd", "--as", "a", "--", "sh", "-c", f'touch "{started}"; exec sleep 60'],  # exec: a signal goes to the command, not its children, and a shell like dash would leave sleep holding the pipes
                             env=lp.env, cwd=lp.tmp, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    deadline = time.monotonic() + 30
    while not started.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    child.send_signal(signal.SIGTERM)
    child.communicate(timeout=30)
    assert child.returncode != 0 and not lp.bound() and not lp.overrides()


# --- hazard: restoring stops the process first, and never unmounts lazily


def test_restore_stops_the_process_before_the_unmount(lp):
    lp.recipe()
    lp.take("a")
    lp.loop("up", "widgetd", "--as", "a")
    lp.steps.write_text("")
    lp.loop("restore", "widgetd", "--as", "a")
    assert lp.log() == ["stop widgetd", f"umount {lp.target}", "start widgetd"]


def test_the_unmount_is_never_lazy_in_any_script(lp):
    lp.recipe(more=live(lp) + conf(lp))
    lp.take("a")
    lp.loop("up", "widgetd", "--as", "a", "--config", "widgetd:@a=9")
    lp.loop("restore", "widgetd", "--as", "a")
    text = lp.scripts() + (lp.kit / "loop" / "widgetd" / "loop.toml").read_text()
    assert "umount" in text
    for line in text.splitlines():
        assert not re.search(r"umount.*(\s-[a-z]*l|--lazy|\s-f)", line), line
    assert all(s.split()[1:] == [str(lp.target)] for s in lp.log() if s.startswith("umount"))


def test_the_order_is_what_the_script_says(lp):
    lp.recipe()
    r = lp.recipe_obj()
    script = looplib.restore_script(r, "/store", "cat boot", "cat mounts")
    at = [script.index(x) for x in (r.stop, "umount ", r.start)]
    assert at == sorted(at)
    assert script.index("until pidof") > script.index("umount ")  # the wait for the start comes after


def test_a_process_that_wont_stop_is_left_alone_and_stays_bound(lp):
    lp.recipe()
    lp.take("a")
    lp.loop("up", "widgetd", "--as", "a")
    pid = int((lp.r / "pid.widgetd").read_text())
    (lp.r / "wont-stop").touch()
    lp.steps.write_text("")
    done = lp.loop("restore", "widgetd", "--as", "a", code=1)
    assert "Not unmounting under it and not killing it" in done.stderr and "reboot is the rollback" in done.stderr.lower()
    assert lp.log() == ["stop widgetd"]
    assert lp.bound() and "loop-widgetd" in lp.overrides()
    assert Path(f"/proc/{pid}").exists()


def test_a_stop_command_that_fails_unmounts_nothing(lp):
    lp.recipe()
    lp.take("a")
    lp.loop("up", "widgetd", "--as", "a")
    (lp.r / "stop-fails").touch()
    lp.steps.write_text("")
    assert "stop command failed" in lp.loop("restore", "widgetd", "--as", "a", code=1).stderr
    assert not [s for s in lp.log() if s.startswith("umount")] and lp.bound()


def test_an_unmount_that_fails_stays_recorded_and_doesnt_retry_lazily(lp):
    lp.recipe()
    lp.take("a")
    lp.loop("up", "widgetd", "--as", "a")
    (lp.r / "umount-fails").touch()
    lp.steps.write_text("")
    assert "reboot is the rollback" in lp.loop("restore", "widgetd", "--as", "a", code=1).stderr.lower()
    assert [s for s in lp.log() if s.startswith("umount")] == [f"umount {lp.target}"]
    assert lp.bound() and "loop-widgetd" in lp.overrides()


def test_a_recipe_may_not_unmount_itself(lp):
    lp.recipe(restart='reexec = "umount -l /usr/bin/x"')
    assert "unmounts" in lp.loop("check", code=1).stderr


def test_a_reboot_rolled_it_back_so_restore_restarts_nothing(lp):
    lp.recipe(more=conf(lp))
    lp.take("a")
    lp.loop("up", "widgetd", "--as", "a", "--config", "widgetd:@a=9")
    lp.dev("release", "--as", "a")
    lp.device.set("boot_id", "boot-2")
    lp.device.set("mountinfo", "20 1 0:20 / / rw - ext4 /dev/root rw")
    lp.take("a", "--dirty", "after a reboot")
    assert "loop-widgetd" not in lp.overrides()  # dev cleared the dead bind's record
    assert "still there" in lp.loop("up", "widgetd", "--as", "a", code=1).stderr  # its original would be lost
    lp.steps.write_text("")
    lp.loop("restore", "widgetd", "--as", "a")
    assert lp.log() == [] and lp.conf.read_text() == "@a=1\n@b=2\n" and not lp.overrides()


# --- hazard: live values are reapplied


def record_live(lp, name, value, **more):
    lp.device.override(name, owner="a", boot_id="boot-1", value=value, remove="by hand", **more)


def test_lost_live_values_are_reapplied_after_each_restart(lp):
    lp.recipe(more=live(lp))
    lp.take("a")
    record_live(lp, "speed-test", "fast", setting="speed", binary="widgetd")
    record_live(lp, "plain-one", "2 words")
    lp.loop("up", "widgetd", "--as", "a")
    assert lp.ctl.read_text().splitlines() == ["plain-one=2 words", "speed=fast"]
    assert lp.log()[-2:] == ["reapply plain-one", "reapply speed"]
    lp.ctl.write_text("")
    lp.steps.write_text("")
    done = lp.loop("restore", "widgetd", "--as", "a")
    assert lp.ctl.read_text().splitlines() == ["plain-one=2 words", "speed=fast"]
    assert lp.log() == ["stop widgetd", f"umount {lp.target}", "start widgetd", "reapply plain-one", "reapply speed"]
    assert "never recorded are gone" in done.stderr
    assert set(lp.overrides()) == {"speed-test", "plain-one"}  # those are someone's records, not loop's


def test_only_values_this_process_lost_come_back(lp):
    lp.recipe(more=live(lp) + conf(lp))
    lp.take("a")
    record_live(lp, "other-binary", "x", binary="somethingelse")
    lp.device.override("old-boot", owner="a", boot_id="boot-0", value="x")
    lp.device.override("a-bind", owner="a", boot_id="boot-1", value="x", target="/usr/bin/other")
    lp.loop("up", "widgetd", "--as", "a", "--config", "widgetd:@a=9")
    assert not lp.ctl.exists()


def test_a_value_that_cant_be_reapplied_says_so_and_fails_after_finishing(lp):
    lp.recipe(more="[live]\nreapply = 'false $name $value'\n")
    lp.take("a")
    lp.loop("up", "widgetd", "--as", "a")
    record_live(lp, "speed", "fast")
    done = lp.loop("restore", "widgetd", "--as", "a", code=1)
    assert "couldn't reapply speed" in done.stderr
    assert not lp.bound() and "loop-widgetd" not in lp.overrides()


def test_with_no_reapply_a_lost_value_says_its_lost(lp):
    lp.recipe()
    lp.take("a")
    lp.loop("up", "widgetd", "--as", "a")
    record_live(lp, "speed", "fast")
    assert "so it's lost: speed" in lp.loop("restore", "widgetd", "--as", "a", code=1).stderr


def test_a_reapply_writing_to_the_fifo_is_allowed_and_reading_it_isnt(lp):
    lp.recipe(more="""[live]\nreapply = 'printf "%s %s" "$name" "$value" > /run/widgetd.ctl'\n""")
    assert "sound" in lp.loop("check").stdout
    script = looplib.restore_script(lp.recipe_obj(), "/store", "cat boot", "cat mounts")
    assert "> /run/widgetd.ctl" in script and "timeout 5" in script  # a FIFO with no reader would block otherwise
    lp.recipe(more="""[live]\nreapply = 'cat /run/widgetd.ctl; echo "$name $value"'\n""")
    assert "FIFO" in lp.loop("check", code=1).stderr


# --- hazard: the daemon is never killed by hand


@pytest.mark.parametrize("command", ["kill $(pidof widgetd)", "pkill widgetd", "killall -9 widgetd", "/bin/kill 1"])
@pytest.mark.parametrize("key", ["stop", "start", "reexec"])
def test_a_daemon_recipe_may_not_kill_it(lp, key, command):
    lp.recipe()
    toml = lp.kit / "loop" / "widgetd" / "loop.toml"
    text = toml.read_text()
    text = re.sub(rf'^{key} = .*$', "", text, flags=re.M).replace("[restart]\n", f'[restart]\n{key} = "{command}"\n')
    toml.write_text(text)
    assert "kills the daemon by hand" in lp.loop("check", code=1).stderr


def test_kill_as_part_of_a_word_is_fine(lp):
    lp.recipe(restart='reexec = "skillet-tool --kill-free"')
    assert "sound" in lp.loop("check").stdout


def test_nothing_generated_signals_anything(lp):
    lp.recipe(more=live(lp) + conf(lp))
    r = lp.recipe_obj()
    r.settings = {"@a": "9"}
    r.stop, r.start = "STOP", "START"
    for script in (looplib.up_script([r], "/store", "cat boot", "cat mounts", "who", "boot", {r.name: "sha"}),
                   looplib.restore_script(r, "/store", "cat boot", "cat mounts")):
        assert not re.search(r"(?<![\w-])(kill|pkill|killall)(?![\w-])", script)


# --- hazard: the config file keeps its inode and mode


def test_the_config_keeps_inode_and_mode_through_up_and_restore(lp):
    lp.recipe(more=conf(lp))
    before = lp.conf.stat()
    assert stat.S_IMODE(before.st_mode) == 0o664
    lp.take("a")
    lp.loop("up", "widgetd", "--as", "a", "--config", "widgetd:@a=9", "--config", "widgetd:@new=x y")
    during = lp.conf.stat()
    assert lp.conf.read_text().splitlines() == ["@b=2", "@a=9", "@new=x y"]
    assert (during.st_ino, stat.S_IMODE(during.st_mode)) == (before.st_ino, 0o664)
    lp.loop("restore", "widgetd", "--as", "a")
    after = lp.conf.stat()
    assert lp.conf.read_text() == "@a=1\n@b=2\n"
    assert (after.st_ino, stat.S_IMODE(after.st_mode)) == (before.st_ino, 0o664)


def test_the_config_commands_say_in_place(lp):
    lp.recipe(more=conf(lp))
    r = lp.recipe_obj()
    r.settings = {"@a": "9"}
    up = looplib.up_script([r], "/store", "b", "m", "who", "boot", {r.name: "sha"})
    down = looplib.restore_script(r, "/store", "b", "m")
    path = str(lp.conf)
    for script in (up, down):
        assert f"> {path}" in script and "stat -c '%i %a %u %g'" in script
        for line in script.splitlines():
            if path in line:
                assert not re.match(r"\s*(mv|cp|install|chmod|chown|rm|ln)\s", line), line
                assert "sed -i" not in line


def test_a_changed_inode_is_noticed(lp):
    lp.recipe(more=conf(lp))
    lp.take("a")
    real = shutil.which("stat")
    lp.tool_script("stat", f'''if [ "$3" = {lp.conf} ]; then
  [ -e {lp.r}/stat-seen ] && {{ echo changed; exit 0; }}
  touch {lp.r}/stat-seen
fi
exec {real} "$@"''')
    assert "changed inode, mode or owner" in lp.loop("up", "widgetd", "--as", "a", "--config", "widgetd:@a=9", code=1).stderr


def test_a_recipe_may_not_touch_the_config_itself(lp):
    lp.recipe(restart=f'reexec = "mv {lp.conf}.new {lp.conf}"', more=conf(lp))
    assert "touches the config" in lp.loop("check", code=1).stderr
    lp.recipe(restart='reexec = "sed -i s/a/b/ /etc/other"', more=conf(lp))
    assert "sed -i" in lp.loop("check", code=1).stderr


def test_a_changed_setting_is_recorded_so_devs_check_passes(lp):
    lp.recipe(more=conf(lp))
    toml = lp.home / "dev.toml"
    toml.write_text(toml.read_text().replace(f'settings = "{lp.r}/settings"', f'settings = "{lp.conf}"'))
    lp.take("a")
    lp.loop("up", "widgetd", "--as", "a", "--config", "widgetd:@a=9")
    record = lp.overrides()["loop-widgetd-config-@a"]
    assert "setting: @a" in record and "survives_reboot: yes" in record and "value: 9" in record
    lp.dev("release", "--as", "a")
    lp.take("a")  # a clean take: the difference from the seed has a record
    lp.loop("restore", "widgetd", "--as", "a")
    assert not lp.overrides()


@pytest.mark.parametrize("item, words", [("widgetd:bad key=1", "letters, digits"), ("widgetd:noequals", "NAME:KEY=VALUE"), ("other:@a=1", "NAME:KEY=VALUE")])
def test_bad_settings_are_refused_before_anything(lp, item, words):
    lp.recipe(more=conf(lp))
    lp.take("a")
    assert words in lp.loop("up", "widgetd", "--as", "a", "--config", item, code=1).stderr
    assert not lp.log()


def test_a_setting_needs_a_config_in_the_recipe(lp):
    lp.recipe()
    lp.take("a")
    assert "no [config]" in lp.loop("up", "widgetd", "--as", "a", "--config", "widgetd:@a=1", code=1).stderr


# --- hazard: a compositor restart closes every window, and says so, and needs a go


def compositor(lp, **kw):
    return lp.recipe("panelcomp", role="compositor", **kw)


def test_a_compositor_recipe_must_say_it_closes_windows(lp):
    compositor(lp, top="closes_windows = false")
    assert "closes every app window" in lp.loop("check", code=1).stderr


def test_a_compositor_restart_without_a_go_is_refused_before_anything(lp):
    compositor(lp)
    lp.take("a")
    assert "name a go" in lp.loop("up", "panelcomp", "--as", "a", code=1).stderr
    assert not lp.log() and not (lp.r / "built-log").exists()
    lp.dev("release", "--as", "a")
    lp.take("a")
    assert "unknown" in lp.loop("up", "panelcomp", "--as", "a", "--go", "go-1", code=1).stderr  # no hub, so no
    assert not lp.log()


def test_a_compositor_restart_says_so_and_the_go_goes_in_the_lock(lp):
    compositor(lp)
    lp.take("a")
    done = lp.loop("up", "panelcomp", "--as", "a", "--go-said", "yes, close them")
    assert "closes every app window" in done.stderr
    assert lp.device.field("does") == "close-apps" and "yes, close them" in lp.device.field("go")
    lp.loop("restore", "panelcomp", "--as", "a")  # the same lock's go covers the way back
    assert not lp.overrides()


def test_restoring_under_a_new_lock_needs_a_go_of_its_own(lp):
    compositor(lp)
    lp.take("a")
    lp.loop("up", "panelcomp", "--as", "a", "--go-said", "yes")
    lp.dev("release", "--as", "a")
    lp.take("a")
    lp.steps.write_text("")
    assert "name a go" in lp.loop("restore", "panelcomp", "--as", "a", code=1).stderr
    assert not lp.log() and "loop-panelcomp" in lp.overrides()
    lp.loop("restore", "panelcomp", "--as", "a", "--go-said", "yes, again")
    assert not lp.overrides()


def test_next_restart_leaves_it_bound_and_restarts_nothing(lp):
    compositor(lp)
    lp.take("a")
    lp.loop("up", "panelcomp", "--as", "a", "--go-said", "yes")
    lp.steps.write_text("")
    lp.loop("restore", "panelcomp", "--as", "a", "--next-restart")
    assert not lp.log()
    assert "removed: next agreed restart" in lp.overrides()["loop-panelcomp"]
    assert "still yours" not in lp.dev("release", "--as", "a").stderr


def test_a_compositor_that_can_reexec_needs_the_go_anyway(lp):
    compositor(lp, restart=f'reexec = "{lp.r}/bin/fake-reexec panelcomp"')
    lp.take("a")
    assert "name a go" in lp.loop("up", "panelcomp", "--as", "a", code=1).stderr  # the way back stops it


# --- re-execute where it can, and the session's own environment


def test_up_re_executes_where_it_can_and_restore_still_stops_first(lp):
    lp.recipe(restart=f'reexec = "{lp.r}/bin/fake-reexec widgetd"')
    lp.take("a")
    pid = (lp.r / "pid.widgetd").read_text()
    lp.loop("up", "widgetd", "--as", "a")
    assert lp.log() == [f"mount --bind {lp.storage}/loop-widgetd/binary {lp.target}", "reexec widgetd"]
    assert (lp.r / "pid.widgetd").read_text() == pid  # the same process
    lp.steps.write_text("")
    lp.loop("restore", "widgetd", "--as", "a")
    assert lp.log() == ["stop widgetd", f"umount {lp.target}", "start widgetd"]


def test_a_start_gets_the_sessions_environment_and_nothing_of_ours(lp):
    lp.recipe()
    lp.take("a")
    lp.loop("up", "widgetd", "--as", "a")
    env = (lp.r / "start-env.widgetd").read_text()
    assert "SESSION_VAR=from the session" in env and "SESSION_SPACED=two words" in env
    assert "GLASSINE" not in env and "LOOP_START" not in env
    (lp.r / "start-env.widgetd").unlink()
    lp.loop("restore", "widgetd", "--as", "a")
    assert "SESSION_VAR=from the session" in (lp.r / "start-env.widgetd").read_text()  # the saved one: the process had gone


def test_environment_from_takes_another_process(lp):
    lp.recipe(restart='environment_from = "sessionleader"')
    lp.take("a")
    assert "no session environment" in lp.loop("up", "widgetd", "--as", "a", code=1).stderr
    assert not lp.overrides()
    lp.start_process("sessionleader")
    lp.loop("up", "widgetd", "--as", "a")


def test_a_process_not_running_is_no_environment_and_binds_nothing(lp):
    lp.recipe()
    lp.take("a")
    (lp.r / "pid.widgetd").unlink()
    assert "no session environment" in lp.loop("up", "widgetd", "--as", "a", code=1).stderr
    assert not lp.overrides() and not lp.bound()


def test_a_start_that_doesnt_come_up_says_so_and_stays_recorded(lp):
    lp.recipe()
    lp.take("a")
    (lp.r / "wont-start").touch()
    done = lp.loop("up", "widgetd", "--as", "a", code=1)
    assert "didn't start" in done.stderr and "loop restore" in done.stderr
    assert lp.bound() and "loop-widgetd" in lp.overrides()


# --- a set swaps together


def protocol_pair(lp, a_version=3, b_version=3):
    proto = '[protocol]\nfile = "proto.h"\npattern = \'VERSION (\\d+)\'\n'
    lp.recipe("widgetd", top='set = "wire"', more=proto)
    lp.recipe("gadgetd", top='set = "wire"', more=proto)
    (lp.home / "widgetd" / "proto.h").write_text(f"VERSION {a_version}\n")
    (lp.home / "gadgetd" / "proto.h").write_text(f"VERSION {b_version}\n")


def test_a_protocol_mismatch_is_refused_before_anything(lp):
    protocol_pair(lp, 3, 4)
    done = lp.loop("up", "widgetd", "--as", "a", code=1)
    assert "doesn't agree on its protocol: gadgetd 4, widgetd 3" in done.stderr and "--set wire" in done.stderr
    assert not lp.device.log.exists() and not lp.log()


def test_a_branch_with_a_new_protocol_needs_its_other_side(lp):
    protocol_pair(lp, 3, 3)
    branch = lp.tmp / "branch"
    branch.mkdir()
    (branch / "proto.h").write_text("VERSION 4\n")
    (branch / "built").write_text("from the branch\n")
    assert "doesn't agree" in lp.loop("up", "widgetd", "--as", "a", "--source", f"widgetd={branch}", code=1).stderr
    other = lp.tmp / "other"
    other.mkdir()
    (other / "proto.h").write_text("VERSION 4\n")
    (other / "built").write_text("from the other branch\n")
    lp.take("a")
    lp.loop("up", "--set", "wire", "--as", "a", "--source", f"widgetd={branch}", "--source", f"gadgetd={other}")
    assert (lp.storage / "loop-widgetd" / "binary").read_text() == "from the branch\n"


def test_a_set_stops_all_binds_all_and_starts_all(lp):
    protocol_pair(lp)
    lp.take("a")
    lp.loop("up", "--set", "wire", "--as", "a")
    words = [s.split()[0] for s in lp.log()]
    assert words == ["stop", "stop", "mount", "mount", "start", "start"]
    lp.steps.write_text("")
    lp.loop("restore", "--set", "wire", "--as", "a")
    words = [s.split()[0] for s in lp.log()]
    assert words[:2] == ["stop", "stop"] and "umount" not in words[:2]
    assert not lp.overrides()


def test_a_constant_missing_from_whats_swapped_is_refused_and_from_the_other_side_warns(lp):
    protocol_pair(lp)
    (lp.home / "gadgetd" / "proto.h").unlink()
    lp.take("a")
    assert "isn't compared" in lp.loop("up", "widgetd", "--as", "a").stderr
    lp.loop("restore", "widgetd", "--as", "a")
    (lp.home / "widgetd" / "proto.h").unlink()
    assert "no protocol constant" in lp.loop("up", "widgetd", "--as", "a", code=1).stderr


# --- recipes


BASE = """what = "x"
binary = "x"
role = "other"
target = "/usr/bin/x"
storage = "/data/o"
[build]
repo = "x"
command = ["true"]
output = "x"
[restart]
stop = "a"
start = "b"
"""


@pytest.mark.parametrize("change, words", [
    (('what = "x"\n', ""), "what says"),
    (('binary = "x"', 'binary = "a b"'), "process name"),
    (('role = "other"', 'role = "boss"'), "role is"),
    (('target = "/usr/bin/x"', 'target = "usr/bin/x"'), "absolute path"),
    (('storage = "/data/o"', 'storage = "/run/o"'), "nothing under /run"),
    (('[build]', 'expect = "soon"\n[build]'), "duration"),
    (('repo = "x"\n', ""), "needs repo"),
    (('command = ["true"]', 'command = "true"'), "list of strings"),
    (('stop = "a"\n', ""), "needs stop"),
    (('start = "b"', 'start = "cat /run/x.ctl"'), "FIFO"),
    (('start = "b"', 'start = "b"\nstop_seconds = 0'), "stop_seconds"),
    (('start = "b"', 'starts = "b"'), "unknown keys"),
    (('[build]', 'color = "red"\n[build]'), "unknown keys"),
    (('[build]', '[protocol]\nfile = "p"\npattern = "(x)"\n[build]'), "name the set"),
    (('[build]', 'set = "s"\n[protocol]\nfile = "p"\npattern = "x"\n[build]'), "one group"),
    (('[build]', '[live]\nreapply = "set it"\n[build]'), "$name and $value"),
    (('[build]', '[config]\npath = "etc/x"\n[build]'), "absolute path"),
])
def test_bad_recipes_are_refused_in_words(lp, change, words):
    (lp.kit / "loop" / "bad").mkdir(parents=True)
    assert change[0] in BASE
    (lp.kit / "loop" / "bad" / "loop.toml").write_text(BASE.replace(*change))
    assert words in lp.loop("check", code=1).stderr
    assert words in lp.loop("up", "bad", "--as", "a", code=1).stderr
    assert not lp.device.log.exists()


def test_a_sound_recipe_and_list(lp):
    lp.recipe("panelcomp", role="compositor")
    assert "panelcomp: sound" in lp.loop("check").stdout
    assert "(does: close-apps)" in lp.loop("list").stdout


def test_show_prints_what_would_run_and_touches_nothing(lp):
    lp.recipe(more=live(lp))
    shown = lp.loop("show", "widgetd").stdout
    assert "mount --bind" in shown and "umount" in shown and "fake-stop widgetd" in shown
    assert not lp.device.log.exists() and not lp.log()
    with_dev = subprocess.run(["sh", "-n"], input=shown, text=True, capture_output=True)
    assert with_dev.returncode == 0, with_dev.stderr


def test_nothing_generated_reads_run(lp):
    lp.recipe(more=live(lp) + conf(lp))
    lp.take("a")
    lp.loop("up", "widgetd", "--as", "a", "--config", "widgetd:@a=9")
    lp.loop("restore", "widgetd", "--as", "a")
    for script in lp.scripts().split("\n----\n"):
        assert not kitlib.reads_run(script), script
    assert not devlib.touches_run("/proc/self/mountinfo")


def test_awkward_values_stay_one_word_on_the_device(lp):
    lp.recipe(more=conf(lp))
    lp.take("a")
    awkward = "it's $HOME; `x` \"y\""
    lp.loop("up", "widgetd", "--as", "a", "--config", f"widgetd:@a={awkward}")
    assert f"@a={awkward}" in lp.conf.read_text()


def test_this_repository_ships_only_sound_recipes():
    env = {k: v for k, v in os.environ.items() if k != "GLASSINE_KIT"}
    done = subprocess.run([sys.executable, str(TOOLS / "loop"), "check"], env=env, capture_output=True, text=True, timeout=20)
    assert done.returncode == 0, done.stderr


def test_the_tool_never_calls_dev_release():
    source = (TOOLS / "loop").read_text()
    code = re.sub(r'"""(.|\n)*?"""', "", source)
    assert '"release"' not in code and "do_release" not in code


def test_as_is_needed(lp):
    lp.recipe()
    assert "--as" in lp.loop("up", "widgetd", code=1).stderr
    assert "takes no command" in lp.loop("up", "widgetd", "--as", "a", "--", "true", code=1).stderr
    assert "needs a test" in lp.loop("run", "widgetd", "--as", "a", code=1).stderr


def running(lp, name="widgetd"):
    return subprocess.run([str(lp.device.bin / "pidof"), "-s", name], capture_output=True).returncode == 0


STDIN_LIVE = "[live]\nreapply = 'cat >/dev/null; echo \"reapply-g $name=$value\" >> {r}/steps'\n"


def bound_at(lp, path):
    return f" {path} " in (lp.r / "mountinfo").read_text()


def test_a_reapply_that_reads_stdin_doesnt_eat_the_rest_of_the_script(lp):
    lp.recipe("gadgetd", top='set = "wire"', more=STDIN_LIVE.format(r=lp.r))
    lp.recipe("widgetd", top='set = "wire"', more=live(lp))
    lp.take("a")
    record_live(lp, "speed", "fast")
    lp.loop("up", "--set", "wire", "--as", "a")
    assert [s for s in lp.log() if s.startswith("reapply")] == ["reapply-g speed=fast", "reapply speed"]  # the second recipe's still ran
    lp.steps.write_text("")
    done = lp.loop("restore", "--set", "wire", "--as", "a")
    assert "gadgetd: restored" in done.stdout and "widgetd: restored" in done.stdout
    assert [s for s in lp.log() if s.startswith("reapply")] == ["reapply-g speed=fast", "reapply speed"]
    assert not bound_at(lp, lp.target) and not bound_at(lp, lp.r / "usr-bin" / "gadgetd")
    assert running(lp) and running(lp, "gadgetd")
    assert not [k for k in lp.overrides() if k.startswith("loop-")] and not (lp.storage / "loop-widgetd").exists()


def test_every_script_is_one_group_with_no_stdin(lp):
    lp.recipe(more=live(lp))
    lp.take("a")
    lp.loop("up", "widgetd", "--as", "a")
    lp.loop("restore", "widgetd", "--as", "a")
    r = lp.recipe_obj()
    assert "</dev/null" in looplib.reapply_block(r, "/store", "cat boot").split("timeout 5", 1)[1].splitlines()[0]
    chunks = [x for x in lp.scripts().split("\n{\n") if "mount --bind" in x or "restore.sh </dev/null" in x]
    assert len(chunks) == 2 and all("\n} </dev/null\n" in x for x in chunks)  # up, and restore
    assert looplib.up_script([r], "/store", "b", "m", "who", "boot", {r.name: "sha"}).endswith("\n} </dev/null\n")


def test_a_second_restore_after_a_failed_start_starts_it_and_keeps_the_record_until_then(lp):
    lp.recipe(more=live(lp))
    lp.take("a")
    lp.loop("up", "widgetd", "--as", "a")
    (lp.r / "wont-start").touch()
    done = lp.loop("restore", "widgetd", "--as", "a", code=1)
    assert "didn't start" in done.stderr and "restored" not in done.stdout
    assert not lp.bound() and not running(lp)
    assert "loop-widgetd" in lp.overrides() and (lp.storage / "loop-widgetd" / "restore.sh").exists()  # what the exit message says to retry from
    (lp.r / "wont-start").unlink()
    lp.steps.write_text("")
    done = lp.loop("restore", "widgetd", "--as", "a")
    assert "widgetd: restored" in done.stdout
    assert lp.log() == ["start widgetd"]  # the stop and the unmount were already done
    assert running(lp) and not lp.bound()
    assert not lp.overrides() and not (lp.storage / "loop-widgetd").exists()


def test_a_second_restore_after_a_failed_config_write_writes_it_and_starts(lp):
    lp.recipe(more=conf(lp))
    lp.take("a")
    lp.loop("up", "widgetd", "--as", "a", "--config", "widgetd:@a=9")
    real = shutil.which("stat")
    lp.tool_script("stat", f'''if [ "$3" = {lp.conf} ] && [ -e {lp.r}/stat-breaks ]; then
  n=$(cat {lp.r}/stat-count 2>/dev/null || echo 0)
  echo $((n + 1)) > {lp.r}/stat-count
  [ $((n % 2)) = 1 ] && {{ echo changed; exit 0; }}
fi
exec {real} "$@"''')
    (lp.r / "stat-breaks").touch()
    assert "changed inode, mode or owner" in lp.loop("restore", "widgetd", "--as", "a", code=1).stderr
    assert not lp.bound() and not running(lp)
    assert "loop-widgetd" in lp.overrides() and (lp.storage / "loop-widgetd").is_dir()
    (lp.r / "stat-breaks").unlink()
    lp.steps.write_text("")
    done = lp.loop("restore", "widgetd", "--as", "a")
    assert "widgetd: restored" in done.stdout and lp.log() == ["start widgetd"]
    assert running(lp) and lp.conf.read_text() == "@a=1\n@b=2\n"
    assert not [k for k in lp.overrides() if k.startswith("loop-")] and not (lp.storage / "loop-widgetd").exists()


def test_restored_means_the_process_is_running(lp):
    lp.recipe(more=conf(lp))
    lp.take("a")
    lp.loop("up", "widgetd", "--as", "a", "--config", "widgetd:@a=9")
    lp.dev("release", "--as", "a")
    lp.device.set("boot_id", "boot-2")
    lp.device.set("mountinfo", "20 1 0:20 / / rw - ext4 /dev/root rw")
    lp.take("a", "--dirty", "after a reboot")
    (lp.r / "pid.widgetd").unlink()  # nothing started it after the reboot
    lp.steps.write_text("")
    done = lp.loop("restore", "widgetd", "--as", "a", code=1)
    assert "widgetd isn't running" in done.stderr and "restored" not in done.stdout
    assert lp.log() == []  # a rolled-back bind still restarts nothing


def test_the_override_folder_and_the_saved_environment_are_private(lp):
    lp.recipe(more=conf(lp))
    lp.take("a")
    old = os.umask(0o022)
    try:
        lp.loop("up", "widgetd", "--as", "a", "--config", "widgetd:@a=9")
    finally:
        os.umask(old)
    folder = lp.storage / "loop-widgetd"
    assert stat.S_IMODE(folder.stat().st_mode) == 0o700
    assert stat.S_IMODE((folder / "session.environ").stat().st_mode) == 0o600
    assert stat.S_IMODE((folder / "config.orig").stat().st_mode) == 0o600
    assert "SESSION_VAR" in (folder / "session.environ").read_text()


def test_help_says_the_environment_values_are_briefly_in_argv():
    text = (TOOLS / "loop").read_text()
    assert "argv" in text and "/proc/*/cmdline" in text


@pytest.mark.parametrize("command", ["sed -i s/a/b/ f", "sed -Ei s/a/b/ f", "sed -ni s/a/b/p f", "sed --in-place s/a/b/ f",
                                     "sed --in-place=.bak s/a/b/ f", "sed -i.bak s/a/b/ f", "sed -e x -i f", "sed -n -e 'a;b' -i f",
                                     "echo hi; sed -E -i s/a/b/ f"])
def test_every_way_of_saying_sed_in_place_is_refused(lp, command):
    lp.recipe(restart=f'reexec = "{command}"')
    assert "sed -i" in lp.loop("check", code=1).stderr


@pytest.mark.parametrize("command", ["sed -n p f", "sed -E s/a/b/ f", "sed -e 's/a/-i/' f", "sed s/a/b/ f; ls -i", "sed -n 's/a/b/p' f | wc -l"])
def test_sed_that_edits_nothing_in_place_is_fine(lp, command):
    lp.recipe(restart=f'reexec = "{command}"')
    assert "sound" in lp.loop("check").stdout


def test_help_and_claude_md_say_the_lints_are_text_matches():
    for text in ((TOOLS / "loop").read_text(), (TOOLS.parent / "CLAUDE.md").read_text()):
        assert "text match" in text
        assert "script a recipe calls" in text or "scripts a recipe calls" in text


def test_help_says_stop_and_start_exec_their_daemon_and_reapply_quotes_its_values():
    text = (TOOLS / "loop").read_text()
    assert "should exec its daemon" in text and 'Quote them: "$name" and "$value"' in text


def test_a_copy_over_a_bind_whose_record_was_lost_keeps_the_folder_and_its_restore(lp):
    lp.recipe()
    lp.take("a")
    lp.loop("up", "widgetd", "--as", "a")
    folder = lp.storage / "loop-widgetd"
    script, inode = (folder / "restore.sh").read_text(), (folder / "binary").stat().st_ino
    (lp.device.store / "overrides.d" / "loop-widgetd").unlink()  # the record is gone, the bind isn't
    lp.recipe(content="another build\n")
    done = lp.loop("up", "widgetd", "--as", "a", "--dirty", "a lost record", code=1)
    assert "already bound" in done.stderr
    assert lp.bound() and (folder / "binary").stat().st_ino == inode  # the file the bind sits on wasn't deleted
    assert (folder / "binary").read_text() == "a new build\n" and (folder / "restore.sh").read_text() == script
