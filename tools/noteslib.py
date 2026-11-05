# SPDX-License-Identifier: GPL-2.0-or-later
"""Notes: per topic, one current state, open items and a log, each claim saying where it comes from.

`notes` is the command line, and notesindex the search. docs/workspace.md in the hub is the design.
Notes live in notes/<area>/<topic>.md under their own local git with no remote, on this host only.
"""
from __future__ import annotations

import os
import re
import stat
import subprocess
import tomllib
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import artstore
from wslib import ToolError, home, locked, notes, private_dir

SECTIONS = ("Current", "Open", "Log")
KINDS = ("seen", "inferred", "guess", "user", "archive")
_TOPIC = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]*/[A-Za-z0-9][A-Za-z0-9._+-]*")
_ID = re.compile(r"c[0-9]+")
_RETRACTED = re.compile(r"\bRETRACTED ([0-9]{4}-[0-9]{2}-[0-9]{2}) by (c[0-9]+)\b")
# The org scan checks only the first address-shaped line of a file, so an allowed one hides the
# rest. Until that's fixed there, _flagged_addresses checks them all, with the scan's own patterns.
_ADDRESS = re.compile(r"(?<![0-9A-Fa-f:])(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}(?![0-9A-Fa-f:])")
_ADDRESS_OK = re.compile(r"(00:){5}00|([Ff][Ff]:){5}[Ff][Ff]|(([0-9A-Fa-f]{2}:){2})?(00:00:5[Ee]:).*")
_SCAN = Path(".glassine/org/scripts/safety-scan.sh")
_SECONDS = 120
_in_hook = False


@dataclass
class Item:
    """One bullet: its lines, where it sits, and what it says about itself."""
    start: int
    end: int
    section: str | None
    date: str | None
    words: str
    id: str | None = None
    kind: str | None = None
    value: str = ""
    text: str = ""
    retracted_by: str | None = None

    @property
    def tag(self) -> str:
        return f"{self.kind}: {self.value}" if self.value else (self.kind or "")

    def bases(self) -> list[str]:
        return [word.strip() for word in self.value.split(",") if word.strip()] if self.kind == "inferred" else []

    def evidence(self) -> dict[str, list[str]]:
        found: dict[str, list[str]] = {}
        for part in re.split(r",\s*(?=[A-Za-z]+=)", self.value):
            key, equals, value = part.partition("=")
            if equals and value.strip():
                found.setdefault(key.strip(), []).append(value.strip())
        return found


@dataclass
class Doc:
    lines: list[str]
    items: list[Item] = field(default_factory=list)
    problems: list[tuple[int, str]] = field(default_factory=list)

    def by_id(self) -> dict[str, Item]:
        return {item.id: item for item in self.items if item.id}


def _tag_end(words: str) -> int | None:
    """Where the bracket opening the words closes: a command in the evidence may hold brackets."""
    depth = 0
    for at, char in enumerate(words):
        depth += (char == "[") - (char == "]")
        if depth == 0:
            return at
    return None


def _read_claim(item: Item, problem) -> None:
    words = item.words
    if m := re.match(r"(c[0-9]+)\s+", words):
        item.id, words = m.group(1), words[m.end():]
    if not words.startswith("["):
        item.text = words
        if item.id:
            problem(f"{item.id} has no provenance: seen, inferred, guess, user or archive")
        elif item.section != "Log":
            problem(f"a claim in {item.section} needs provenance: {words[:40]}")
    else:
        end = _tag_end(words)
        if end is None:
            problem(f"the bracket opening the provenance never closes: {words[:40]}")
            return
        kind, _, value = words[1:end].partition(":")
        item.kind, item.value, item.text = kind.strip(), value.strip(), words[end + 1:].strip()
        if item.kind not in KINDS:
            problem(f"provenance is seen, inferred, guess, user or archive, not {item.kind}")
            item.kind = None
        elif item.kind == "inferred" and not item.value:
            problem(f"{item.id or 'a claim'} is inferred, but not from what: [inferred: c12]")
        elif item.kind == "seen":
            evidence = item.evidence()
            if "base" not in evidence or not ("cmd" in evidence or "art" in evidence):
                problem(f"{item.id or 'a claim'} is seen, so it needs a base and a command or artifact: "
                        "[seen: base=ID, art=NAME] or [seen: base=ID, cmd=WORDS]")
        if not item.text:
            problem(f"{item.id or 'a claim'} says nothing after its provenance")
    if "RETRACTED" in item.text:
        if m := _RETRACTED.search(item.text):
            item.retracted_by = m.group(2)
            if not item.id:
                problem("a retraction needs the claim's id")
        else:
            problem(f"{item.id or 'a claim'}: a retraction reads RETRACTED YYYY-MM-DD by cN")


def parse(text: str) -> Doc:
    """Read a topic file. What doesn't fit becomes a problem, so a search can still read the rest."""
    doc = Doc(text.split("\n"))
    section = day = None
    seen_sections: list[str] = []
    current: Item | None = None
    raw: list[Item] = []
    for number, line in enumerate(doc.lines):
        def problem(words: str, number=number) -> None:
            doc.problems.append((number + 1, words))

        if not line.strip():
            current = None
        elif line.startswith("## "):
            current, day, section = None, None, line[3:].strip()
            if section not in SECTIONS:
                problem(f"sections are Current, Open and Log, not {section}")
            elif section in seen_sections or list(SECTIONS).index(section) < len(seen_sections):
                problem(f"{section} is repeated or out of order: Current, Open, Log")
            seen_sections.append(section)
        elif line.startswith("### "):
            current, day = None, line[4:].strip()
            if section != "Log":
                problem(f"a dated heading belongs in Log: {day}")
            try:
                date.fromisoformat(day)
            except ValueError:
                problem(f"a Log heading is a date as YYYY-MM-DD, not {day}")
        elif line.startswith("# ") and section is None:
            current = None
        elif line.startswith("- "):
            current = Item(number, number + 1, section, day, line[2:].strip())
            raw.append(current)
            if section is None:
                problem("a claim outside Current, Open and Log")
            elif section == "Log" and day is None:
                problem("a Log entry needs a dated heading above it")
        elif line[0].isspace() and current:
            current.end, current.words = number + 1, f"{current.words} {line.strip()}"
        else:
            problem(f"only claims and headings go here, so nothing reads as fact unlabeled: {line[:40]}")
            current = None
    for name in SECTIONS:
        if name not in seen_sections:
            doc.problems.append((len(doc.lines), f"the section {name} is missing"))
    for item in raw:
        _read_claim(item, lambda words, item=item: doc.problems.append((item.start + 1, words)))
        doc.items.append(item)
    doc.problems.sort()
    return doc


def _grounded(claim: str, current: dict[str, Item], path: tuple[str, ...] = ()) -> bool:
    """Whether a Current claim comes down to something seen or said, not to itself."""
    item = current.get(claim)
    if item is None or claim in path:
        return False
    return item.kind != "inferred" or all(_grounded(base, current, (*path, claim)) for base in item.bases())


def lint(text: str) -> list[tuple[int, str]]:
    """What keeps this text out: line number and words. Nothing means it may go in."""
    doc = parse(text)
    problems = list(doc.problems)
    ids: dict[str, Item] = {}
    for item in doc.items:
        if item.id in ids:
            problems.append((item.start + 1, f"{item.id} is used twice"))
        elif item.id:
            ids[item.id] = item
    current = {i.id: i for i in doc.items if i.section == "Current" and i.id and i.kind and not i.retracted_by}
    for item in doc.items:
        problem = lambda words, item=item: problems.append((item.start + 1, words))
        name = item.id or "a claim"
        for base in item.bases():
            if _ID.fullmatch(base) and base not in ids:
                problem(f"{name} rests on {base}, and no claim here has that id")
        if item.retracted_by and item.retracted_by not in ids:
            problem(f"{name} was retracted by {item.retracted_by}, and no claim here has that id")
        if item.section != "Current" or not item.kind:
            continue
        if not item.id:
            problem(f"a claim in Current needs an id: {item.text[:40]}")
        if item.retracted_by:
            problem(f"{name} is retracted, so it can't stay in Current")
        elif item.kind in ("guess", "archive"):
            problem(f"{name} is {item.kind}, so it stays in Open or Log until it's seen again or the person says so")
        elif item.kind == "seen":
            for artifact in item.evidence().get("art", []):
                try:
                    artstore.path_of(artifact)
                except ToolError:
                    problem(f"{name} names art={artifact}, which isn't in artifacts/")
        elif item.kind == "inferred" and item.id:
            for base in item.bases():
                if base not in current:
                    problem(f"{name} rests on {base}, which isn't in Current")
                elif not _grounded(base, current, (item.id,)):
                    problem(f"{name} rests on {base}, which comes back to itself and to nothing seen")
    return sorted(problems)


def next_id(doc: Doc) -> str:
    """Ids are per topic and never come back, since nothing is deleted."""
    used = [int(m.group(1)) for line in doc.lines if (m := re.match(r"- c([0-9]+)\b", line))]
    return f"c{max(used, default=0) + 1}"


def _insert(lines: list[str], section: str, bullet: str) -> None:
    start = lines.index(f"## {section}") + 1
    end = next((n for n in range(start, len(lines)) if lines[n].startswith("## ")), len(lines))
    while end > start and not lines[end - 1].strip():
        end -= 1
    lines.insert(end, bullet)


def _log(lines: list[str], bullet: str) -> None:
    """Put a line under today's heading in Log, opening the heading if it isn't there."""
    today = f"### {date.today().isoformat()}"
    if today not in lines:
        while lines and not lines[-1].strip():
            lines.pop()
        lines.extend([today, bullet, ""])
        return
    end = lines.index(today) + 1
    while end < len(lines) and lines[end].strip() and not lines[end].startswith("#"):
        end += 1
    lines.insert(end, bullet)


def _dependents(doc: Doc, root: str) -> list[str]:
    """Current claims resting on this one, directly or through another."""
    out: list[str] = []
    grew = True
    while grew:
        grew = False
        for item in doc.items:
            if item.section == "Current" and item.id and item.id not in out and item.id != root:
                if set(item.bases()) & {root, *out}:
                    out.append(item.id)
                    grew = True
    return out


def _marked(item: Item, marker: str) -> str:
    return f"- {item.words} {marker}"


def _take_out(lines: list[str], dependents: list[str], root: str) -> None:
    """Move what rested on a claim out of Current, into Log, in words."""
    doc = parse("\n".join(lines))
    today = date.today().isoformat()
    found = doc.by_id()
    moved = [found[claim] for claim in dependents]
    for item in sorted(moved, key=lambda i: i.start, reverse=True):
        del lines[item.start:item.end]
    for item in moved:
        _log(lines, _marked(item, f"OUT {today}: rested on {root}."))


def _find(doc: Doc, claim: str) -> Item:
    item = doc.by_id().get(claim)
    if item is None:
        raise ToolError(f"no claim {claim} in this topic")
    return item


def add(text: str, words: str, tag: str | None, *, open_item: bool = False) -> tuple[str, str | None]:
    """A new line in Log, as things happen, or a new Open item. New text and its id."""
    doc = parse(text)
    lines = list(doc.lines)
    if tag is None and open_item:
        raise ToolError("an Open item needs provenance: --guess, for one")
    claim = next_id(doc) if tag else None
    bullet = f"- {claim} [{tag}] {words}" if tag else f"- {words}"
    if open_item:
        _insert(lines, "Open", bullet)
    else:
        _log(lines, bullet)
    return "\n".join(lines), claim


def set_claim(text: str, words: str, tag: str, *, replaces: str | None = None,
              open_item: bool = False) -> tuple[str, str, list[str]]:
    """A claim in Current (or Open), replacing another in place if asked. The old one goes to Log."""
    doc = parse(text)
    lines = list(doc.lines)
    section = "Open" if open_item else "Current"
    claim = next_id(doc)
    bullet = f"- {claim} [{tag}] {words}"
    if replaces is None:
        _insert(lines, section, bullet)
        return "\n".join(lines), claim, []
    old = _find(doc, replaces)
    if old.retracted_by:
        raise ToolError(f"{replaces} is already retracted")
    dependents = _dependents(doc, replaces)
    old_line = _marked(old, f"REPLACED {date.today().isoformat()} by {claim}.")
    if old.section == "Log":
        lines[old.start:old.end] = [old_line]
        _insert(lines, section, bullet)
    else:
        if old.section == section:
            lines[old.start:old.end] = [bullet]
        else:
            del lines[old.start:old.end]
            _insert(lines, section, bullet)
        _log(lines, old_line)
    _take_out(lines, dependents, replaces)
    return "\n".join(lines), claim, dependents


def retract(text: str, claim: str, by: str, why: str) -> tuple[str, list[str]]:
    """Mark a claim retracted, and take out of Current what rests on it. Nothing is deleted."""
    doc = parse(text)
    lines = list(doc.lines)
    item = _find(doc, claim)
    witness = _find(doc, by)
    if by == claim:
        raise ToolError(f"{claim} can't retract itself: name the claim that shows it wrong")
    if witness.section != "Current":
        raise ToolError(f"{by} isn't in Current, and only what's seen or said can take a claim out")
    if item.retracted_by:
        raise ToolError(f"{claim} is already retracted")
    dependents = _dependents(doc, claim)
    line = _marked(item, f"RETRACTED {date.today().isoformat()} by {by}: {why}")
    if item.section == "Log":
        lines[item.start:item.end] = [line]
    else:
        del lines[item.start:item.end]
        _log(lines, line)
    _take_out(lines, dependents, claim)
    return "\n".join(lines), dependents


def close(text: str, claim: str, why: str) -> str:
    """An Open item that's settled, or no longer asked, goes to Log with why."""
    doc = parse(text)
    lines = list(doc.lines)
    item = _find(doc, claim)
    if item.section != "Open":
        raise ToolError(f"{claim} isn't in Open")
    del lines[item.start:item.end]
    _log(lines, _marked(item, f"CLOSED {date.today().isoformat()}: {why}"))
    return "\n".join(lines)


def settings() -> dict:
    config = home() / "workspace.toml"
    try:
        return tomllib.loads(config.read_text()) if config.exists() else {}
    except tomllib.TOMLDecodeError as e:
        raise ToolError(f"{config}: {e}") from None


def topic_path(topic: str) -> Path:
    topic = topic.removesuffix(".md")
    if not _TOPIC.fullmatch(topic):
        raise ToolError(f"a topic is AREA/TOPIC, with no other folders or odd characters: {topic}")
    return notes() / f"{topic}.md"


def topics() -> list[Path]:
    """Every note there is, without creating anything."""
    if not notes().is_dir():
        return []
    return sorted(p for p in notes().rglob("*.md") if p.is_file() and ".git" not in p.relative_to(notes()).parts)


def template(topic: str) -> str:
    return f"# {topic.removesuffix('.md')}\n\n## Current\n## Open\n## Log\n"


def _env() -> dict:
    """A hook keeps git's variables: they name the index being committed. Otherwise none may leak
    in, and git may not walk up into the workspace's repository if notes/ has none of its own."""
    if _in_hook:
        return dict(os.environ)
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env["GIT_CEILING_DIRECTORIES"] = str(home())
    return env


def _git(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    done = subprocess.run(["git", "-c", "commit.gpgsign=false", *args], cwd=notes(), env=_env(), capture_output=True,
                          text=True, timeout=_SECONDS)
    if check and done.returncode:
        raise ToolError(f"git {args[0]} failed: {done.stderr.strip()}")
    return done


def _hook_text() -> str:
    tool = Path(__file__).resolve().with_name("notes")
    return f"""#!/bin/sh
# Written by tools/notes, for hand edits: the same scan and lint as notes add and set.
top=$(git rev-parse --show-toplevel)
for tool in "$top/../tools/notes" "{tool}"; do
  [ -x "$tool" ] && exec "$tool" hook
done
echo "notes: tools/notes isn't there, so nothing can scan this commit: refused" >&2
exit 1
"""


def _ensure_repo() -> None:
    """notes/ is its own local git with no remote, made on the first write."""
    root = notes()
    if not (root / ".git").exists():
        _git("init", "-q", "-b", "main")
    identity = settings().get("identity")
    if identity and _git("config", "user.name", check=False).returncode:
        _git("config", "user.name", identity["name"])
        _git("config", "user.email", identity["email"])
    _git("config", "core.hooksPath", ".git/hooks")  # a global setting would skip the hook below
    exclude = root / ".git" / "info" / "exclude"
    exclude.parent.mkdir(exist_ok=True)
    if not exclude.exists() or ".lock" not in exclude.read_text().splitlines():
        with open(exclude, "a") as f:
            f.write(".lock\n.*.new\n")
    hook = root / ".git" / "hooks" / "pre-commit"
    hook.parent.mkdir(exist_ok=True)
    if not hook.exists() or hook.read_text() != _hook_text():
        hook.write_text(_hook_text())
    hook.chmod(0o700)
    if _git("rev-parse", "--verify", "-q", "HEAD", check=False).returncode:
        _git("commit", "-q", "--no-verify", "--allow-empty", "-m", "notes: start")


def _scan_script() -> Path:
    places = [home() / _SCAN, Path(__file__).resolve().parents[1] / _SCAN]
    for place in places:
        if place.is_file():
            return place
    raise ToolError(f"the safety scan isn't at {places[0]} or {places[1]} (git submodule update --init), "
                    "and nothing goes in unscanned")


def _staged() -> list[str]:
    done = _git("diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z")
    return [name for name in done.stdout.split("\0") if name]


def _flagged_addresses(name: str) -> list[str]:
    text = _git("show", f":{name}").stdout
    flagged = []
    for number, line in enumerate(text.splitlines(), 1):
        if any(not _ADDRESS_OK.fullmatch(m.group()) for m in _ADDRESS.finditer(line)):
            flagged.append(f"FLAG  {name}  (an address at line {number})")
    return flagged


def scan() -> None:
    """Scan what's staged, from inside notes/. Refuses, in words, with where but not what."""
    done = subprocess.run([str(_scan_script()), "--staged"], cwd=notes(), env=_env(), capture_output=True, text=True,
                          timeout=_SECONDS)
    flagged = [line for line in done.stdout.splitlines() if line.startswith("FLAG")] if done.returncode else []
    for name in _staged():
        flagged += _flagged_addresses(name)
    if flagged or done.returncode:
        words = "\n".join(flagged) or done.stderr.strip() or f"the scan exited {done.returncode}"
        raise ToolError(f"the safety scan refused this, keep it in artifacts/ and name it by id:\n{words}")


def lint_or_refuse(text: str, label: str) -> None:
    problems = lint(text)
    if problems:
        raise ToolError("\n".join(f"{label}:{number}: {words}" for number, words in problems))


def hook() -> int:
    """The pre-commit hook: hand edits meet the scan and the lint too."""
    global _in_hook
    _in_hook = True
    os.environ["GLASSINE_WORKSPACE"] = str(Path.cwd().parent)
    scan()
    problems = []
    for name in _staged():
        if name.endswith(".md"):
            problems += [f"{name}:{n}: {w}" for n, w in lint(_git("show", f":{name}").stdout)]
    if problems:
        raise ToolError("\n".join(problems))
    return 0


def write(topic: str, edit) -> None:
    """Edit, lint, scan and commit one topic under the lock. `edit` gets the text, returns the new
    text. Any refusal leaves the file as it was, and nothing flagged stays in git's objects."""
    path = topic_path(topic)
    relative = str(path.relative_to(notes()))
    with locked(private_dir(notes()) / ".lock"):
        _ensure_repo()
        before = path.read_text() if path.exists() else None
        after = edit(before if before is not None else template(relative))
        lint_or_refuse(after, relative)
        private_dir(path.parent)
        temporary = path.with_name(f".{path.name}.new")
        temporary.write_text(after)
        temporary.chmod(stat.S_IRUSR | stat.S_IWUSR)
        os.replace(temporary, path)
        try:
            _git("add", "--", relative)
            scan()
            _git("commit", "-q", "--no-verify", "-m", f"notes: {relative}", "--", relative)
        except (ToolError, OSError, subprocess.TimeoutExpired) as e:
            if before is None:
                path.unlink()
            else:
                path.write_text(before)
            _git("reset", "-q", "--", relative, check=False)
            _git("prune", "--expire=now", check=False)
            raise ToolError(f"nothing was written to {relative}. {e}") from None
