# Command reference

`--project` defaults to `.` on every subcommand that takes it, same as
`cargo build` -- omit it entirely when run from inside the target crate.

## `run`

Builds (if the graph/doc index are missing or stale) and serves the viewer,
for the crate in the current directory:

```sh
cargo codemap run
```

Pointed at another crate on disk instead:

```sh
cargo codemap run --project /path/to/target-crate
```

Optional flags:

```sh
cargo codemap run --port 8080
```

```sh
cargo codemap run --no-browser
```

`run` builds only if the existing output is missing or looks stale (any
`.rs` file under a dependency-closure member's own `src/` newer than the
existing `graph.json`/`source_index.json`), then serves — so a plain `run`
is also the "just reopen what I already built" case.

## `build`

Always regenerates the graph + doc index, unconditionally, together
(they're never meaningfully used apart — the doc index's own
cross-referencing needs a `graph.json` to resolve method nodes against):

```sh
cargo codemap build
```

```sh
cargo codemap build --project /path/to/target-crate
```

Force this instead of relying on `run`'s own missing-or-stale check when
you've touched something the staleness check doesn't look at (e.g. a
dependency outside the target crate's own `src/`).

Private (non-`pub`) items are always documented
(`--document-private-items` is unconditional, not a flag) — a private
`#[instrument]`'d function needs a doc-index entry to resolve during
replay (see [tracing-format.md](tracing-format.md)); this is a real cost
that scales with how many private items exist, on top of the
type-checking `cargo doc` already does either way, but turning it off
silently breaks replay for exactly the functions someone is most likely to
want to see.

## `validate-trace`

Checks a trace.jsonl against the schema in `schema/` — useful on your own
trace to confirm it actually matches the format in
[tracing-format.md](tracing-format.md):

```sh
cargo codemap validate-trace /path/to/trace.jsonl
```

## Output location

Output always lands at `<target crate>/target/cargo-codemap/<crate
name>/...` (next to cargo's own build output) — there's no flag to
redirect it. Run any subcommand with `--help` for details:

```sh
cargo codemap run --help
```

## Maintainer-only checks (not for your own project)

The MIR-format canary is a `cargo test`, run from this repo — builds the
graph for the in-repo fixture (`examples/dummy-cli`) and asserts a fixed
set of facts about it, specifically to catch a future Rust toolchain
upgrade silently changing MIR's text format:

```sh
cargo test
```
