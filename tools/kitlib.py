# SPDX-License-Identifier: GPL-2.0-or-later
"""What repro and bench share: reading a definition, the private-input rule, running one bounded
command, the stamp, and storing a result in artifacts/.

The repository holds the definitions (repro/<name>/repro.toml, bench/<name>/bench.toml). Results stay
on this host, in artifacts/, named by id. These tools write them there themselves, because the safety
scan only catches patterns. Nothing here asks the hub, and the device is reached only through dev.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import signal
import subprocess
import sys
import tempfile
import time
import tomllib
from dataclasses import dataclass
from pathlib import Path

import artstore
import devlib
import wslib
from wslib import ToolError

TOOLS = Path(__file__).resolve().parent
SKIPPED = 77  # never 0: a skip chained with && must not read as a pass
_ARTIFACT_ID = re.compile(rf"[0-9a-f]{{{artstore.ID_LENGTH}}}|[0-9a-f]{{64}}")  # what artstore.resolve matches: the id or the whole sha256
_ACTION = re.compile(r"[a-z0-9][a-z0-9-]*")
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_WRITE_TARGET = re.compile(r">>?\s*(\"[^\"]*\"|'[^']*'|[^\s;|&]+)")
BINARIES = ("compositor", "daemon", "boot_image")  # what dev stamp hashes or names


class Skipped(ToolError):
    """A private input isn't on this host. It's reported as skipped, and no result is stored."""


def root() -> Path:
    """The checkout these tools came from, as definitions are code. GLASSINE_KIT overrides it, for tests."""
    return Path(os.environ.get("GLASSINE_KIT") or TOOLS.parent)


def reads_run(text: str) -> bool:
    """Whether shell reads or globs /run. Reading the daemon's FIFO blocks; writing to one is how it's told things.

    A lint, not a boundary: it matches text, so a path built at run time (cd /; cat run/x, /ru?/x, ${D}n/x) passes.
    What bounds a script that blocks anyway is its timeout, and ssh_seconds for a counter read."""
    return devlib.touches_run(_WRITE_TARGET.sub(" ", text)) or any(
        "*" in m[1] and devlib.touches_run(m[1]) for m in _WRITE_TARGET.finditer(text))


def strict(table: dict, known: set[str], where: str) -> None:
    """A misspelt key must not quietly mean a weaker test."""
    unknown = sorted(set(table) - known)
    if unknown:
        raise ToolError(f"{where}: unknown keys: {', '.join(unknown)} (known: {', '.join(sorted(known))})")


def names(kind: str) -> list[str]:
    folder = root() / kind
    return sorted(p.parent.name for p in folder.glob(f"*/{kind}.toml")) if folder.is_dir() else []


def read_toml(kind: str, name: str) -> tuple[dict, Path]:
    if not _NAME.fullmatch(name):
        raise ToolError(f"not a name: {name}")
    folder = root() / kind / name
    path = folder / f"{kind}.toml"
    if not path.is_file():
        raise ToolError(f"no {kind}/{name}/{kind}.toml")
    within(folder, root() / kind, f"{kind}/{name}")
    within(path, folder, f"{kind}/{name}/{kind}.toml")
    try:
        return tomllib.loads(path.read_text()), folder
    except tomllib.TOMLDecodeError as e:
        raise ToolError(f"{path}: {e}") from e


def within(path: Path, folder: Path, what: str) -> Path:
    """PATH, once symlinks are followed, must still be under FOLDER: a link can't take a definition's source
    or the definition itself out of the folder it's reviewed in."""
    resolved = path.resolve()
    if not resolved.is_relative_to(folder.resolve()):
        raise ToolError(f"{what}: {path} is a link that leaves {folder}: it has to stay inside")
    return resolved


def words_list(table: dict, key: str, where: str) -> list[str]:
    value = table.get(key, [])
    if not (isinstance(value, list) and all(isinstance(x, str) and x.strip() for x in value)):
        raise ToolError(f"{where}: {key} is a list of strings")
    return value


def check_common(table: dict, where: str) -> dict:
    """The fields every definition has. Returns does, needs, expect and device, checked."""
    if not (isinstance(table.get("what"), str) and table["what"].strip()):
        raise ToolError(f"{where}: what says what this is, in words")
    does = words_list(table, "does", where)
    for action in does:
        if not _ACTION.fullmatch(action):
            raise ToolError(f"{where}: does: {action!r} isn't an action name")
    device = table.get("device", True)
    if not isinstance(device, bool):
        raise ToolError(f"{where}: device is true or false")
    if does and not device:
        raise ToolError(f"{where}: does names device actions, so device can't be false")
    expect = table.get("expect", "30m")
    duration(expect, f"{where}: expect")
    return {"does": does, "needs": words_list(table, "needs", where), "expect": expect, "device": device}


def duration(text, where: str) -> int:
    parts = re.fullmatch(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?", text) if isinstance(text, str) and text else None
    if not parts:
        raise ToolError(f"{where}: {text!r}: a duration like 90m, 2h or 1h30m")
    h, m, s = (int(x or 0) for x in parts.groups())
    return h * 3600 + m * 60 + s


def check_source(table: dict, where: str) -> dict:
    """[source]: file (in the definition's folder) or artifact (an id, since a name in a repository can say too much)."""
    source = table.get("source")
    if not isinstance(source, dict):
        raise ToolError(f"{where}: needs [source], with file or artifact")
    strict(source, {"file", "artifact"}, f"{where} [source]")
    if len(source) != 1:
        raise ToolError(f"{where}: [source] is one of file or artifact")
    if "artifact" in source and not _ARTIFACT_ID.fullmatch(str(source["artifact"])):
        raise ToolError(f"{where}: [source] artifact is an id ({artstore.ID_LENGTH} hex characters) or a whole sha256: a private input goes by id")
    if "file" in source and (Path(source["file"]).is_absolute() or ".." in Path(source["file"]).parts):
        raise ToolError(f"{where}: [source] file is inside the definition's folder")
    return source


def check_run(table: dict, where: str, device: bool) -> dict:
    """[run]: where = device feeds the source to the device's sh; where = host runs command on this host."""
    run = table.get("run")
    if not isinstance(run, dict):
        raise ToolError(f"{where}: needs [run]")
    strict(run, {"where", "command", "timeout", "binary"}, f"{where} [run]")
    place = run.get("where")
    if place not in ("device", "host"):
        raise ToolError(f"{where}: [run] where is device or host")
    if place == "device" and "command" in run:
        raise ToolError(f"{where}: [run] where = device runs the source; command is for the host")
    if place == "device" and not device:
        raise ToolError(f"{where}: [run] where = device, but device = false")
    if place == "host" and not (isinstance(run.get("command"), list) and run["command"] and all(isinstance(x, str) for x in run["command"])):
        raise ToolError(f"{where}: [run] where = host needs command, a list of strings")
    if run.get("binary") is not None and run["binary"] not in BINARIES:
        raise ToolError(f"{where}: [run] binary is one of {', '.join(BINARIES)}")
    if "timeout" not in run:
        raise ToolError(f"{where}: [run] timeout: every run is bounded")
    if duration(run["timeout"], f"{where}: [run] timeout") <= 0:
        raise ToolError(f"{where}: [run] timeout is more than nothing")
    return run


def resolve_input(artifact: str) -> Path:
    """A private input by id, checked against its hash. Not here means skipped, never a pass."""
    try:
        path = artstore.path_of(artifact)
    except ToolError as e:
        raise Skipped(f"skipped: no private input ({e})") from None
    if not artstore.digest(path).startswith(artifact):
        raise ToolError(f"artifact {artifact}: its file doesn't match its id")
    return path


def resolve_source(source: dict, folder: Path, where: str) -> Path:
    if "artifact" in source:
        return resolve_input(source["artifact"])
    path = folder / source["file"]
    within(path, folder, f"{where} [source] file")
    if not path.is_file():
        raise ToolError(f"{where}: [source] file {source['file']} isn't there")
    return path


def check_device_script(path: Path, where: str) -> None:
    """A script for the device is held to the rule dev keeps: nothing reads or globs /run. See reads_run: it's a lint."""
    try:
        text = path.read_text()
    except (UnicodeDecodeError, OSError) as e:
        raise ToolError(f"{where}: where = device runs the source as a shell script, and this isn't one: {e}") from None
    if reads_run(text):
        raise ToolError(f"{where}: the script reads or globs /run on the device, whose control FIFO blocks. Writing to it is fine")


def fill_command(command: list[str], source: Path, folder: Path) -> list[str]:
    """A host command with {source} and {dir} filled in, anywhere in an argument: --src={source} too."""
    return [x.replace("{source}", str(source)).replace("{dir}", str(folder)) for x in command]


def host_env(device: bool, **more: str) -> dict:
    """The environment of a host run. Only one under the lock is told how to reach the device."""
    env = {k: v for k, v in os.environ.items() if k not in ("GLASSINE_DEV_SSH", "GLASSINE_DEV_STORE")}
    if device:
        env["GLASSINE_DEV_SSH"] = devlib.load_config().ssh
    return env | more


def device_script(source: bytes, prefix: bytes = b"") -> bytes:
    """What goes to the device's sh -s. The script is one { } group, which sh reads whole before running any of it,
    with stdin from /dev/null: a command in it that reads stdin can't eat the rest of the script."""
    return prefix + b"{\n" + source + (b"" if source.endswith(b"\n") else b"\n") + b"} </dev/null\n"


def check_held(name: str | None, does: list[str]) -> None:
    """_inside runs only under dev take: the lock is held by NAME, and its does covers this run's, which took a go."""
    if not name:
        raise ToolError("--as: this needs the device, and so the lock")
    st = devlib.Device(devlib.load_config()).read()
    if not st.locked or st.lock.get("owner") != name:
        holder = st.lock.get("owner") or "nobody"
        raise ToolError(f"the lock isn't held by {name} (held by {holder}): this runs only under dev take, which run uses")
    held = [x for x in st.lock.get("does", "").split(",") if x]
    missing = [x for x in does if x not in held]
    if missing:
        raise ToolError(f"the lock's does doesn't cover {','.join(missing)}, so no go does: this runs only under dev take")


def dev(*args: str, **kw) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(TOOLS / "dev"), *args], **kw)


def take_argv(name: str, purpose: str, a, does: list[str], expect: str | None) -> list[str]:
    """dev take's arguments. Actions come from the definition, never from the command line."""
    argv = [sys.executable, str(TOOLS / "dev"), "take", "--as", name, "--purpose", purpose]
    if expect:
        argv += ["--expect", expect]
    if does:
        argv += ["--does", ",".join(does)]
    for flag, value in (("--go", a.go), ("--go-said", a.go_said), ("--dirty", a.dirty), ("--ahead", getattr(a, "ahead", None))):
        if value:
            argv += [flag, value]
    return argv


def add_take_flags(parser) -> None:
    parser.add_argument("--as", dest="name", help="the lock's name")
    parser.add_argument("--go")
    parser.add_argument("--go-said")
    parser.add_argument("--dirty", metavar="WHY")
    parser.add_argument("--ahead", metavar="WHY")


def stamp() -> dict:
    """dev stamp, as dev prints it, and what's bound over a live binary, which a stamp's hashes alone don't say."""
    got = {}
    for name, command in (("stamp", "stamp"), ("status", "status")):
        done = dev(command, "--json", capture_output=True, text=True)
        if done.returncode:
            raise ToolError(f"no {command}, so nothing runs that couldn't be recorded: {done.stderr.strip()}")
        try:
            got[name] = json.loads(done.stdout)
        except ValueError as e:
            raise ToolError(f"dev {command} printed what isn't JSON: {e}") from None
    bound = {k: v.get("target") for k, v in (got["status"].get("overrides") or {}).items() if v.get("target")}
    return {**got["stamp"], "bound": bound}


def stamped(stamp_: dict, key: str | None) -> str | None:
    """A stamp's value, or None for its "unknown ...", which is no value to compare."""
    value = stamp_.get(key) if key else None
    return None if not value or str(value).startswith("unknown") else value


def binary_hash(stamp_: dict, binary: str | None) -> str | None:
    if binary in ("compositor", "daemon"):
        return stamped(stamp_, f"{binary}_sha256")
    return stamped(stamp_, binary)


def tool_commit() -> tuple[str | None, bool]:
    """The commit of the checkout the definitions and tools came from, and whether it had changes on top.
    It's the tool's version. What ran on the device is the stamp's kit_id and base."""
    here = str(root())
    head = subprocess.run(["git", "-C", here, "rev-parse", "HEAD"], capture_output=True, text=True)
    if head.returncode:
        return None, False
    changes = subprocess.run(["git", "-C", here, "status", "--porcelain"], capture_output=True, text=True)
    return head.stdout.strip(), bool(changes.stdout.strip())


@dataclass
class Ran:
    code: int | None  # None: stopped or timed out
    seconds: float
    timed_out: bool = False
    stopped: bool = False


def run_bounded(argv: list[str], log: Path, *, stdin: bytes | None = None, seconds: int, env: dict | None = None,
                cwd: Path | None = None) -> Ran:
    """One command with its output in a file. It has its own process group, so a timeout or a signal ends all of it."""
    start = time.monotonic()
    stopped = []
    with open(log, "ab") as out, tempfile.TemporaryFile() as feed:
        feed.write(stdin or b"")
        feed.seek(0)
        try:
            child = subprocess.Popen(argv, stdout=out, stderr=subprocess.STDOUT, stdin=feed, env=env, cwd=cwd, start_new_session=True)
        except OSError as e:
            out.write(f"can't run {argv[0]}: {e}\n".encode())
            return Ran(127, 0.0)

        def end(signum, _frame=None):
            stopped.append(signum)
            try:
                os.killpg(child.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass

        old = {s: signal.signal(s, end) for s in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)}
        timed_out = False
        try:
            try:
                code = child.wait(timeout=seconds)
            except subprocess.TimeoutExpired:
                timed_out = True
                end(signal.SIGTERM)
                stopped.clear()
                try:
                    code = child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(child.pid, signal.SIGKILL)
                    code = child.wait()
        finally:
            for s, handler in old.items():
                signal.signal(s, handler)
    took = round(time.monotonic() - start, 3)
    if timed_out or stopped:
        return Ran(None, took, timed_out=timed_out, stopped=bool(stopped))
    return Ran(code, took)


def scratch(folder: str) -> tempfile.TemporaryDirectory:
    """Where a result is written before it's stored: inside the private store, so nothing lands in a checkout."""
    return tempfile.TemporaryDirectory(dir=wslib.private_dir(wslib.private_dir(wslib.artifacts()) / folder), prefix=".new-")


def result_name(prefix: str, name: str) -> str:
    moment = wslib.utc().replace("-", "").replace(":", "")
    return f"{prefix}-{name}-{moment}-{secrets.token_hex(3)}"


def store_log(log: Path, folder: str, name: str, what: str, by: str) -> str | None:
    if not log.exists():
        return None
    # output from a device or an app can hold anything of the person's, so it's user data
    entry, _ = artstore.add(log, folder, name=f"{name}.log", cls="user-data", what=what, keep="user", by=by)
    return entry["id"]


def store_result(record: dict, folder: str, name: str, what: str, by: str, tag: str, summary: dict) -> dict:
    """The record as a file in the store, so it has an id a claim can name. Results are kept until the person says."""
    with scratch(folder) as where:
        path = Path(where) / f"{name}.json"
        path.write_text(json.dumps(record, ensure_ascii=False, indent=1) + "\n")
        entry, _ = artstore.add(path, folder, name=f"{name}.json", cls="ours", what=what, keep="user", by=by,
                                sources=record.get("inputs", []), extra={tag: summary})
    return entry
