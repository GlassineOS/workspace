# SPDX-License-Identifier: GPL-2.0-or-later
"""What dev is made of: its settings, the one seam to the device, what it finds there, and the hub.

Everything on the device goes through Device.run, a shell script on the stdin of a configurable ssh
command, so each step can also be done by hand and tests can run it against a local folder. The
scripts use plain POSIX sh and the device's own clock. They never read the daemon's /run files: the
control FIFO blocks whoever reads it. A setting or probe that points there is refused on loading.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import shlex
import socket
import subprocess
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

import wslib

STORE_PARTS = ("queue.d", "overrides.d", "left.d", "history.d")
LEFT_STATES = ("cpu-renderer",)  # short and fixed: anything else needs --dirty from the next session
HISTORY_KEPT = 50
TEXT_CAP = 200  # purpose, notice and other free text: data for agents, so capped
HUB_SECONDS = 3.0  # past this the hub counts as down

DEFAULTS = {
    "name": "device",  # what reports and the hub call it; dev.toml names it
    "ssh": "",  # the whole ssh command to the device, with its user and address: dev.toml or GLASSINE_DEV_SSH
    "store": "/root/glassine-dev",  # unverified: must survive a reboot, a deployment switch and a rollback
    "ssh_seconds": 30,
    "settings": "",  # the settings file the seed is compared with: particular to a device
    "seed": "",  # a file holding the seeded settings; none is shipped yet, so that check says it can't compare
    "renderer_expected": "",
    "fallback_flag": "",
    "compositor": "",  # process names, for the hash of what's running
    "daemon": "",
    "binds_watch": ["/usr", "/etc", "/bin", "/sbin", "/lib", "/opt"],
    "wait_max_hours": 4,
    "release_tries": 6,  # after CMD: the device may be rebooting, and the lock survives that
    "release_gap_seconds": 10,
}

# Plain kernel interfaces. What's particular to a device has no default: it's set in dev.toml, and
# until then the check and the stamp say "unknown".
DEFAULT_PROBES = {
    "boot_id": "cat /proc/sys/kernel/random/boot_id",
    "hangchecks": "d=$(dmesg) || exit 1; printf '%s\\n' \"$d\" | grep -c hangcheck; exit 0",
    "binds": "cat /proc/self/mountinfo",
    "battery_temp": "cat /sys/class/power_supply/battery/temp",
    "charging": "cat /sys/class/power_supply/battery/status",
    "gpu_min_clock": "cat /sys/class/devfreq/*.gpu/min_freq",
}
CHECK_PROBES = ("renderer", "hangchecks", "binds", "_fallback", "_settings", "_seed")
REPORT_PROBES = ("renderer", "base", "deployment", "boot_image", "bases", "_free_kb")
STAMP_PROBES = ("renderer", "_fallback", "base", "boot_image", "compositor_sha256", "compositor_commit",
                "daemon_sha256", "daemon_commit", "open_apps", "gpu_min_clock", "battery_temp", "charging")

_RUN = re.compile(r"(?<![\w.$}-])(?:/var)?/run(?![\w.-])")


def touches_run(text: str) -> bool:
    return bool(_RUN.search(text))


@dataclass
class Config:
    values: dict
    probes: dict

    def __getattr__(self, key):
        try:
            return self.__dict__["values"][key]
        except KeyError:
            raise AttributeError(key) from None


def load_config() -> Config:
    """Defaults, then dev.toml in the workspace (ignored by git), then the environment."""
    path = wslib.home() / "dev.toml"
    values = dict(DEFAULTS)
    probes = dict(DEFAULT_PROBES)
    if path.exists():
        try:
            read = tomllib.loads(path.read_text())
        except tomllib.TOMLDecodeError as e:
            raise wslib.ToolError(f"{path}: {e}") from e
        probes.update(read.pop("probes", {}))
        unknown = sorted(set(read) - set(DEFAULTS))
        if unknown:
            raise wslib.ToolError(f"{path}: unknown settings: {', '.join(unknown)}")
        values.update(read)
    values["ssh"] = os.environ.get("GLASSINE_DEV_SSH") or values["ssh"]
    values["store"] = os.environ.get("GLASSINE_DEV_STORE") or values["store"]
    if not values["store"].startswith("/"):
        raise wslib.ToolError(f"store {values['store']!r}: must be an absolute path on the device")
    for where, text in [*((k, values[k]) for k in ("store", "settings", "seed", "fallback_flag")), *probes.items()]:
        if touches_run(text):
            raise wslib.ToolError(f"{where} {text!r}: never read or glob /run on the device, its control FIFO blocks")
    return Config(values, probes)


def word(text: str | None) -> str:
    """Free text as one capped line, since the on-device files are a line per value."""
    return " ".join((text or "").split())[:TEXT_CAP]


def put(path: str, value) -> str:
    """Shell to write one value to one plain file."""
    return f"printf '%s\\n' {shlex.quote(str(value))} > {shlex.quote(path)}"


def fields_text(pairs) -> str:
    return "\n".join(f"{k}: {word(str(v))}" for k, v in pairs)


def parse_fields(text: str) -> dict:
    """`key: value` lines. A bare word, as `echo ready >> file` leaves, counts as a key that's set."""
    out = {}
    for line in text.splitlines():
        key, sep, value = line.partition(":")
        if key.strip():
            out[key.strip()] = value.strip() if sep else "yes"
    return out


@dataclass
class State:
    """One read of the device."""
    now: int = 0
    locked: bool = False
    files: dict = field(default_factory=dict)
    probes: dict = field(default_factory=dict)  # name: (exit status, output); absent when no probe is set

    def _under(self, folder: str) -> dict:
        return {k[len(folder) + 1:]: v for k, v in self.files.items() if k.startswith(folder + "/")}

    @property
    def lock(self) -> dict:
        return {k: v.strip() for k, v in self._under("lock.d").items()}

    @property
    def owner(self) -> str:
        return self.lock.get("owner", "")

    @property
    def boot(self) -> str:
        code, out = self.probes.get("boot_id", (1, ""))
        return out.strip() if code == 0 else ""

    @property
    def overrides(self) -> dict:
        return {k: parse_fields(v) for k, v in self._under("overrides.d").items()}

    @property
    def left(self) -> dict:
        return {k: parse_fields(v) for k, v in self._under("left.d").items()}

    @property
    def last_release(self) -> str:
        return self.files.get("last-release", "").strip()

    def dead_override(self, record: dict) -> bool:
        """A reboot kills binds and live values, so a record from another boot is of nothing."""
        return bool(self.boot) and record.get("boot_id") != self.boot and record.get("survives_reboot") != "yes"

    def queue(self, max_hours: float) -> list[dict]:
        """Entries in order, each with file, who, time, until, ready and expired."""
        entries = []
        for name, text in sorted(self._under("queue.d").items()):
            if name.endswith(".tmp"):
                continue
            stamp, _, who = name.partition("-")
            f = parse_fields(text)
            time = int(stamp) if stamp.isdigit() else 0
            until = f.get("until", "")
            until = int(until) if until.isdigit() else time + int(max_hours * 3600)
            entries.append({"file": name, "who": f.get("who") or who, "for": f.get("for", ""), "on": f.get("on", ""),
                            "time": time, "until": until, "ready": "ready" in f, "expired": until <= self.now})
        return sorted(entries, key=lambda e: (e["time"], e["file"]))


class Device:
    def __init__(self, config: Config):
        self.config = config
        if not config.ssh.strip():
            raise wslib.ToolError("no way to reach the device: set GLASSINE_DEV_SSH, or ssh in dev.toml, "
                                  "to the ssh command, e.g. ssh -o BatchMode=yes root@HOST")
        self.argv = shlex.split(config.ssh)
        self.quoted = shlex.quote(config.store)

    def run(self, script: str) -> subprocess.CompletedProcess:
        """The seam. Exit 255 is ssh's own failure. Scripts leave 0..254 for themselves."""
        try:
            done = subprocess.run([*self.argv, "sh -s"], input=script, capture_output=True, text=True,
                                  timeout=self.config.ssh_seconds)
        except subprocess.TimeoutExpired:  # a device that is rebooting can hang rather than refuse: the same as unreachable
            raise Unreachable(f"the device didn't answer in {self.config.ssh_seconds} s; nothing is known") from None
        except OSError as e:
            raise wslib.ToolError(f"can't run {self.argv[0]}: {e}") from e
        if done.returncode == 255:
            raise Unreachable(f"can't reach the device: {done.stderr.strip() or 'ssh failed'}")
        return done

    def do(self, body: str) -> str:
        """Run shell inside the store, on the device. Any failure is a failure of the command."""
        done = self.run(f"cd {self.quoted} || exit 3\n{body}\n")
        if done.returncode:
            raise wslib.ToolError(f"on the device: exit {done.returncode}: {done.stderr.strip()}")
        return done.stdout

    def prepare(self) -> str:
        return f"mkdir -p {self.quoted} && cd {self.quoted} && mkdir -p {' '.join(STORE_PARTS)} || exit 3\n"

    def probe_command(self, name: str) -> str | None:
        c = self.config
        if name == "_fallback":
            return f"if [ -e {shlex.quote(c.fallback_flag)} ]; then echo set; else echo clear; fi" if c.fallback_flag else None
        if name in ("_settings", "_seed"):
            path = c.settings if name == "_settings" else c.seed
            return f"[ -f {shlex.quote(path)} ] && cat {shlex.quote(path)}" if path else None
        if name == "_free_kb":
            return f"df -k {self.quoted} | tail -n 1"
        if name in c.probes:
            return c.probes[name]
        if name in ("compositor_sha256", "daemon_sha256"):
            process = c.compositor if name.startswith("compositor") else c.daemon
            if process:
                return f"sha256sum /proc/$(pidof -s {shlex.quote(process)})/exe | cut -d' ' -f1"
        return None

    def read(self, probes=()) -> State:
        """Everything in the store, and the named probes. Only regular files, so a stray FIFO can't block.

        Every line the script adds to what it reads starts with a mark made for this call, which nothing stored on the
        device or printed by a probe can know, so free text can't pass for one."""
        mark = "@@dev-" + secrets.token_hex(8) + ":"
        lines = [
            f"printf '{mark}now %s\\n' \"$(date +%s)\"",
            f"if cd {self.quoted} 2>/dev/null; then",
            f"  [ -d lock.d ] && echo {mark}locked",
            "  for f in lock.d/* queue.d/* overrides.d/* left.d/* hangcheck-base last-release; do",
            f"    if [ -f \"$f\" ]; then printf '{mark}file %s\\n' \"$f\"; cat \"$f\"; echo; fi",
            "  done",
            "fi",
        ]
        for name in ("boot_id", *probes):
            command = self.probe_command(name)
            if command is not None:
                lines += [f"echo '{mark}probe {name}'", f"( {command}\n) 2>/dev/null </dev/null", f"printf '\\n{mark}rc %s\\n' \"$?\""]
        done = self.run("\n".join(lines) + "\n")
        if done.returncode:
            raise wslib.ToolError(f"reading the device: exit {done.returncode}: {done.stderr.strip()}")
        return parse_read(done.stdout, mark)


class Unreachable(wslib.ToolError):
    """ssh itself failed or didn't answer in time: the device is off, rebooting or not plugged in."""


# --- the take behind a lock: lock.d/running names the process on this host whose CMD the lock is held for

def proc_stat(pid: int) -> tuple[str, str] | None:
    """(state, start time) of a process on this host, or None if there's none. Only /proc/PID/stat is read."""
    try:
        text = Path(f"/proc/{pid}/stat").read_text()
    except (FileNotFoundError, ProcessLookupError):
        return None
    rest = text.rpartition(")")[2].split()  # the name in front may hold spaces and brackets
    return (rest[0], rest[19]) if len(rest) > 19 else None


def pid_namespace() -> str:
    try:
        return os.readlink("/proc/self/ns/pid")
    except OSError:
        return ""


def take_record(pid: int) -> str:
    """What a take writes to lock.d/running, so a later release can tell whether that take is still there."""
    found = proc_stat(pid)
    return fields_text([("host", socket.gethostname()), ("pid", pid), ("start", found[1] if found else ""), ("ns", pid_namespace())])


def take_has_ended(record: str) -> bool | None:
    """Whether the take that wrote this record is gone: True or False, or None when this host can't tell. That's another
    host or pid namespace, a record it can't read (an empty one from an older dev), or no /proc."""
    fields = parse_fields(record)
    pid = fields.get("pid", "")
    if not pid.isdigit() or fields.get("host") != socket.gethostname() or not Path("/proc/self/stat").exists():
        return None
    if fields.get("ns") and fields["ns"] != pid_namespace():
        return None
    try:
        found = proc_stat(int(pid))
    except OSError:
        return None
    if found is None or found[0] in ("Z", "X"):
        return True
    return bool(fields.get("start")) and fields["start"] != found[1]  # a different start time: the pid is someone else's now


def parse_read(text: str, mark: str = "@@dev:") -> State:
    state = State()
    kind, name, body = None, None, []

    def finish():
        if kind == "file":
            state.files[name] = "\n".join(body).rstrip("\n")

    for line in text.splitlines():
        if line.startswith(mark):
            tag, _, rest = line[len(mark):].partition(" ")
            if tag == "now":
                state.now = int(rest) if rest.isdigit() else 0
            elif tag == "locked":
                state.locked = True
            elif tag == "file":
                finish()
                kind, name, body = "file", rest, []
            elif tag == "probe":
                finish()
                kind, name, body = "probe", rest, []
            elif tag == "rc":
                state.probes[name] = (int(rest) if rest.isdigit() else 1, "\n".join(body).strip())
                kind, name, body = None, None, []
            continue
        if kind:
            body.append(line)
    finish()
    return state


def mountinfo_binds(text: str, watch: list[str]) -> list[str]:
    """Mount points of bind mounts over the watched folders: a subtree of a filesystem mounted there."""
    found = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 5:
            continue
        root, point = (re.sub(r"\\([0-7]{3})", lambda m: chr(int(m[1], 8)), p) for p in parts[3:5])
        if root != "/" and any(point == w or point.startswith(w.rstrip("/") + "/") for w in watch):
            found.append(point)
    return found


def setting_lines(text: str) -> set[str]:
    return {s for s in map(str.strip, text.splitlines()) if s and not s.startswith("#")}


def setting_key(line: str) -> str:
    return line.split("=", 1)[0].split()[0]


@dataclass
class Check:
    name: str
    state: str  # ok, fail, unknown, unconfirmed
    words: str

    def __str__(self):
        return f"{self.name}: {self.state}: {self.words}"


def device_check(config: Config, st: State) -> list[Check]:
    """The checks a take runs and a release records. Unknown says so and doesn't stop a take; a set
    probe that fails is a failure, as the check can't vouch for the device."""
    out = []

    def probe(name):
        return st.probes.get(name)

    got = probe("renderer")
    left = st.left.get("cpu-renderer")
    if got is None or not config.renderer_expected:
        out.append(Check("renderer", "unknown", "no probe or expected value set in dev.toml"))
    elif got[0]:
        out.append(Check("renderer", "fail", f"couldn't read it (exit {got[0]})"))
    elif got[1] == config.renderer_expected:
        out.append(Check("renderer", "ok", got[1]))
    elif left:
        out.append(Check("renderer", "ok", f"{got[1]}, left as cpu-renderer by {left.get('owner', '?')}: {left.get('why', '')}"))
    else:
        out.append(Check("renderer", "fail", f"{got[1]}, expected {config.renderer_expected}"))

    got = probe("_fallback")
    if got is None:
        out.append(Check("fallback", "unknown", "no fallback_flag set in dev.toml"))
    elif got[1] == "set":
        out.append(Check("fallback", "fail", f"{config.fallback_flag} is there"))
    else:
        out.append(Check("fallback", "ok", "clear"))

    got = probe("hangchecks")
    if got is None or got[0] or not got[1].isdigit():
        out.append(Check("hangchecks", "fail", "couldn't count them"))
    else:
        since = int(got[1]) - hangcheck_base(st)
        out.append(Check("hangchecks", "fail", f"{since} since the last release") if since > 0 else Check("hangchecks", "ok", "none"))

    overrides = {k: v for k, v in st.overrides.items() if not st.dead_override(v)}
    got = probe("binds")
    if got is None or got[0]:
        out.append(Check("binds", "fail", "couldn't read the mounts"))
    else:
        recorded = {v.get("target") for v in overrides.values()}
        stray = [b for b in mountinfo_binds(got[1], config.binds_watch) if b not in recorded]
        out.append(Check("binds", "fail", f"no override record: {', '.join(stray)}") if stray else Check("binds", "ok", "all recorded"))

    now, seed = probe("_settings"), probe("_seed")
    if now is None or seed is None:
        out.append(Check("settings", "unknown", "no seed set in dev.toml, so nothing to compare with"))
    elif now[0] or seed[0]:
        out.append(Check("settings", "unknown", "the settings or seed file isn't there, so nothing to compare with"))
    else:
        recorded = set(overrides) | {v.get("setting") for v in overrides.values()}
        keys = sorted({setting_key(s) for s in setting_lines(now[1]) ^ setting_lines(seed[1])} - recorded)
        out.append(Check("settings", "fail", f"differ from the seed, no override record: {', '.join(keys)}") if keys else Check("settings", "ok", "as seeded or recorded"))

    for name, record in sorted(overrides.items()):
        if not record.get("target"):
            out.append(Check("live setting", "unconfirmed", f"{name}={record.get('value', '')}: recorded, unconfirmed"))
    return out


def hangcheck_base(st: State) -> int:
    boot, _, count = st.files.get("hangcheck-base", "").strip().partition(" ")
    return int(count) if boot == st.boot and count.isdigit() else 0


def move_hangcheck_base(st: State) -> str:
    """Shell to count from here on, or nothing when the count isn't known."""
    got = st.probes.get("hangchecks")
    if got is None or got[0] or not got[1].isdigit():
        return ":"
    return put("hangcheck-base", f"{st.boot} {got[1]}")


# --- the hub: one JSON line in, one out, hubd's socket on this host. Never needed for the device.

def hub_socket() -> Path:
    if os.environ.get("GLASSINE_HUB_SOCKET"):
        return Path(os.environ["GLASSINE_HUB_SOCKET"])
    base = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    return Path(base) / "glassine-hub" / "hubd.sock"


def hub_ask(request: dict) -> dict | None:
    """The answer, or None for anything that isn't one in time: no socket, no such op, past the limit."""
    limit = float(os.environ.get("GLASSINE_HUB_SECONDS") or HUB_SECONDS)
    try:
        with socket.socket(socket.AF_UNIX) as s:
            s.settimeout(limit)
            s.connect(str(hub_socket()))
            s.sendall(json.dumps(request).encode() + b"\n")
            with s.makefile("rb") as f:
                reply = json.loads(f.readline(1 << 20))
    except (OSError, ValueError):
        return None
    return reply if isinstance(reply, dict) and reply.get("ok") else None


def hub_says(actions: list[str], go: str | None, device: str, spend: bool = False) -> tuple[str, str]:
    """Ask about a go: ("yes" or "no" or "unknown", the hub's words). With spend, a yes uses up a run of it."""
    reply = hub_ask({"op": "decision.use" if spend else "decision.check", "agent_token": os.environ.get("GLASSINE_HUB_MCP_TOKEN"), "kind": "go",
                     "id": go, "actions": actions, "target": device})
    if reply is None or reply.get("answer") not in ("yes", "no"):
        return "unknown", "the hub didn't answer"
    return reply["answer"], word(reply.get("words"))


def hub_holding() -> bool | None:
    """"I've got the device": one read that needs no token. None when the hub doesn't say."""
    reply = hub_ask({"op": "decision.holding"})
    return reply["holding"] if reply is not None and isinstance(reply.get("holding"), bool) else None
