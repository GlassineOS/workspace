# CLAUDE.md: the GlassineOS workspace

Start every session in this directory. Its memory and these rules go with this path. Each
repository's own `CLAUDE.md` loads when you read its files, and applies inside it.

## Where things are

| Path | What | Rule |
|---|---|---|
| `<repo>/` | main checkout (`.github` lives in `github/`) | Stays on its default branch and clean. Read it, don't work in it. |
| `work/<task>/<repo>/` | your worktrees | All edits, builds and commits happen here. |
| `~/.cache/glassine/` | build output, kernel objects, downloads | Not in a repository. |

Lab notes, firmware and dumps live outside this tree. Use `--add-dir`
when a task needs them, and never copy any of it into a repository here.

## Working

```sh
./ws status                       # what's dirty, unpushed, and which task holds what
./ws sync REPO                    # clone or fetch a main checkout
./ws new TASK REPO [REPO...]      # worktrees on branch TASK; --branch to continue one
./ws done TASK                    # remove them; refuses while anything isn't on GitHub
```

- One task, one folder, however many repositories it spans. Use the same branch name in each,
  and link the pull requests to each other.
- Before starting, run `./ws status`: another session's task is its own, don't touch it.
- A repository being worked on elsewhere may not have moved here yet: see `workspace.toml`
  and what `./ws status` shows, and ask if unsure.
- The hub runs from `hub/`, so changing that checkout changes the running service.
  Hub sessions get `work/<session>/<repo>` on `agent/<session>`, or the pull request's branch.

Organization guidance for coding agents (run `git submodule update --init` if the import
below is missing):

@.glassine/org/AGENTS.md
