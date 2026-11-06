# SPDX-License-Identifier: GPL-2.0-or-later
"""What loop runs on the device, generated from a recipe, so the hazards are in the tool.

A recipe (loop/<name>/loop.toml) is data about one binary, and these functions turn it into POSIX sh.
They touch nothing, so the tests and `loop show` read exactly what would run:

- Restoring stops the process first, and never unmounts lazily under it. If it won't stop, or the
  unmount fails, the bind stays, and a reboot is the rollback.
- Live values die with the process, so they're reapplied after each restart, from overrides.d.
- A daemon is never killed by hand: only its own stop command, and the wait after it.
- The config file is written in place, keeping its inode and mode, and that's checked each time.
- A start uses the environment of the running session, saved from /proc before anything stops.
- Restarting a compositor closes every app window, so its recipe names an action that needs a go.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from shlex import quote

import devlib
import kitlib
from wslib import ToolError

ROLES = ("daemon", "compositor", "other")
CLOSE_APPS = "close-apps"
KEYS = {"what", "binary", "role", "target", "storage", "set", "expect", "closes_windows", "does", "build", "protocol", "restart", "live", "config"}
_BINARY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]*")
_KILLS = re.compile(r"(?<![\w-])(kill|pkill|killall|skill)(?![\w-])")
_UNMOUNT = re.compile(r"(?<![\w-])umount(?![\w-])")
_KEY = re.compile(r"[@A-Za-z0-9_-]+")


@dataclass
class Recipe:
    name: str
    what: str
    binary: str
    role: str
    target: str
    storage: str
    expect: str
    closes_windows: bool
    does: list[str]
    repo: str
    build: list[str]
    output: str
    build_seconds: int
    stop: str
    start: str
    reexec: str | None
    stop_seconds: int
    start_seconds: int
    environment_from: str
    reapply: str | None = None
    config: str | None = None
    set: str | None = None
    protocol: dict | None = None
    settings: dict = field(default_factory=dict)

    @property
    def override(self) -> str:
        return f"loop-{self.name}"

    @property
    def folder(self) -> str:
        return f"{self.storage.rstrip('/')}/{self.override}"

    @property
    def log(self) -> str:
        return f"{self.storage.rstrip('/')}/{self.override}.start.log"


def need_text(table: dict, key: str, where: str) -> str:
    value = table.get(key)
    if not (isinstance(value, str) and value.strip()):
        raise ToolError(f"{where}: needs {key}")
    return value


def absolute(path: str, key: str, where: str) -> str:
    if not path.startswith("/") or ".." in path.split("/") or devlib.touches_run(path) or re.search(r"\s", path):
        raise ToolError(f"{where}: {key} {path!r}: an absolute path, with no spaces and nothing under /run")
    return path


def seconds_of(table: dict, key: str, default: int, where: str) -> int:
    value = table.get(key, default)
    if not (isinstance(value, int) and value > 0):
        raise ToolError(f"{where}: {key} is a number of seconds, more than 0")
    return value


def check_shell(text: str, key: str, role: str, config: str | None, where: str) -> None:
    """What a recipe's own commands may not do. The tool does the unmount and the config write itself."""
    if _UNMOUNT.search(text):
        raise ToolError(f"{where}: {key} unmounts. loop does that itself, after the process stops, and never lazily")
    if role == "daemon" and _KILLS.search(text):
        raise ToolError(f"{where}: {key} kills the daemon by hand. Use its own stop command")
    if kitlib.reads_run(text):
        raise ToolError(f"{where}: {key} reads or globs /run on the device, whose control FIFO blocks. Writing to it is fine")
    if config and config in text:
        raise ToolError(f"{where}: {key} touches the config file. loop writes it in place itself, keeping its inode and mode")
    if re.search(r"(?<![\w-])sed\s+(-\w+\s+)*-i", text):
        raise ToolError(f"{where}: {key} uses sed -i, which replaces a file's inode")


def load(name: str) -> Recipe:
    table, _folder = kitlib.read_toml("loop", name)
    where = f"loop/{name}"
    kitlib.strict(table, KEYS, where)
    binary = need_text(table, "binary", where)
    if not _BINARY.fullmatch(binary):
        raise ToolError(f"{where}: binary is a process name")
    role = table.get("role")
    if role not in ROLES:
        raise ToolError(f"{where}: role is one of {', '.join(ROLES)}")
    closes = table.get("closes_windows", False)
    if not isinstance(closes, bool):
        raise ToolError(f"{where}: closes_windows is true or false")
    if role == "compositor" and not closes:
        raise ToolError(f"{where}: restarting a compositor closes every app window, so closes_windows = true, and that needs a go")
    expect = table.get("expect", "30m")
    kitlib.duration(expect, f"{where}: expect")
    does = kitlib.words_list(table, "does", where)
    if closes and CLOSE_APPS not in does:
        does = [*does, CLOSE_APPS]
    kitlib.check_common({"what": table.get("what"), "does": does}, where)

    build = table.get("build")
    if not isinstance(build, dict):
        raise ToolError(f"{where}: needs [build]")
    kitlib.strict(build, {"repo", "command", "output", "timeout"}, f"{where} [build]")
    if not (isinstance(build.get("command"), list) and build["command"] and all(isinstance(x, str) for x in build["command"])):
        raise ToolError(f"{where}: [build] command is a list of strings, building for arm64")
    build_seconds = kitlib.duration(build.get("timeout", "30m"), f"{where}: [build] timeout")

    restart = table.get("restart")
    if not isinstance(restart, dict):
        raise ToolError(f"{where}: needs [restart]")
    kitlib.strict(restart, {"stop", "start", "reexec", "stop_seconds", "start_seconds", "environment_from"}, f"{where} [restart]")
    live = table.get("live", {})
    kitlib.strict(live, {"reapply"}, f"{where} [live]")
    config = table.get("config", {})
    kitlib.strict(config, {"path"}, f"{where} [config]")
    path = absolute(config["path"], "[config] path", where) if config else None
    for label, text in (("stop", need_text(restart, "stop", where)), ("start", need_text(restart, "start", where)),
                        ("reexec", restart.get("reexec")), ("reapply", live.get("reapply"))):
        if text is not None:
            if not isinstance(text, str):
                raise ToolError(f"{where}: {label} is shell, as a string")
            check_shell(text, label, role, path, where)
    if "reapply" in live and not ("$name" in live["reapply"] and "$value" in live["reapply"]):
        raise ToolError(f"{where}: [live] reapply sets a value, and gets it as $name and $value")
    environment_from = restart.get("environment_from", binary)
    if not _BINARY.fullmatch(environment_from):
        raise ToolError(f"{where}: environment_from is a process name")

    protocol = table.get("protocol")
    group = table.get("set")
    if protocol is not None:
        kitlib.strict(protocol, {"file", "pattern"}, f"{where} [protocol]")
        need_text(protocol, "file", f"{where} [protocol]")
        try:
            if re.compile(need_text(protocol, "pattern", f"{where} [protocol]")).groups != 1:
                raise ToolError(f"{where}: [protocol] pattern has one group, the constant")
        except re.error as e:
            raise ToolError(f"{where}: [protocol] pattern: {e}") from None
        if not group:
            raise ToolError(f"{where}: [protocol] is compared across a set, so name the set")
    return Recipe(name=name, what=table["what"], binary=binary, role=role, target=absolute(need_text(table, "target", where), "target", where),
                  storage=absolute(need_text(table, "storage", where), "storage", where), expect=expect, closes_windows=closes, does=does,
                  repo=need_text(build, "repo", f"{where} [build]"), build=build["command"], output=need_text(build, "output", f"{where} [build]"),
                  build_seconds=build_seconds, stop=restart["stop"], start=restart["start"], reexec=restart.get("reexec"),
                  stop_seconds=seconds_of(restart, "stop_seconds", 15, where), start_seconds=seconds_of(restart, "start_seconds", 15, where),
                  environment_from=environment_from, reapply=live.get("reapply"), config=path, set=group, protocol=protocol)


def check_setting(key: str, value: str) -> None:
    if not _KEY.fullmatch(key):
        raise ToolError(f"config key {key!r}: letters, digits, '@', '_' and '-' only")
    if "\n" in value:
        raise ToolError(f"config {key}: one line")


# --- shell. Everything from a recipe or the command line is quoted, or checked above. A recipe's own commands are shell.


def grouped(script: str) -> str:
    """What goes to the device's sh -s: one { } group, read whole before any of it runs, with no stdin, so a command in it
    that reads stdin can't eat the rest of the script. The same guard as kitlib.device_script."""
    return kitlib.device_script(script.encode()).decode()


def fail(words: str, code: int) -> str:
    return f"{{ echo {quote('loop: ' + words)} >&2; exit {code}; }}"


def is_bound(r: Recipe, binds: str) -> str:
    """Whether the target is a mount point right now, by dev's own mounts probe."""
    return f"( {binds}\n) 2>/dev/null </dev/null | awk -v t={quote(r.target)} 'BEGIN {{ r = 1 }} $5 == t {{ r = 0 }} END {{ exit r }}'"


def running(r: Recipe) -> str:
    return f"pidof -s {quote(r.binary)} >/dev/null 2>&1"


def stop_block(r: Recipe) -> str:
    """Its own stop command and then a wait. Past the wait it stops trying: nothing gets killed, nothing unmounted."""
    still = f"{r.binary} is still running after {r.stop_seconds} s. Not unmounting under it and not killing it: reboot is the rollback"
    return f"""if {running(r)}; then
  ( {r.stop}
  ) </dev/null >&2 || {fail(f"{r.binary}: its stop command failed. Nothing was unmounted: reboot is the rollback", 21)}
  loop_n=0
  while {running(r)}; do
    loop_n=$((loop_n + 1))
    if [ "$loop_n" -gt {r.stop_seconds} ]; then {fail(still, 22)}; fi
    sleep 1
  done
fi"""


def unmount_block(r: Recipe, binds: str) -> str:
    """Only after stop_block. Plain umount: if it can't, the bind stays."""
    return f"""umount {quote(r.target)} || {fail(f"can't unmount {r.target}, so {r.binary} is stopped and it stays bound: reboot is the rollback", 23)}
if {is_bound(r, binds)}; then {fail(f"{r.target} is still bound after the unmount: reboot is the rollback", 24)}; fi"""


def start_block(r: Recipe) -> str:
    """The session's own environment, saved before anything stopped, and nothing else."""
    return f"""LOOP_START={quote(r.start)}
export LOOP_START
nohup xargs -0 sh -c 'exec env -i "$@" sh -c "$LOOP_START" </dev/null' loop-start < {quote(r.folder + '/session.environ')} >> {quote(r.log)} 2>&1 &
unset LOOP_START
loop_n=0
until {running(r)}; do
  loop_n=$((loop_n + 1))
  if [ "$loop_n" -gt {r.start_seconds} ]; then {fail(f"{r.binary} didn't start in {r.start_seconds} s: see {r.log}", 25)}; fi
  sleep 1
done"""


def running_hash(r: Recipe) -> str:
    return f'sha256sum "/proc/$(pidof -s {quote(r.binary)})/exe" 2>/dev/null | cut -d" " -f1'


def verify_block(r: Recipe, sha: str) -> str:
    """A start isn't proof: a bind that failed quietly would leave the old binary running."""
    return f"""loop_n=0
until [ "$({running_hash(r)})" = {quote(sha)} ]; do
  loop_n=$((loop_n + 1))
  if [ "$loop_n" -gt {r.start_seconds} ]; then {fail(f"{r.binary} isn't running the copied binary", 26)}; fi
  sleep 1
done"""


def reapply_block(r: Recipe, store: str, boot: str) -> str:
    """Live values from overrides.d: the ones it can see. One set and never recorded is lost, and can't be known."""
    records = quote(store + "/overrides.d")
    timed = f"timeout 5 sh -c {quote(r.reapply)} </dev/null" if r.reapply else "false"
    word = "couldn't reapply" if r.reapply else "no reapply in the recipe, so it's lost:"
    return f"""loop_boot=$( ( {boot}
) 2>/dev/null </dev/null )
for loop_f in {records}/*; do
  [ -f "$loop_f" ] || continue
  grep -q '^target: .' "$loop_f" && continue
  grep -q '^config: ' "$loop_f" && continue
  loop_b=$(sed -n 's/^binary: //p' "$loop_f")
  [ -z "$loop_b" ] || [ "$loop_b" = {quote(r.binary)} ] || continue
  if [ "$(sed -n 's/^boot_id: //p' "$loop_f")" != "$loop_boot" ] && [ "$(sed -n 's/^survives_reboot: //p' "$loop_f")" != yes ]; then continue; fi
  name=$(sed -n 's/^setting: //p' "$loop_f")
  [ -n "$name" ] || name=$(basename "$loop_f")
  value=$(sed -n 's/^value: //p' "$loop_f")
  export name value
  if {timed}; then echo "loop: reapplied $name" >&2; else echo "loop: warning: {word} $name" >&2; loop_lost=1; fi
done
echo 'loop: values set live and never recorded are gone, and nothing can see them' >&2"""


def write_config(path: str, source: str, code: int) -> str:
    """In place: a redirect into the file keeps its inode and mode, and a rename or sed -i wouldn't. Checked, not assumed."""
    return f"""loop_was=$(stat -c '%i %a %u %g' {quote(path)}) || {fail(f"can't stat {path}", code)}
cat {quote(source)} > {quote(path)} || {fail(f"can't write {path}", code)}
[ "$(stat -c '%i %a %u %g' {quote(path)})" = "$loop_was" ] || {fail(f"{path} changed inode, mode or owner", code)}"""


def edit_config(r: Recipe, settings: dict) -> str:
    if not settings:
        return ":"
    new = r.folder + "/config.new"
    lines = [f"cat {quote(r.folder + '/config.orig')} > {quote(new)} || exit 27"]
    for key, value in settings.items():
        lines += [f"grep -v {quote('^' + key + '=')} {quote(new)} > {quote(new + '.next')}",
                  f"printf '%s\\n' {quote(f'{key}={value}')} >> {quote(new + '.next')}",
                  f"cat {quote(new + '.next')} > {quote(new)}", f"rm -f {quote(new + '.next')}"]
    return "\n".join(lines) + "\n" + write_config(r.config, new, 27)


def record_lines(r: Recipe, owner: str, boot: str, sha: str) -> str:
    remove = f"sh {r.folder}/restore.sh"
    return devlib.fields_text([("owner", owner), ("boot_id", boot), ("value", sha[:12]), ("remove", remove), ("survives_reboot", "no"),
                               ("removed", "now"), ("target", r.target), ("binary", r.binary)])


def config_record(r: Recipe, owner: str, boot: str, key: str, value: str) -> tuple[str, str]:
    """The file outlives a reboot, so its record does too."""
    return f"{r.override}-config-{key}", devlib.fields_text(
        [("owner", owner), ("boot_id", boot), ("value", value), ("remove", f"sh {r.folder}/restore.sh"), ("survives_reboot", "yes"),
         ("removed", "now"), ("setting", key), ("config", r.config), ("binary", r.binary)])


def up_script(recipes: list[Recipe], store: str, boot_probe: str, binds: str, owner: str, boot: str, shas: dict[str, str]) -> str:
    """Bind one binary, or a set together: everything prepared, then all stopped, all bound, all started."""
    records = store + "/overrides.d/"
    out = ["set -u", "loop_lost=0"]
    for r in recipes:
        out += [f"if {is_bound(r, binds)}; then {fail(f'{r.target} is already bound: restore it first', 20)}; fi",
                f"loop_pid=$(pidof -s {quote(r.environment_from)}) || {fail(f'{r.environment_from} is not running, so there is no session environment to start {r.binary} with', 20)}",
                f'cat "/proc/$loop_pid/environ" > {quote(r.folder + "/session.environ")} || {fail("cannot read the session environment", 20)}']
        if r.settings:
            out.append(f"cat {quote(r.config)} > {quote(r.folder + '/config.orig')} || {fail(f'cannot read {r.config}', 20)}")
    for r in recipes:
        if not r.reexec:
            out.append(stop_block(r))
    for r in recipes:
        # the record first: a connection lost here leaves a record with no bind, never a bind with no record
        out.append(devlib.put(records + r.override, record_lines(r, owner, boot, shas[r.name])))
        out.append(f"mount --bind {quote(r.folder + '/binary')} {quote(r.target)} || {{ rm -f {quote(records + r.override)}; {fail(f'cannot bind over {r.target}', 28)}; }}")
        for key, value in r.settings.items():
            name, text = config_record(r, owner, boot, key, value)
            out.append(devlib.put(records + name, text))
        out.append(edit_config(r, r.settings))
    for r in recipes:
        if r.reexec:
            out.append(f"( {r.reexec}\n) </dev/null >&2 || {fail(f'{r.binary}: its re-exec command failed: loop restore', 29)}")
        else:
            out.append(start_block(r))
        out.append(verify_block(r, shas[r.name]))
    for r in recipes:
        out.append(reapply_block(r, store, boot_probe))
    out.append('exit "$loop_lost"')
    return grouped("\n".join(out) + "\n")


def restore_script(r: Recipe, store: str, boot_probe: str, binds: str) -> str:
    """Kept in the override's folder, so it also works by hand, by anyone, with no tool and no recipe."""
    records = quote(store + "/overrides.d")
    restart = "\n".join([stop_block(r), unmount_block(r, binds)])
    after = "\n".join([start_block(r), reapply_block(r, store, boot_probe)])
    return f"""#!/bin/sh
# Takes {r.target} back to what the system has. Stop first, plain unmount, and then the start.
set -u
loop_lost=0
if {is_bound(r, binds)}; then loop_was_bound=1; else loop_was_bound=0; fi
if [ "$loop_was_bound" = 1 ]; then
{restart}
fi
if [ -f {quote(r.folder + '/config.orig')} ]; then
{write_config(r.config, r.folder + '/config.orig', 30) if r.config else ':'}
fi
if [ "$loop_was_bound" = 1 ]; then
{after}
else
  echo 'loop: {r.target} was not bound (a reboot rolls it back): no restart' >&2
fi
rm -f {records}/{r.override} {records}/{r.override}-config-*
rm -rf {quote(r.folder)}
exit "$loop_lost"
"""
