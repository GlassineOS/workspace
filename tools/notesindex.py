# SPDX-License-Identifier: GPL-2.0-or-later
"""The word search behind `notes find`: SQLite FTS5, in a private folder under the cache.

It's built from the notes and from the sources in workspace.toml, which never passed the scan. So
it stays in a 0700 folder, nothing reads it but this tool, and every row says what it is in words.
Sources are read only when they're configured; there are none by default:

  [[notes.source]]
  name = "memory"           # the word each row shows
  path = "~/some/folder"    # relative paths start at the workspace
  kind = "text"             # text (paragraphs) or transcripts (.jsonl, one message a line)
  glob = "**/*.md"          # optional; the kind sets the default
"""
from __future__ import annotations

import json
import os
import re
import stat
from pathlib import Path

import noteslib
from wslib import ToolError, home, locked, notes, private_dir

_SCHEMA = 1
_CHUNK = 2000
_KINDS = {"text": "**/*.md", "transcripts": "**/*.jsonl"}
# Current ranks first, and a retracted theory can't pass as a fact. Retracted rows come before the
# archive, so a flood of old text can't push out a warning that something was already disproved.
_RANK = {"current": 0, "open": 1, "history": 2, "retracted": 3, "archive, unreviewed": 4}
_ARCHIVE_SOURCE_RANK = 5
_SKIP_BLOCKS = {"thinking", "redacted_thinking", "image", "document"}


def _sqlite3():
    """The module, once FTS5 is known to work here, or a refusal in words."""
    try:
        import sqlite3
    except ImportError:
        raise ToolError("notes find needs Python's sqlite3, and this Python has none. Nothing was searched.") from None
    try:
        sqlite3.connect(":memory:").execute("CREATE VIRTUAL TABLE probe USING fts5(x)")
    except sqlite3.Error:
        raise ToolError(f"notes find needs SQLite with FTS5, and this Python's SQLite {sqlite3.sqlite_version} "
                        "doesn't have it. Nothing was searched.") from None
    return sqlite3


def cache() -> Path:
    return private_dir(Path.home() / ".cache" / "glassine" / "notes-index")


def sources() -> list[dict]:
    """The configured sources, checked. A wrong entry stops the search rather than narrowing it."""
    found = []
    for entry in noteslib.settings().get("notes", {}).get("source", []):
        name, path, kind = entry.get("name"), entry.get("path"), entry.get("kind")
        if not name or not path or kind not in _KINDS or name == "notes":
            raise ToolError(f"workspace.toml: a [[notes.source]] needs a name other than notes, a path, "
                            f"and a kind of text or transcripts: {entry}")
        pattern = entry.get("glob", _KINDS[kind])
        if not isinstance(pattern, str) or not pattern or Path(pattern).is_absolute():
            raise ToolError(f"workspace.toml: the glob of a [[notes.source]] is a pattern inside its path, "
                            f"not empty or absolute: {entry}")
        root = Path(path).expanduser()
        found.append({"name": name, "kind": kind, "root": root if root.is_absolute() else home() / root,
                      "glob": pattern})
    return found


def state_of(item: noteslib.Item) -> str:
    """What a row says it is. A hand-edited guess in Current still doesn't read as current."""
    if item.retracted_by:
        return "retracted"
    if item.kind == "archive":
        return "archive, unreviewed"
    if item.section == "Current":
        return "current" if item.kind in ("seen", "user", "inferred") else "open"
    return "open" if item.section == "Open" else "history"


def _note_rows(path: Path) -> list[tuple]:
    topic = str(path.relative_to(notes()).with_suffix(""))
    rows = []
    for item in noteslib.parse(path.read_text(errors="replace")).items:
        state = state_of(item)
        label = " ".join(word for word in (item.id, f"[{item.tag}]" if item.kind else "") if word)
        rows.append((_RANK[state], state, label, f"{topic}:{item.start + 1}", item.text or item.words))
    return rows


def _clean(text: str) -> str:
    """Text SQLite can store: a lone surrogate, which JSON escapes can make (\\ud83d cut in half), becomes U+FFFD."""
    return text.encode("utf-8", "surrogatepass").decode("utf-8", "replace")


def _chunks(text: str):
    """Pieces of at most _CHUNK characters, cut where there is whitespace so no word is split. A word
    longer than a chunk is cut hard, and is then no word anyone could search for whole."""
    while text:
        if len(text) <= _CHUNK:
            yield text
            return
        window = text[:_CHUNK + 1]  # one more, so a space right at the end is a cut too
        cut = max(window.rfind(space) for space in " \t\n")
        cut = cut if cut > 0 else _CHUNK
        yield text[:cut]
        text = text[cut:].lstrip()


def _text_rows(path: Path) -> list[tuple]:
    rows, paragraph, first = [], [], 1
    def flush():
        for chunk in _chunks(" ".join(paragraph)):
            rows.append((_ARCHIVE_SOURCE_RANK, "archive, unreviewed", "", f"{path}:{first}", chunk))
        paragraph.clear()

    with open(path, errors="replace") as f:
        for number, line in enumerate(f, 1):
            if not line.strip() or line.startswith("#"):
                flush()
            if line.strip():
                if not paragraph:
                    first = number
                paragraph.append(line.strip())
    flush()
    return rows


def _message_texts(content) -> list[str]:
    """Words in a message: what was said, what a tool was asked, what it answered."""
    if isinstance(content, str):
        return [content]
    words: list[str] = []
    for block in content if isinstance(content, list) else []:
        if isinstance(block, str):
            words.append(block)
        elif isinstance(block, dict) and block.get("type") not in _SKIP_BLOCKS:
            if block.get("type") == "tool_use":
                words.append(json.dumps(block.get("input", ""), ensure_ascii=False))
            elif "content" in block:
                words += _message_texts(block["content"])
            elif isinstance(block.get("text"), str):
                words.append(block["text"])
    return words


def _transcript_rows(path: Path) -> list[tuple]:
    """One line per message. A line that isn't JSON is kept as it is, and metadata lines aren't."""
    rows = []
    with open(path, errors="replace") as f:
        for number, line in enumerate(f, 1):
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                words = [line]
            else:
                if not isinstance(entry, dict):
                    continue
                message = entry.get("message")
                words = _message_texts(message.get("content")) if isinstance(message, dict) else []
                if isinstance(entry.get("summary"), str):
                    words.append(entry["summary"])
            text = " ".join(" ".join(words).split())
            for chunk in _chunks(text):
                rows.append((_ARCHIVE_SOURCE_RANK, "archive, unreviewed", "", f"{path}:{number}", chunk))
    return rows


def _walk(warn) -> list[tuple[str, Path, callable]]:
    """Every file once, under the first name that reaches it: the notes, then the sources in order.
    A source may cover the workspace, which holds notes/, or sit inside another one."""
    files = [("notes", path, _note_rows) for path in noteslib.topics()]
    for source in sources():
        if not source["root"].is_dir():
            warn(f"{source['name']}: {source['root']} isn't there, so it wasn't searched")
            continue
        read = _text_rows if source["kind"] == "text" else _transcript_rows
        files += [(source["name"], path, read) for path in sorted(source["root"].glob(source["glob"]))]
    once, found = set(), []
    for source, path, read in files:
        key = path.resolve()
        if key not in once:
            once.add(key)
            found.append((source, path, read))
    return found


def _open(sqlite3) -> "sqlite3.Connection":
    path = cache() / "index.db"
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    os.close(fd)
    db = sqlite3.connect(path)
    if db.execute("PRAGMA user_version").fetchone()[0] != _SCHEMA:
        db.executescript("""
            DROP TABLE IF EXISTS fts; DROP TABLE IF EXISTS rows; DROP TABLE IF EXISTS files;
            CREATE TABLE files(id INTEGER PRIMARY KEY, path TEXT UNIQUE, source TEXT, mtime INTEGER, size INTEGER);
            CREATE TABLE rows(id INTEGER PRIMARY KEY, file INTEGER, rank INTEGER, state TEXT, label TEXT,
                              place TEXT, text TEXT);
            CREATE INDEX rows_file ON rows(file);
            CREATE VIRTUAL TABLE fts USING fts5(text, content='rows', content_rowid='id',
                                                tokenize='porter unicode61 remove_diacritics 2');
        """)
        db.execute(f"PRAGMA user_version = {_SCHEMA}")
    return db


def _forget(db, file_id: int) -> None:
    db.execute("INSERT INTO fts(fts, rowid, text) SELECT 'delete', id, text FROM rows WHERE file = ?", (file_id,))
    db.execute("DELETE FROM rows WHERE file = ?", (file_id,))
    db.execute("DELETE FROM files WHERE id = ?", (file_id,))


def _refresh(db, warn) -> None:
    """Bring the index up to date: only what changed is read again, and what's gone goes."""
    known = {path: (fid, source, mtime, size)
             for fid, path, source, mtime, size in db.execute("SELECT id, path, source, mtime, size FROM files")}
    seen = set()
    for source, path, read in _walk(warn):
        try:
            info = path.stat()
            if not stat.S_ISREG(info.st_mode):  # never open a FIFO: it would block
                continue
            name = _clean(str(path))
            seen.add(name)
            old = known.get(name)
            if old and old[1:] == (source, info.st_mtime_ns, info.st_size):
                continue
            rows = [tuple(_clean(v) if isinstance(v, str) else v for v in row) for row in read(path)]
        except (OSError, ValueError, RecursionError) as e:  # one file's trouble is that file's warning
            why = e.strerror if isinstance(e, OSError) and e.strerror else type(e).__name__
            warn(f"{source}: {path} couldn't be read, so it wasn't searched: {why}")
            continue
        with db:
            if old:
                _forget(db, old[0])
            fid = db.execute("INSERT INTO files(path, source, mtime, size) VALUES (?, ?, ?, ?)",
                             (name, source, info.st_mtime_ns, info.st_size)).lastrowid
            for row in rows:
                rid = db.execute("INSERT INTO rows(file, rank, state, label, place, text) VALUES (?, ?, ?, ?, ?, ?)",
                                 (fid, *row)).lastrowid
                db.execute("INSERT INTO fts(rowid, text) VALUES (?, ?)", (rid, row[4]))
    with db:
        for path, (fid, *_) in known.items():
            if path not in seen:
                _forget(db, fid)


def _query(words: list[str]) -> str:
    """Every word must appear, taken as words: nothing a person types is FTS syntax."""
    usable = [word for word in words if re.search(r"\w", word)]
    if not usable:
        raise ToolError("no words to look for")
    return " ".join('"' + word.replace('"', '""') + '"' for word in usable)


def find(words: list[str], limit: int, warn) -> tuple[list[dict], int]:
    """Matching rows, best state first, then how many there were in all."""
    query = _query(words)
    sqlite3 = _sqlite3()
    place = cache()
    db = None
    for attempt in (1, 2):
        try:
            with locked(place / ".lock"):
                db = _open(sqlite3)
                _refresh(db, warn)
            total = db.execute("SELECT count(*) FROM fts WHERE fts MATCH ?", (query,)).fetchone()[0]
            found = db.execute("""
                SELECT r.state, r.label, r.place, r.rank, r.text, snippet(fts, 0, '', '', ' ... ', 40)
                FROM fts JOIN rows r ON r.id = fts.rowid WHERE fts MATCH ?
                ORDER BY r.rank, bm25(fts), r.id LIMIT ?""", (query, limit)).fetchall()
            break
        except sqlite3.DatabaseError as e:  # regenerable: a damaged index is rebuilt once
            if isinstance(e, (sqlite3.OperationalError, sqlite3.IntegrityError)):
                # busy or full, or a constraint: not damage, and a rebuild would pull the file out from under
                # whoever has it open
                raise ToolError(f"the search index at {place} can't be used just now, and it wasn't touched: {e}") from None
            if db is not None:
                db.close()
            if attempt == 2:
                raise ToolError(f"the search index at {place} can't be used: {e}") from None
            warn("the search index was damaged, so it's being rebuilt")
            for leftover in place.glob("index.db*"):
                leftover.unlink()
    db.close()
    rows = [{"state": state, "label": label, "where": where,
             "text": " ".join((snippet if rank == _ARCHIVE_SOURCE_RANK else text).split())}
            for state, label, where, rank, text, snippet in found]
    return rows, total
