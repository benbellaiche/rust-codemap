# rust-codemap

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
`<target crate>/target/rust-codemap/<crate name>/` (next to cargo's own
build output, nested under the crate's own name so that multiple crates in
one workspace don't overwrite each other's output at the same path), and
the viewer picks that data up via its **"Load graph…" / "Load doc
index…" / "Load trace…"** toolbar buttons — plain file pickers, reading
straight off disk, no server-side path coordination needed.

## Quick start

The fastest way to get going, run from this repo's root:

```sh
python -m codemap run --project /path/to/target-crate
```

This compiles the target crate (and every local crate it actually depends
on — see [Multi-crate merging](#multi-crate-merging)), extracts the
call-graph, cross-references it with `cargo doc`, writes both under
`<target-crate>/target/rust-codemap/<crate name>/`, starts the viewer, and
opens it in a browser. The command's own output prints the exact file
paths — in the viewer toolbar, click **"Load graph…"** and pick
`graph.json` (and **"Load doc index…"** for `source_index.json`) from
there.

No `--bin`/`--lib` to choose: the target can be either, and it doesn't
change how the graph is built (see below). A library has no `fn main`, so
its graph won't have a single obvious entry point (the viewer's layout
falls back to auto-detecting roots) — see
[Known limitations](#known-limitations) for what's not yet supported on
libraries (namely, execution replay).

### Step by step

Useful when iterating (e.g. regenerating just the graph after a code
change, without restarting the server):

```sh
python -m codemap graph --project /path/to/target-crate
python -m codemap doc   --project /path/to/target-crate
python -m codemap serve
```

`graph` and `doc` share the same default output directory
(`<target-crate>/target/rust-codemap/<crate name>/`) unless you pass
`--out`/`--graph` explicitly. `serve` only ever serves `viewer/index.html`
— use "Load
graph…" / "Load doc index…" in the already-running page to pick up
whatever you just (re)generated.

### Replaying a real execution

> Currently exercised against binaries only — the replay animation unwinds
> back to a node named `main` at the end of a trace, which only a binary's
> entry point is guaranteed to have. Not yet adapted for library crates;
> see PROJECT.md.

Run the target binary once (it must emit logs per the format below, to some
file, e.g. `trace_output.jsonl`), then use **"Load trace…"** in the viewer
to pick that file directly — no CLI step needed. If the viewer is running
under `codemap serve`/`run`, the raw log is parsed server-side (the same
`trace_log.py` the `trace` subcommand below uses); with no server to ask
(e.g. `index.html` opened straight off disk) it falls back to an equivalent
parser in the viewer itself. To keep a parsed copy on disk instead: `python
-m codemap trace --input /path/to/trace_output.jsonl --project
/path/to/target-crate` (same default directory as `graph`/`doc`; omit
`--project` to just write `trace.json` in the current directory).

Either way, hit **Play** (or **Step >**) in the viewer to replay the run.

### Doc-driven graph focus

A crate with thousands of functions renders as an unreadable wall of nodes
if you just dump the whole call-graph at once (see "Known limitations"). The
left-hand **"Public API (doc index)"** panel is the way around that: load a
`source_index.json` via **"Load doc index…"**, and it lists every
`cargo doc`-documented item, grouped by crate then by class/type (both come
straight from `doc_index.py`'s output — no new mapping: a method's class is
the `Type` half of its `Type::method` node id, a free fn has none, and a
type's own entry acts as its own class heading, which is why its methods
land right under it).

Clicking an entry does two things at once:

1. **Focuses the graph** on that function plus everything it calls,
   recursively (outgoing edges only — cytoscape's `successors()`, walked
   ourselves layer by layer so it can be capped). Set **"Max call depth
   (doc focus)"** in Settings (0 = unlimited) to bound it; if the cap
   actually cut off real callees, the info panel and status bar say so
   explicitly (`— truncated: ...`) rather than silently showing a
   smaller-looking graph. **"Show full graph"** clears the focus.
2. **Loads the native `cargo doc` HTML page** for that item in the frame
   below the list — the real styled page (fields, trait impls, examples),
   not just the signature/doc text also shown in the graph's info panel.
   This needs the doc HTML actually served: `codemap run` wires this up
   automatically; calling `serve` directly needs `--docs <target
   crate>/target/doc` (see "Command reference"). Without it, the frame
   just says so instead of showing a broken/blocked `file://` load.

An entry that isn't a node in the currently loaded graph (e.g. a struct's
own doc page — the type itself isn't a call-graph node, only its methods
are) still opens its native doc page, but can't focus the graph on it
(reported in the info panel, not a silent no-op).

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
python -m codemap run   --project <path> [--port 8787] [--no-browser]
python -m codemap graph --project <path> [--out <path>]
python -m codemap doc   --project <path> [--graph <path>] [--out <path>]
python -m codemap trace --input <path-to-log> [--project <path>] [--out <path>]
python -m codemap serve [--dir viewer] [--docs <path>] [--port 8787]
```

`--out`/`--graph` default to `<target crate>/target/rust-codemap/<crate name>/...` — see
above. Run any subcommand with `--help` for details.

`run` always passes its own `--docs` to `serve` automatically (pointing at
`<target crate>/target/doc/`, generated by the `doc` step it just ran) — see
"Doc-driven graph focus" below for what that's for. Calling `serve` directly
needs `--docs` spelled out if you want that too.

## Multi-crate merging

If the target crate depends on other crates that live locally (path
dependencies — e.g. sibling members of the same cargo workspace), their
call-graphs are merged in automatically: `graph`/`doc` resolve the target's
own transitive dependency graph via `cargo metadata`, compile each local
dependency with `RUSTFLAGS=--emit=mir`, and merge every one's MIR into a
single graph (and doc every one of them too, so cross-references resolve
correctly no matter which crate a function actually lives in).

This is deliberately the crate's own **dependency closure** — what it
actually depends on — not "every crate that happens to share its
workspace". A workspace can contain unrelated sibling projects, or a client
binary that depends *on* the target crate (rather than the other way
around); neither belongs in the target's own call-graph, and both are
correctly excluded by walking `cargo metadata`'s resolved dependency graph
outward from the target instead of just listing workspace members.

Known limitation: node ids aren't crate-qualified (a function is just
`name` or `Type::method`, see below), so if two different crates in the
closure happen to define the same free function name or the same
`Type::method` pair, they collide into a single graph node. Not yet solved
— see PROJECT.md.

## How the call-graph is built

`codemap/mir_graph.py` parses MIR text (produced via `cargo build` with
`RUSTFLAGS=--emit=mir`, see above) with regular expressions — no AST, no
type-checker, no external tool beyond `rustc` itself. It handles a few
cases a naive "grep for calls" would miss:

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

- Regex-based MIR parsing has been exercised against generics/
  monomorphization, recursion, nested/chained closures, chained iterator
  combinators, and `async fn` (a dedicated fixture, one function per shape)
  — two real bugs found and fixed (a generic call site's monomorphized
  turbofish, and recursion producing no self-edge); the rest already worked.
  Crate-root `impl` blocks, module-qualified `Self` types, and
  inconsistently-qualified free functions were bugs found earlier via the
  same kind of dedicated multi-crate test fixture and are also fixed. See
  PROJECT.md §3 for all of them. `cargo vendor`-based dependencies are
  handled too, though the specific failure mode this guards against
  (a vendored dependency's code being mistaken for the target crate's own)
  couldn't actually be reproduced on this toolchain to confirm it matters
  in practice — kept as a low-cost safeguard regardless.
- The static call-graph (`run`/`graph`/`doc`) works for both binaries and
  libraries, standalone or with local dependencies merged in. Execution
  replay does not yet: it assumes a trace's root span is named `main`,
  which only a binary is guaranteed to have.
- Cross-crate node-id collisions (see "Multi-crate merging" above) are
  detected-but-unresolved: two crates' same-named items silently merge into
  one graph node.
- All call edges are typed generically as `call` — the distinction between
  a direct call, a dynamic dispatch, and a loop are not yet recovered from
  MIR (the viewer already supports styling `dispatch`/`loop_call`/
  `trampoline` differently if the generator is later extended to emit them).
- There's no dedicated "call stack + timing" frame yet beyond the sidebar's
  flat execution-trace list. The documentation frame does now show the real
  rustdoc page (see "Doc-driven graph focus" above), alongside the scraped
  signature/doc snippets inline in the graph's own info panel — but
  navigating a link *inside* the rustdoc iframe doesn't sync the doc list's
  selection or the graph's focus back to wherever that link went.

## License

See [LICENSE](LICENSE).
