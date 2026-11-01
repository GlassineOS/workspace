# GlassineOS/workspace

The layout agents and people work in: main checkouts of the organization's repositories next
to each other, and `work/<task>/<repo>` worktrees for anything being changed. `ws` manages
them, `workspace.toml` lists the repositories, and `CLAUDE.md` holds the rules.

```sh
git clone git@github.com:GlassineOS/workspace.git ~/GlassineOS
cd ~/GlassineOS && git submodule update --init && ./ws sync
```

`ws` needs Python 3.11 and git. Run the tests with `make check`.
