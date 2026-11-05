# SPDX-License-Identifier: GPL-2.0-or-later
"""The artifact store: one manifest.jsonl per folder of artifacts/. An entry makes a file count.

`art` is its command line, and `dumps` adds entries through it. docs/workspace.md in the hub is the
design. Results and private data live here, on this host, and never in a repository.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import tomllib
from datetime import date
from pathlib import Path

from wslib import ToolError, artifacts, home, locked, private_dir, utc

FOLDERS = ("dumps", "bases", "images", "captures", "bench", "vendor")
CLASSES = ("ours", "vendor", "user-data", "derived")
ID_LENGTH = 12
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]*")
_CHUNK = 1 << 20
_MANIFEST = "manifest.jsonl"
_LISTED = 100  # unreadable paths kept by name in a search's stats; the count is always whole

# Where `art rm` looks for references, in the order they're read. Paths starting with / or ~ have
# to exist, or the search can't be trusted and rm refuses. Paths relative to the workspace may not
# exist yet (notes/). A pattern only matches checkouts. Replaced by [art] search in workspace.toml,
# or by GLASSINE_ART_PLACES (separated by colons), which is how tests inject it.
DEFAULT_PLACES = (".", "*", "work/*/*", "artifacts", "notes", "~/BooxNoteAirHacking")
_NEVER_READ = {".git", "__pycache__", ".pytest_cache", ".venv", "node_modules"}


def check_keep(keep: str) -> str:
    """always, user, or the date until which the file is kept."""
    if keep in ("always", "user"):
        return keep
    try:
        return date.fromisoformat(keep).isoformat()
    except ValueError:
        raise ToolError(f"keep is always, user or a date as YYYY-MM-DD, not {keep}") from None


def check_name(name: str) -> str:
    if not _NAME.fullmatch(name):
        raise ToolError(f"not a name: {name}")
    return name


def _manifest(folder: str) -> Path:
    return artifacts() / folder / _MANIFEST


_TEXT_FIELDS = ("id", "name", "path", "sha256", "class", "what", "by", "keep")


def _check_entry(entry: object, where: str) -> None:
    """A hand-edited manifest line has to be an entry the commands can use, or they stop with words."""
    def bad(why: str):
        raise ToolError(f"{where} {why}: fix it by hand, nothing was changed")

    if not isinstance(entry, dict):
        bad("isn't an entry (a JSON object)")
    for field in _TEXT_FIELDS:
        if not isinstance(entry.get(field), str):
            bad(f"has no {field} as text")
    if not isinstance(entry.get("size"), int) or isinstance(entry.get("size"), bool):
        bad("has no size as a number")
    try:
        check_keep(entry["keep"])
    except ToolError as e:
        bad(f"has a keep that fails: {e}")
    tombstone = entry.get("tombstone")
    if tombstone is not None and not (isinstance(tombstone, dict) and isinstance(tombstone.get("when"), str)
                                      and isinstance(tombstone.get("why"), str)):
        bad("has a tombstone without a when and a why")


def read(folder: str) -> list[dict]:
    path = _manifest(folder)
    if not path.exists():
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as e:
        raise ToolError(f"{path} isn't UTF-8 text (byte {e.start}): fix it by hand, nothing was changed") from None
    entries = []
    # Split on "\n" only: splitlines() also splits on U+2028, U+2029 and U+0085, which _write leaves raw.
    for number, line in enumerate(text.split("\n"), 1):
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            raise ToolError(f"{path} line {number} isn't JSON: fix it by hand, nothing was changed") from None
        _check_entry(entry, f"{path} line {number}")
        entries.append(entry)
    return entries


def _write(folder: str, entries: list[dict]) -> None:
    path = _manifest(folder)
    temporary = path.with_name(path.name + ".new")
    try:  # encode everything before touching a file: a lone surrogate (non-UTF-8 argument) fails here
        data = "".join(json.dumps(entry, ensure_ascii=False) + "\n" for entry in entries).encode("utf-8")
    except UnicodeEncodeError:
        raise ToolError("a value isn't valid UTF-8 text (an argument in another encoding?), so it can't go in "
                        "the manifest; nothing was changed") from None
    try:
        with open(temporary, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        temporary.chmod(0o600)
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)  # no half-written manifest.jsonl.new left behind (a full disk, say)
        raise


def _lock():
    return locked(private_dir(artifacts()) / ".lock")


def everything() -> list[tuple[str, dict]]:
    return [(folder, entry) for folder in FOLDERS for entry in read(folder)]


def location(entry: dict) -> Path:
    return Path(entry["path"]) if entry.get("in_place") else artifacts() / entry["path"]


def resolve(ref: str) -> tuple[str, dict]:
    """The live entry with this name or id. A removed one says when and why, rather than 'unknown'."""
    known = everything()
    live = [(f, e) for f, e in known if not e.get("tombstone")]
    hits = [(f, e) for f, e in live if e["name"] == ref] or [(f, e) for f, e in live if ref in (e["id"], e["sha256"])]
    if len(hits) > 1:
        names = ", ".join(e["name"] for _, e in hits)
        raise ToolError(f"{ref} is the same content under several names, use one: {names}")
    if hits:
        return hits[0]
    gone = [e for _, e in known if ref in (e["name"], e["id"], e["sha256"])]
    if gone:
        t = gone[-1]["tombstone"]
        raise ToolError(f"{ref} was removed {t['when']}: {t['why']}")
    raise ToolError(f"no artifact {ref}")


def path_of(ref: str) -> Path:
    _, entry = resolve(ref)
    path = location(entry)
    if not path.is_file():
        raise ToolError(f"{entry['name']} is indexed, but its file is missing: {path}")
    return path


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(_CHUNK):
            h.update(chunk)
    return h.hexdigest()


def _store(source: Path, into: Path, sha256: str, copy: bool) -> None:
    """Put a file in the store, read-only. A move is a rename if it can be, and always ends verified."""
    executable = source.stat().st_mode & stat.S_IXUSR
    if not copy and source.stat().st_dev == into.parent.stat().st_dev:
        os.rename(source, into)
    else:
        temporary = into.with_name(into.name + ".new")
        try:
            shutil.copyfile(source, temporary)
            same = digest(temporary) == sha256
        except BaseException:
            temporary.unlink(missing_ok=True)  # a partial copy (a full disk) isn't left in the store
            raise
        if not same:
            temporary.unlink()
            raise ToolError(f"copying {source} gave different content, nothing was stored")
        os.rename(temporary, into)
        if not copy:  # a move leaves nothing behind: the store copy was verified above
            try:
                source.unlink()
            except OSError as e:
                into.unlink()  # one copy, where it was, rather than two
                raise ToolError(f"couldn't move {source}: its copy was verified but the original can't be removed: {e}") from None
    into.chmod(0o500 if executable else 0o400)


def check_source(source: Path, in_place: bool = False) -> Path:
    """What add refuses in a file, for a caller that stores two things and has to know before the first."""
    if source.is_symlink() or not source.is_file():
        raise ToolError(f"not a regular file: {source}")
    source = source.resolve()
    if in_place and source.is_relative_to((Path.home() / ".cache" / "glassine").resolve()):
        raise ToolError("nothing may point into the build cache: anything may delete it")
    return source


def add(source: Path, folder: str, *, name: str | None, cls: str, what: str, keep: str, by: str,
        sources: list[str] = (), parent_base: str | None = None, made: str | None = None, in_place: bool = False,
        copy: bool = False, extra: dict | None = None) -> tuple[dict, bool]:
    """Index a file, moving it into the store unless told otherwise. Returns the entry, and whether it's new."""
    source = Path(source)
    if folder not in FOLDERS:
        raise ToolError(f"not a folder of artifacts: {folder} (one of {', '.join(FOLDERS)})")
    if cls not in CLASSES:
        raise ToolError(f"not a class: {cls} (one of {', '.join(CLASSES)})")
    if not what.strip():
        raise ToolError("say what it is: what can't be empty")
    keep = check_keep(keep)
    name = check_name(name or source.name)
    source = check_source(source, in_place)
    sha256 = digest(source)
    entry = {"id": sha256[:ID_LENGTH], "name": name, "path": None, "sha256": sha256, "size": source.stat().st_size,
             "class": cls, "what": what, "made": made or utc(source.stat().st_mtime), "by": by,
             "from": list(sources), "keep": keep, "tombstone": None}
    if in_place:
        entry["in_place"] = True
        entry["path"] = str(source)
    else:
        entry["path"] = f"{folder}/{entry['id']}-{name}"
    if extra:
        entry.update(extra)
    private_dir(artifacts() / folder)
    with _lock():
        if parent_base:  # under the lock, so `art rm` can't remove the parent between this check and the entry
            entry["parent_base"] = resolve(parent_base)[1]["id"]
        for _, other in everything():
            if other["name"] == name and not other.get("tombstone"):
                if other["sha256"] == sha256:
                    return other, False
                raise ToolError(f"the name {name} is taken by different content ({other['id']})")
        # The entry first, then the file: a failed write moves nothing, and a failed move takes the entry out again.
        # A crash between the two leaves an entry whose file is missing, which `art verify` reports as an error.
        before = read(folder)
        _write(folder, before + [entry])
        if not in_place:
            try:
                _store(source, artifacts() / entry["path"], sha256, copy)
            except BaseException as failure:
                try:
                    _write(folder, before)
                except Exception as e:
                    raise ToolError(f"{name} couldn't be stored ({failure}) and its entry couldn't be taken out again ({e}): "
                                    "`art verify` shows it as missing") from None
                raise
    return entry, True


def _expand(pattern: str, base: Path) -> list[Path]:
    if pattern.startswith(("/", "~")):
        path = Path(pattern).expanduser()
        if not path.is_dir():
            raise ToolError(f"can't search {path}: it isn't there, so no reference search can be trusted. "
                            "If it's gone for good, take it out of [art] search in workspace.toml")
        return [path]
    if any(c in pattern for c in "*?["):
        return sorted(p for p in base.glob(pattern) if (p / ".git").exists())
    path = base / pattern
    return [path] if path.is_dir() else []


def places() -> list[Path]:
    """Every folder `art rm` searches, from the injected list, the configured one, or the default."""
    base = home()
    injected = os.environ.get("GLASSINE_ART_PLACES")
    if injected is not None:
        patterns = [p for p in injected.split(os.pathsep) if p]
    else:
        config = base / "workspace.toml"
        try:
            settings = tomllib.loads(config.read_text(encoding="utf-8")) if config.exists() else {}
        except (tomllib.TOMLDecodeError, UnicodeDecodeError) as e:
            raise ToolError(f"{config} can't be read: {e}") from None
        art = settings.get("art", {})
        patterns = art.get("search", DEFAULT_PLACES) if isinstance(art, dict) else None
        if not isinstance(patterns, (list, tuple)) or not all(isinstance(p, str) for p in patterns):
            raise ToolError(f"{config}: [art] search has to be a list of folders or patterns, like [\"~/old\", \"work/*/*\"]")
    found: dict[Path, None] = {}
    for pattern in patterns:
        for path in _expand(pattern, base):
            found[path.resolve()] = None
    if not found:
        raise ToolError("no places to search for references, so no search can be trusted: GLASSINE_ART_PLACES or "
                        "[art] search in workspace.toml leaves nothing that exists")
    return list(found)


def _needles(entry: dict) -> re.Pattern:
    """What counts as naming this file: its id or hash, its name or file name, or its path."""
    names = {entry["name"], location(entry).name}
    paths = {entry["path"], str(location(entry))}
    try:
        paths.add("~/" + str(location(entry).relative_to(Path.home())))
    except ValueError:
        pass
    parts = [f"(?<![0-9a-fA-F]){re.escape(entry['id'])}"]
    parts += [rf"(?<![\w.+-]){re.escape(n)}(?![\w+-]|\.\w)" for n in sorted(names)]
    parts += [re.escape(p) for p in sorted(paths)]
    return re.compile("|".join(parts).encode())


_ESCAPED = re.compile(rb"\\([0-3][0-7]{2})")


def _mounts(table: str = "/proc/self/mountinfo") -> set[str]:
    """Other file systems inside a place (a mounted image, a chroot's /proc) aren't that place's files.

    mountinfo writes a space, tab, newline and backslash in a mount point as \\040, \\011, \\012 and \\134, so
    they're turned back, or a mount point with a space in it would be walked as if it were the place's own.
    """
    try:
        rows = Path(table).read_bytes().split(b"\n")
    except OSError:
        return set()
    return {os.fsdecode(_ESCAPED.sub(lambda m: bytes([int(m.group(1), 8)]), row.split()[4])) for row in rows
            if len(row.split()) > 4}


def _names_it(path: Path, needles: re.Pattern, overlap: int) -> bool | None:
    """Whether a text file holds a needle. None if it has a NUL in its first 8 KiB: a reference is text.

    That also skips UTF-16 files and logs with a stray NUL, which are text. They are listed by name in the
    output and the tombstone (`binary_paths`), so a clear result says what it didn't read.
    """
    with open(path, "rb") as f:
        buffer = f.read(8192)
        if b"\0" in buffer:
            return None
        while True:
            if needles.search(buffer):
                return True
            more = f.read(_CHUNK)
            if not more:
                return False
            buffer = buffer[-overlap:] + more


def _artifact_hits(entry: dict, folder: str, needles: re.Pattern) -> list[str]:
    """The other live entries that refer to this one: by parent_base, or by naming it in a field."""
    hits = []
    for other_folder, other in everything():
        if other.get("tombstone") or (other_folder == folder and other["name"] == entry["name"]):
            continue
        if other.get("parent_base") == entry["id"]:
            hits.append(f"artifact {other['name']}: parent_base")
            continue
        same_content = other["sha256"] == entry["sha256"]  # a twin under another name doesn't refer to this one
        fields = {k: v for k, v in other.items() if not (same_content and k in ("id", "sha256", "path"))}
        if needles.search(json.dumps(fields, ensure_ascii=False).encode()):
            hits.append(f"artifact {other['name']}")
    return hits


def search(entry: dict, folder: str, limit: int = 50) -> tuple[list[str], dict]:
    """What refers to this entry: other entries always, then every place. Stops once `limit` files hit."""
    needles = _needles(entry)
    hits = _artifact_hits(entry, folder, needles)
    target = location(entry).resolve()
    roots = places()
    mounted = {Path(m) for m in _mounts()} - set(roots)
    pruned = set(roots) | mounted  # a root is walked as its own; nothing else is left out
    overlap = len(needles.pattern)
    store = artifacts().resolve()
    stats = {"places": [str(r) for r in roots], "files": 0, "binary": 0, "unreadable": 0, "unreadable_paths": [],
             "binary_paths": [], "unlistable": [], "mounts": []}

    def unlistable(error: OSError) -> None:
        stats["unlistable"].append(f"{error.filename}: {error.strerror}")

    for root in roots:
        for directory, subdirs, files in os.walk(root, onerror=unlistable):
            at = Path(directory)
            stats["mounts"] += [str(at / d) for d in subdirs if at / d in mounted and len(stats["mounts"]) < _LISTED]
            subdirs[:] = [d for d in subdirs if d not in _NEVER_READ and at / d not in pruned]
            for name in files:
                path = at / name
                if path == target or (name in (_MANIFEST, ".lock") and path.is_relative_to(store)):
                    continue  # the manifests were read above, entry by entry
                try:
                    kind = path.lstat().st_mode
                    if stat.S_ISLNK(kind):  # a link is a path: it refers to the file if its target does
                        stats["files"] += 1
                        if needles.search(os.fsencode(os.readlink(path))) or path.resolve() == target:
                            hits.append(f"{path} (symlink)")
                            if len(hits) >= limit:
                                stats["stopped_at"] = limit
                                return hits, stats
                        continue
                    if not stat.S_ISREG(kind):  # never open a FIFO: it would block
                        continue
                    found = _names_it(path, needles, overlap)
                except OSError:
                    stats["unreadable"] += 1
                    if len(stats["unreadable_paths"]) < _LISTED:
                        stats["unreadable_paths"].append(str(path))
                    continue
                stats["files"] += 1
                if found is None:
                    stats["binary"] += 1
                    if len(stats["binary_paths"]) < _LISTED:
                        stats["binary_paths"].append(str(path))
                elif found:
                    hits.append(str(path))
                    if len(hits) >= limit:
                        stats["stopped_at"] = limit
                        return hits, stats
    return hits, stats


def coverage(stats: dict) -> str:
    """What a search read, in words, so a clear result isn't taken for more than it was."""
    words = (f"searched {stats['files']} files in {len(stats['places'])} places: {stats['binary']} of them binary, "
             f"not read for references, and {stats['unreadable']} more unreadable")
    if stats.get("mounts"):
        words += f"; {len(stats['mounts'])} other file systems mounted inside the places weren't read"
    return words + (f"; stopped at {stats['stopped_at']} hits" if "stopped_at" in stats else "")


def _protected(entry: dict) -> str | None:
    keep = entry["keep"]
    if keep in ("always", "user"):
        return f"keep: {keep}"
    if date.fromisoformat(keep) >= date.today():
        return f"keep: until {keep}"
    return None


def _refusal(entry: dict, stats: dict, reasons: list[str]) -> str:
    return (f"not removing {entry['name']}, {coverage(stats)}:\n" + "\n".join(f"  {r}" for r in reasons) + "\n"
            f"It goes only with a retention decision naming it: --retention-said {entry['name']} \"the person's words\"")


def remove(ref: str, *, why: str, by: str, retention: tuple[str, str] | None = None, delete_in_place: bool = False,
           dry_run: bool = False) -> dict:
    """Remove an artifact if nothing holds it, leaving a tombstone. Raises with the reasons if something does.

    retention is (file named, the person's words). The hub doesn't record retention decisions, and docs/workspace.md
    in the hub says why. Nothing proves the words. It lifts keep, references and changed content, not a failed search.
    """
    if not why.strip():
        raise ToolError("say why: why can't be empty")
    folder, entry = resolve(ref)
    path = location(entry)
    hits, stats = search(entry, folder)
    if stats["unlistable"]:  # like a missing place: a search that skipped a whole folder isn't a clear result
        shown = stats["unlistable"][:10]
        more = f"\n  and {len(stats['unlistable']) - len(shown)} more" if len(stats["unlistable"]) > len(shown) else ""
        raise ToolError(f"can't search: {len(stats['unlistable'])} folders couldn't be listed, so no reference search can be "
                        "trusted:\n" + "\n".join(f"  {u}" for u in shown) + more + "\nMake them readable, or take their place "
                        "out of [art] search in workspace.toml. A retention decision doesn't lift this.")
    reasons = [] if not (reason := _protected(entry)) else [reason]
    reasons += [f"referenced by {hit}" for hit in hits]
    present = path.is_file()
    if present and digest(path) != entry["sha256"]:
        reasons.append("its file no longer matches the manifest")
    lifted = None
    if retention:
        named, words = retention
        if named not in (entry["name"], entry["id"]):
            raise ToolError(f"the retention names {named}, not this file ({entry['name']}, {entry['id']})")
        if not words.strip():
            raise ToolError("a retention carries the person's words: they can't be empty")
        lifted = {"named": named, "words": words}
    report = {"entry": entry, "reasons": reasons, "search": stats, "hits": hits}
    if reasons and not lifted:
        raise ToolError(_refusal(entry, stats, reasons))
    if dry_run:
        return report
    with _lock():
        entries = read(folder)
        for stored in entries:
            if stored["name"] == entry["name"] and stored["sha256"] == entry["sha256"] and not stored.get("tombstone"):
                # The search of places ran without the lock and can take minutes. What the manifests say can be
                # checked again now, so an `art add --parent-base` that landed meanwhile still holds the file.
                present = path.is_file()
                again = [] if not (reason := _protected(stored)) else [reason]
                again += [f"referenced by {hit}" for hit in _artifact_hits(stored, folder, _needles(stored))]
                if present and digest(path) != stored["sha256"]:
                    again.append("its file no longer matches the manifest")
                new = [r for r in again if r not in reasons]
                if new and not lifted:
                    raise ToolError(_refusal(entry, stats, new))
                reasons.extend(new)  # report and tombstone carry what was overridden
                file = "was missing" if not present else "kept in place" if entry.get("in_place") and not delete_in_place else "deleted"
                tombstone = {"when": utc(), "by": by, "why": why, "retention": lifted, "overrode": reasons,
                             "search": stats, "file": "deleting" if file == "deleted" else file}
                stored["tombstone"] = tombstone
                _write(folder, entries)  # first: a crash after this leaves a file with a tombstone, which verify flags
                if file == "deleted":
                    try:
                        path.unlink()
                    except FileNotFoundError:
                        tombstone["file"] = "was missing"
                    except OSError as e:
                        tombstone["file"], tombstone["error"] = "delete failed", str(e)
                        _write(folder, entries)
                        raise ToolError(f"{entry['name']} has its tombstone, but its file is still there and the tombstone "
                                        f"says so: {e}. Remove {path} by hand.") from None
                    else:
                        tombstone["file"] = "deleted"
                    _write(folder, entries)
                return {**report, "entry": stored}
    raise ToolError(f"{entry['name']} was removed or replaced while this ran, nothing was changed")


def verify(refs: list[str]) -> tuple[list[str], list[str]]:
    """Hash what's indexed. Returns problems, then files in the store nothing indexes (only with no refs)."""
    if refs:
        chosen = [resolve(r) for r in refs]
    else:
        chosen = [(f, e) for f, e in everything() if not e.get("tombstone")]
    problems = []
    for _, entry in chosen:
        path = location(entry)
        if not path.is_file():
            problems.append(f"missing  {entry['name']}: {path}")
            continue
        try:
            changed = digest(path) != entry["sha256"]
        except OSError as e:  # one file nobody can read doesn't hide what's wrong with the rest
            problems.append(f"unreadable  {entry['name']}: {path}: {e.strerror}")
            continue
        if changed:
            problems.append(f"changed  {entry['name']}: {path}")
    unindexed = []
    if not refs:
        live = {location(e) for _, e in everything() if not e.get("tombstone")}
        for _, entry in everything():
            tombstone = entry.get("tombstone")
            path = location(entry)
            if tombstone and tombstone["file"] in ("deleting", "deleted", "delete failed") and path.exists() and path not in live:
                problems.append(f"still there  {entry['name']}: {path} (its tombstone says {tombstone['file']})")
        for folder in (artifacts(), *(artifacts() / f for f in FOLDERS)):
            if folder.is_dir() and folder.stat().st_mode & 0o077:
                problems.append(f"too open {folder}: should be mode 0700")
        for folder in FOLDERS:
            directory = artifacts() / folder
            indexed = {e["path"] for e in read(folder)}
            for f in sorted(directory.iterdir()) if directory.is_dir() else []:
                if f.name != _MANIFEST and f"{folder}/{f.name}" not in indexed:
                    unindexed.append(str(f))
    return problems, unindexed
