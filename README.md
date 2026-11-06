# GlassineOS/workspace

The layout agents and people work in: main checkouts of the organization's repositories next
to each other, and `work/<task>/<repo>` worktrees for anything being changed. `ws` manages
them, `workspace.toml` lists the repositories, `tools/` holds the tools for artifacts, dumps, notes, the device,
reproducers, benchmarks and trying a build, and `CLAUDE.md` holds the rules.

`repro/` and `bench/` hold reproducers and benchmarks as code, one folder each with its `repro.toml` or `bench.toml`.
They're tracked here, and their results never are: `tools/repro` and `tools/bench` store them in `artifacts/`, which
stays on the host, by id. A benchmark's result names the kit it ran on by its Kit ID, read from the device.

```sh
git clone git@github.com:GlassineOS/workspace.git ~/GlassineOS
cd ~/GlassineOS && git submodule update --init && ./ws sync
```

`ws` and the tools need Python 3.11 and git. Run the tests with `make check`.
