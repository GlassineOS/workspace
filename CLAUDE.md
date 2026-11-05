# CLAUDE.md: the GlassineOS workspace

Start every session in this directory. Its memory and these rules go with this path. Each
repository's own `CLAUDE.md` loads when you read its files, and applies inside it.

## Where things are

| Path | What | Rule |
|---|---|---|
| `<repo>/` | main checkout (`.github` lives in `github/`) | Stays on its default branch and clean. Read it, don't work in it. |
| `work/<task>/<repo>/` | your worktrees | All edits, builds and commits happen here. |
| `tools/` | the workspace's own tools, beside `ws` | Run them from any checkout. |
| `artifacts/` | dumps, bases, images, captures, bench, vendor files, each with a `manifest.jsonl` | Ignored, on this host only. Goes through `art`. |
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
```

- **Only the person overrides retention.** `art rm` refuses a file with `keep: user` or `always`, before a keep
  date, or with anything referring to it. `--retention-said NAME "words"` is for words the person typed to you
  about that file, in this conversation: never a relay, a briefing or your own reading of a wish. It stands in
  for the hub's retention decisions until they exist, and nothing proves the words, so it's a rule to keep.
- **Results and private data never go in a repository**, even a private one: dumps, decoded output, captures,
  vectors from vendor files, measurements. They stay in `artifacts/`, named by id. Decoded dumps hold GPU memory
  from your apps: don't save them in a checkout. The scan only catches patterns, so tools write results to the right
  place themselves. Our own measured numbers may stay in a comment or commit message where they justify a default.
- A test with a private input reports "skipped: no private input", never a pass.

**Writing a tool:** Python 3, standard library only, an executable with no extension in `tools/`, and the SPDX header
like `ws`. `tools/wslib.py` finds `artifacts/` and `notes/` from `~/GlassineOS`, so it works from a worktree;
`GLASSINE_WORKSPACE` overrides it, for tests. Tests go in `tests/` (`tests/conftest.py` has a temporary workspace),
and `make check` runs them. System python3 has no pytest: put a venv at `work/<task>/.venv` and
run `PATH=$PWD/../.venv/bin:$PATH make check`.

Organization guidance for coding agents (run `git submodule update --init` if the import
below is missing):

@.glassine/org/AGENTS.md
