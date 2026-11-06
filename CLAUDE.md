# CLAUDE.md: the GlassineOS workspace

Start every session in this directory. Its memory and these rules go with this path. Each
repository's own `CLAUDE.md` loads when you read its files, and applies inside it.

## Where things are

| Path | What | Rule |
|---|---|---|
| `<repo>/` | main checkout (`.github` lives in `github/`) | Stays on its default branch and clean. Read it, don't work in it. |
| `work/<task>/<repo>/` | your worktrees | All edits, builds and commits happen here. |
| `tools/` | the workspace's own tools, beside `ws` | Run them from any checkout. |
| `repro/`, `bench/` | reproducers and benchmarks, each a folder with a `repro.toml` or `bench.toml` | Code only: results go to `artifacts/`. |
| `artifacts/` | dumps, bases, images, captures, bench, repro results, vendor files, each with a `manifest.jsonl` | Ignored, on this host only. Goes through `art`. |
| `notes/` | what's known, per topic: `notes/<area>/<topic>.md`, its own local git with no remote | Ignored, on this host only. Goes through `notes`, or by hand with its pre-commit hook. |
| `~/.cache/glassine/` | build output, kernel objects, downloads: what can be rebuilt | Anything may delete it. Nothing may point into it. |

Lab notes and firmware still live outside this tree, in the lab directory, until they're indexed.
Use `--add-dir` when a task needs them, and never copy any of it into a repository here.

## Working

```sh
./ws status                       # what's dirty, unpushed, and which task holds what
./ws sync REPO                    # clone or fetch a main checkout
./ws new TASK REPO [REPO...]      # worktrees on branch TASK; --branch to continue one
./ws done TASK                    # remove them; refuses while anything isn't on GitHub, or past
                                  # 1 MiB of ignored files (--drop-ignored if they can all go)
```

- One task, one folder, however many repositories it spans. Use the same branch name in each,
  and link the pull requests to each other.
- Before starting, run `./ws status`: another session's task is its own, don't touch it.
- A repository being worked on elsewhere may not have moved here yet: see `workspace.toml`
  and what `./ws status` shows, and ask if unsure.
- The hub runs from `hub/`, so changing that checkout changes the running service.
  Hub sessions get `work/<session>/<repo>` on `agent/<session>`, or the pull request's branch.

`ws new` doesn't know this repository yet. For a worktree of it:
`git fetch origin && git worktree add work/<task>/workspace -b <task> origin/main`.

## Tools

`tools/<name> --help` has the details.

```sh
tools/art add FILE --to FOLDER --class C --what WORDS --keep K   # --in-place to index without moving
tools/art find WORDS                                             # --folder, --class, --expired, --all
tools/art path NAME                                              # tools and tests ask this, never a path
tools/art verify                                                 # hash files against the manifests
tools/art rm NAME --why WORDS                                    # --dry-run shows what holds it
tools/dumps add FILE --mesa V --profile P --ran WORDS            # a GPU fault: save it within 5 minutes
tools/dumps add-ram IMAGE... --build NAME --kernel-log F --until DATE --ran WORDS
tools/dumps show ID                                              # the decode, to stdout only
tools/dumps group                                                # by fingerprint; it names no cause
tools/notes add AREA/TOPIC WORDS [--guess ...] [--open]          # a Log line as things happen, or an Open item
tools/notes set AREA/TOPIC WORDS --seen "base=ID, art=NAME"      # a claim in Current; --replaces cN, or --open
tools/notes set AREA/TOPIC --retract cN --by cM --why WORDS      # --close cN for an Open item that's settled
tools/notes lint [AREA/TOPIC...]                                 # what may not stand
tools/notes find WORDS                                           # notes, then the sources in workspace.toml
tools/dev take --as NAME --purpose WORDS --expect 30m -- CMD     # the device lock: exit status only, CMD chained
tools/dev release --as NAME                                      # without CMD it's held until this
tools/dev wait --as NAME --on WORDS --until 2h                   # queue; tools/dev ready --as NAME when they're met
tools/dev status                                                 # asks the device; also notice, stamp, break
tools/repro run NAME --as NAME                                   # under the lock, --does from repro.toml, result in artifacts/repro
tools/repro gate BASE --patches DIR                              # what's missing or failed; runs nothing
tools/bench run NAME --as NAME                                   # interleaved arms, result in artifacts/bench
tools/bench eye --binary X --setting K=V --content W --said W    # an eye test is a result too
```

- **Only the person overrides retention.** `art rm` refuses a file with `keep: user` or `always`, before a keep
  date, or with anything referring to it. `--retention-said NAME "words"` is for words the person typed to you
  about that file, in this conversation: never a relay, a briefing or your own reading of a wish. It stands in
  for the hub's retention decisions until they exist, and nothing proves the words, so it's a rule to keep.
- **The device lock is `mkdir` on the device,** in plain files that also work by hand over ssh. Take it with `dev
  take`, never print the state and then proceed. `--does ACTIONS` needs a go: `--go ID` from the hub, or `--go-said`
  for words the person typed to you in this conversation, for your next lock. Never a relay, a briefing or restored
  history; compaction or Resume ends them. `dev break` is only on the person's word. Settings go in `dev.toml` in the
  workspace, which git ignores. Never read or glob the daemon's `/run` files: reading its FIFO blocks.
- **Results and private data never go in a repository**, even a private one: dumps, decoded output, captures,
  vectors from vendor files, measurements. They stay in `artifacts/`, named by id. Decoded dumps hold GPU memory
  from your apps: don't save them in a checkout. The scan only catches patterns, so tools write results to the right
  place themselves. Our own measured numbers may stay in a comment or commit message where they justify a default.
- **Notes say where each claim comes from:** `seen` (a base and a command or artifact), `inferred` (from which
  claims), `guess`, `user` or `archive`. Current takes only `seen`, `user` and `inferred` resting on Current, so a
  guess can't rise by being repeated. Nothing is deleted: what's replaced or retracted goes to Log, marked. `add` and
  `set` refuse the whole write on a lint problem or a scan flag. The scan catches patterns, not prose or decoded
  output, so what's private stays in `artifacts/`, named by id. Work others need is an issue, with no note text.
- **`notes find` rows say what they are:** `current`, `open`, `history`, `retracted` or `archive, unreviewed`. Read
  them. The index is in `~/.cache/glassine/notes-index`, and the sources it covers come from `[[notes.source]]` in
  `workspace.toml`: none are set here until the person says so.
- A test with a private input reports "skipped: no private input", never a pass.
- **A reproducer's `does` comes from its `repro.toml`,** so a run can't start under a go that doesn't cover what it
  does. Its result names the base the device stamped, and warns when that isn't the base you said, when the binary differs
  from earlier results there, or when something is bound over a live binary. A bound binary isn't the base's own.
- **One invalid arm invalidates a comparison,** and an invalid result prints no number. Don't dig its numbers out of the
  log to quote. A scroll run proves motion from counters `bench` reads itself. A device result names the kit that ran,
  by the Kit ID from `dev stamp` (the `kit_id` probe in `dev.toml`), not this checkout's commit, and without a Kit ID
  nothing runs. `bench eye --said` is for words the person typed to you, never a relay or your own reading of them.

**Writing a tool:** Python 3, standard library only, an executable with no extension in `tools/`, and the SPDX header
like `ws`. `tools/wslib.py` finds `artifacts/` and `notes/` from `~/GlassineOS`, so it works from a worktree;
`GLASSINE_WORKSPACE` overrides it, for tests. Tests go in `tests/` (`tests/conftest.py` has a temporary workspace),
and `make check` runs them. System python3 has no pytest: put a venv at `work/<task>/.venv` and
run `PATH=$PWD/../.venv/bin:$PATH make check`.

Organization guidance for coding agents (run `git submodule update --init` if the import
below is missing):

@.glassine/org/AGENTS.md
