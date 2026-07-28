# cargo-codemap

An interactive call-graph viewer for Rust codebases, with execution replay
from real `tracing` logs.

Point it at any Rust crate (binary or library) and it will:

1. Extract the call-graph (who calls whom) directly from the compiler's MIR
   output — no source parsing, no per-project configuration, no mapping of
   module or type names to maintain. If the crate depends on other local
   (path) crates, their graphs are merged in automatically too.
2. Let you replay a real run of that binary, if it was instrumented with
   [`tracing`](https://docs.rs/tracing) spans, by loading its log output and
   stepping/animating through the call graph in the order things actually
   happened, with per-call duration.
3. Cross-reference the graph with `cargo doc` output, so a function's
   signature, doc comment, and source location are one click away, and type
   names inside a signature link to that type's own doc.

## Prerequisites

- Rust + Cargo (stable toolchain — `--emit=mir` and `cargo doc` both work on
  stable, no nightly features needed).
- A browser, to open the viewer.

## Getting started

### 1. Install

```sh
cargo install --path .
```

Puts `cargo-codemap` on your `PATH` via `~/.cargo/bin` (already there from
a normal rustup setup).

**If you're developing `cargo-codemap` itself** (not just using it): this
installed copy is a frozen snapshot, not kept in sync with your checkout.
`cargo codemap ...` keeps running the old build until you `cargo install
--path .` again — after any change to `src/`, re-run the install command
above before testing via `cargo codemap`, or use `cargo run -- <subcommand>`
instead, which always reflects the current checkout.

### 2. Run

From inside the target crate (`--project` defaults to `.`, same as
`cargo build`):

```sh
cargo codemap run
```

Builds the call-graph, cross-references it with `cargo doc`, and opens the
viewer in a browser with both already loaded. No target crate handy?
`examples/` has a small self-contained one — see
[examples/README.md](examples/README.md). Every flag and subcommand:
[doc/commands.md](doc/commands.md).

### 3. Load a trace

Click **"Load trace…"** in the viewer toolbar and pick a trace file, to
replay a real execution instead of just browsing the static graph. Your
binary needs to emit that file in the right format first — see
[doc/tracing-format.md](doc/tracing-format.md).

### 4. Navigate

Click nodes, focus/expand neighborhoods, step through a loaded trace — see
[doc/viewer-guide.md](doc/viewer-guide.md) for everything the viewer can
do.

## Repository layout

```
cargo-codemap/
├── .claude/
│   └── CLAUDE.md         # guidance for Claude Code when working in this repo
├── doc/                  # reference docs (see "Documentation" below)
├── examples/             # a small, self-contained target crate to try the tool against
│   ├── dummy-core/
│   ├── dummy-api/
│   └── dummy-cli/
├── src/                  # the tool itself
│   ├── main.rs           # CLI entry point + HTTP server (`cargo codemap <subcommand>`)
│   ├── mir_graph.rs      # MIR text -> call-graph
│   ├── doc_index.rs      # cargo doc HTML -> source_index.json
│   ├── trace_log.rs      # tracing JSON-lines -> replayable spans
│   └── schema_check.rs   # trace-format JSON Schema validator
├── schema/               # trace-format JSON Schema
├── viewer/               # the browser UI (Cytoscape.js), served as static files
│   └── index.html
├── Cargo.toml
├── README.md             # this file
└── LICENSE
```

## Documentation

- [Command reference](doc/commands.md) — every subcommand and flag.
- [Using the viewer](doc/viewer-guide.md) — replaying a trace, doc-driven
  graph focus, navigating a large graph by hand.
- [Tracing log format](doc/tracing-format.md) — the setup to add to your
  `main()`, common `#[instrument]` variations, the full format contract,
  and how a span is matched to a graph node.
- [How it works](doc/architecture.md) — multi-crate merging, how the
  call-graph is actually built from MIR.
- [Known limitations](doc/limitations.md).

## License

See [LICENSE](LICENSE).
