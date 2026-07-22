# rust-codemap

An interactive call-graph viewer for Rust codebases, with execution replay
from real `tracing` logs.

Point it at any Rust binary crate and it will:

1. Extract the call-graph (who calls whom) directly from the compiler's MIR
   output — no source parsing, no per-project configuration, no mapping of
   module or type names to maintain.
2. Let you replay a real run of that binary, if it was instrumented with
   [`tracing`](https://docs.rs/tracing) spans, by loading its log output and
   stepping/animating through the call graph in the order things actually
   happened, with per-call duration.
3. Cross-reference the graph with `cargo doc` output, so a function's
   signature, doc comment, and source location are one click away, and type
   names inside a signature link to that type's own doc.

The only thing required from the target crate is that it produces log output
in the format described in [Tracing log format](#tracing-log-format) — the
tool has no other knowledge of, or dependency on, the codebase it's pointed
at.

This tool is a generalization of a prototype originally built inside a
sibling project; see [PROJECT.md](PROJECT.md) for the full design history,
what's settled, and what's still an open decision.

## Requirements

- Rust + Cargo (stable toolchain — `--emit=mir` and `cargo doc` both work on
  stable, no nightly features needed).
- Python 3.8+. The tool uses only the standard library today (see
  [requirements.txt](requirements.txt) — currently empty on purpose), so
  `pip install -r requirements.txt` has nothing to fetch, but it's there for
  the day a dependency is actually needed.
- A browser, to open the viewer.

## Repository layout

```
rust-codemap/
├── codemap/            # the tool itself — a Python package
│   ├── __main__.py      # CLI entry point (python -m codemap ...)
│   ├── mir_graph.py       # MIR text -> call-graph
│   ├── doc_index.py        # cargo doc HTML -> source_index.json
│   └── trace_log.py         # tracing JSON-lines -> replayable spans
├── viewer/              # the browser UI (Cytoscape.js), served as static files
│   └── index.html
├── requirements.txt
├── README.md            # this file
└── PROJECT.md           # design notes: current state, open decisions, TODOs
```

Nothing here refers to any specific target project. `viewer/` contains
**only** the tool's own static UI (`index.html`) — the HTTP server never
serves anything project-specific, and nothing generated is ever written
into this repo. `graph`/`doc`/`trace` write their output under
`<target crate>/target/rust-codemap/` (next to cargo's own build output),
and the viewer picks that data up via its **"Load graph…" / "Load doc
index…" / "Load trace…"** toolbar buttons — plain file pickers, reading
straight off disk, no server-side path coordination needed.

## Quick start

The fastest way to get going, run from this repo's root:

```sh
python -m codemap run --project /path/to/target-crate --bin <bin-name>
# or, to analyze a library crate instead of a binary:
python -m codemap run --project /path/to/target-crate --lib
```

This compiles the target crate, extracts the call-graph, cross-references it
with `cargo doc`, writes both under `<target-crate>/target/rust-codemap/`,
starts the viewer, and opens it in a browser. The command's own output
prints the exact file paths — in the viewer toolbar, click **"Load
graph…"** and pick `graph.json` (and **"Load doc index…"** for
`source_index.json`) from there.

`--bin <name>` and `--lib` are mutually exclusive: pick whichever target of
the crate you want the call-graph for. A library has no `fn main`, so the
graph won't have a single obvious entry point (the viewer's layout falls
back to auto-detecting roots) — see [Known limitations](#known-limitations)
for what's not yet supported on libraries (namely, execution replay).

### Step by step

Useful when iterating (e.g. regenerating just the graph after a code
change, without restarting the server):

```sh
python -m codemap graph --project /path/to/target-crate --bin <bin-name>  # or --lib
python -m codemap doc   --project /path/to/target-crate
python -m codemap serve
```

`graph` and `doc` share the same default output directory
(`<target-crate>/target/rust-codemap/`) unless you pass `--out`/`--graph`
explicitly. `serve` only ever serves `viewer/index.html` — use "Load
graph…" / "Load doc index…" in the already-running page to pick up
whatever you just (re)generated.

### Replaying a real execution

> Currently exercised against binaries only — the replay animation unwinds
> back to a node named `main` at the end of a trace, which only a binary's
> entry point is guaranteed to have. Not yet adapted for library crates;
> see PROJECT.md.

Run the target binary once (it must emit logs per the format below, to some
file, e.g. `trace_output.jsonl`), then use **"Load trace…"** in the viewer
to pick that file directly — parsed client-side, no CLI step needed. To
keep a parsed copy on disk instead: `python -m codemap trace --input
/path/to/trace_output.jsonl --project /path/to/target-crate` (same default
directory as `graph`/`doc`; omit `--project` to just write `trace.json` in
the current directory).

Either way, hit **Play** (or **Step >**) in the viewer to replay the run.

## Tracing log format

> **Status: partially settled.** This is the format the tool currently
> parses correctly (both `codemap trace` and the viewer's client-side
> parser implement exactly this). It has not yet been written up as a
> formal, versioned spec — see [PROJECT.md](PROJECT.md) for what's still
> open (root-span requirements, uniqueness of span names, field-naming
> guarantees). Treat this section as "what works today", not a final
> contract.

The target binary must write one JSON object per line (JSON Lines), which is
exactly what
[`tracing_subscriber`](https://docs.rs/tracing-subscriber)'s JSON formatter
produces out of the box:

```rust
tracing_subscriber::fmt()
    .json()
    .with_span_events(tracing_subscriber::fmt::format::FmtSpan::NEW | tracing_subscriber::fmt::format::FmtSpan::CLOSE)
    .init();
```

Each line is either a span **entry** event or a span **close** event:

- **Entry**: `{"span": {"name": "my_span", ...fields}, "spans": [ {"name": "caller"}, ... ]}`
  — `span.name` identifies the call; `spans` is the ordered list of
  enclosing spans (the call stack at that point, root first).
- **Close**: same shape, but `fields` additionally contains `"time.busy"`
  (a duration string like `"1.23ms"`, `"450µs"`, `"2.1s"`) — this is where
  per-call duration and iteration counts come from.

Known current limitation: spans are deduplicated **by name**. If the same
function is called from genuinely different call sites, they currently
collapse into a single graph node/trace entry rather than being
distinguished — acceptable for a first pass, called out here so it isn't a
surprise.

## Command reference

```
python -m codemap run   --project <path> (--bin <name> | --lib) [--port 8787] [--no-browser]
python -m codemap graph --project <path> (--bin <name> | --lib) [--out <path>]
python -m codemap doc   --project <path> [--graph <path>] [--out <path>]
python -m codemap trace --input <path-to-log> [--project <path>] [--out <path>]
python -m codemap serve [--dir viewer] [--port 8787]
```

`--out`/`--graph` default to `<target crate>/target/rust-codemap/...` — see
above. Run any subcommand with `--help` for details.

## How the call-graph is built

`codemap/mir_graph.py` parses the text output of `cargo rustc --bin <name>
-- --emit=mir` with regular expressions — no AST, no type-checker, no
external tool beyond `rustc` itself. It handles a few cases a naive "grep
for calls" would miss:

- **Dynamic dispatch** (`&dyn Trait` calls): over-approximated by linking to
  *every* local implementation of that trait method, since MIR alone can't
  resolve which concrete type is behind the pointer at that call site.
- **Closures**: MIR turns a closure into its own top-level item. Calls made
  inside a closure body (e.g. inside `.map(...)`) are re-attributed to the
  function that owns the closure, so they still show up as real edges.
- **Noise filtering**: derived/trait-machinery methods (`fmt`, `clone`,
  `eq`, `hash`, ...) and std/core/alloc/serde/tracing-internal calls are
  excluded. This list is generic (applies to any crate using those common
  ecosystem pieces), not project-specific — but it's a starting point, not
  guaranteed exhaustive for every macro-heavy dependency out there.
- **Crate scoping**: a module-qualified item is considered part of the
  target crate unless its embedded source path resolves through cargo's
  dependency or toolchain caches (`.cargo`, `registry`, `.rustup`,
  `toolchains`). This means the tool has no list of "known good" module
  names to keep in sync — it also means vendored dependencies (via
  `cargo vendor`) are currently *not* excluded, since their path doesn't
  look external. Not yet handled; see PROJECT.md.
- **"traced" flag**: each node records whether `#[instrument]`/`span!`/
  `event!` actually expanded into it (detected via the tracing crate's
  `Callsite` scaffolding appearing in its MIR body) — a purely structural
  signal, independent of any particular run, so it isn't fooled by "this
  input just didn't take that branch".

## Known limitations

See [PROJECT.md](PROJECT.md) for the full list and the reasoning behind
each. In short, today:

- Regex-based MIR parsing has only been exercised against small,
  single-crate, synchronous examples — generics, async fns, multi-crate
  workspaces, and deeply chained iterator/closure code are untested.
- The static call-graph (`run`/`graph`/`doc`) works for both binaries
  (`--bin`) and libraries (`--lib`). Execution replay does not yet: it
  assumes a trace's root span is named `main`, which only a binary is
  guaranteed to have.
- All call edges are typed generically as `call` — the distinction between
  a direct call, a dynamic dispatch, and a loop are not yet recovered from
  MIR (the viewer already supports styling `dispatch`/`loop_call`/
  `trampoline` differently if the generator is later extended to emit them).
- There's no dedicated "call stack + timing" frame yet beyond the sidebar's
  flat execution-trace list, and the documentation frame doesn't yet offer
  full rustdoc-style navigation (it shows scraped signature/doc snippets
  inline instead) — both are open design decisions, not bugs.

## License

See [LICENSE](LICENSE).
