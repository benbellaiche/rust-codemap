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
serves anything project-specific by default, and nothing generated is
ever written into this repo. `graph`/`doc`/`trace` write their output
under `<target crate>/target/rust-codemap/<crate name>/` (next to cargo's
own build output, nested under the crate's own name so that multiple
crates in one workspace don't overwrite each other's output at the same
path). `codemap run` knows those exact paths (it just generated them) and
serves them at fixed URLs (`/graph.json`, `/source_index.json`) the viewer
auto-loads on page load — zero clicks. Calling `serve` on its own needs
the same paths spelled out via `--graph`/`--doc` (see "Command reference")
to have anything to show; there's no toolbar file picker as a fallback —
the graph and doc index always come from the server now. **"Load
trace…"** is the one thing that's still a manual toolbar button, since
which run you want to replay can change from one look at the same code to
the next (see "Replaying a real execution").

## Quick start

The fastest way to get going, run from this repo's root:

```sh
python -m codemap run --project /path/to/target-crate
```

This compiles the target crate (and every local crate it actually depends
on — see [Multi-crate merging](#multi-crate-merging)), extracts the
call-graph, cross-references it with `cargo doc`, writes both under
`<target-crate>/target/rust-codemap/<crate name>/`, starts the viewer, and
opens it in a browser — with the graph and doc index already loaded, no
clicks needed. Only **"Load trace…"** stays manual, since which run you
want to replay can change from one look at the same code to the next.

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
python -m codemap serve --graph /path/to/target-crate/target/rust-codemap/<crate name>/graph.json \
                         --doc /path/to/target-crate/target/rust-codemap/<crate name>/source_index.json
```

`graph` and `doc` share the same default output directory
(`<target-crate>/target/rust-codemap/<crate name>/`) unless you pass
`--out`/`--graph` explicitly (that flag on `doc` means something
different — see "Command reference"). Re-running just `graph`/`doc` after
a code change and then reloading the already-running `serve` page picks up
the new output automatically, no restart needed — `serve` re-reads
`--graph`/`--doc` from disk on every request, it doesn't cache them.

### Replaying a real execution

> Currently exercised against binaries only — the replay animation unwinds
> back to a node named `main` at the end of a trace, which only a binary's
> entry point is guaranteed to have. Not yet adapted for library crates;
> see PROJECT.md.

Run the target binary once (it must emit logs per the format below, to some
file, e.g. `trace_output.jsonl`), then use **"Load trace…"** in the viewer
to pick that file directly — no CLI step needed, no separate command to
convert it first. If the viewer is running under `codemap serve`/`run`,
the raw log is parsed server-side (`trace_log.py`, via a `/__codemap_
parse_trace` endpoint); with no server to ask (e.g. `index.html` opened
straight off disk) it falls back to an equivalent parser in the viewer
itself.

Hit **Play** (or **Step >**) in the viewer to replay the run.

### Doc-driven graph focus

A crate with thousands of functions renders as an unreadable wall of nodes
if you just dump the whole call-graph at once (see "Known limitations"). The
left-hand **"Public API (doc index)"** panel is the way around that: once
`source_index.json` is loaded (auto-loaded by `codemap run`, see above),
it lists every `cargo doc`-documented item, grouped by crate then by
class/type (both come
straight from `doc_index.py`'s output — no new mapping: a method's class is
the `Type` half of its `Type::method` node id, a free fn has none, and a
type's own entry acts as its own class heading, which is why its methods
land right under it).

Clicking an entry does two things at once, and it's fully symmetric —
clicking a node **in the graph** does the same pair of things in reverse:

1. **Pans/zooms the graph** to that node (a fixed, strong zoom level,
   consistent regardless of how many other functions call it) and
   highlights it (a soft halo, distinct from the untraced/visited/current
   colors so it never conflicts with them) until the next click or **"Show
   full graph"** (which now just resets the viewport — nothing is ever
   hidden by this).
2. **Updates the "Selected" panel** right below the doc list — the same
   function name/signature/source-link/doc-comment info a graph node's
   click already showed, now living in one place instead of two, plus an
   **"Open in new tab"** button that opens that item's real `cargo doc`
   HTML page (fields, trait impls, examples) in a full browser tab. This
   needs the doc HTML actually served: `codemap run` wires this up
   automatically; calling `serve` directly needs `--docs <target
   crate>/target/doc` (see "Command reference"). Without a matching
   `docPage`, the button just stays disabled instead of opening a broken
   link.

An entry that isn't a node in the currently loaded graph (e.g. a struct's
own doc page — the type itself isn't a call-graph node, only its methods
are) still opens its native doc page, but can't pan the graph to it
(reported in the info panel, alongside its signature and source-file link
— same info any other entry gets, not a silent no-op).

Private (non-`pub`) items only appear here at all if you generated with
`--include-private` (see "Command reference") — the **"Show private"**
button next to "Hide untraced" then reveals them (hidden by default, same
as untraced nodes); the button itself stays hidden if there's nothing for
it to affect. `main`'s own effective visibility is genuinely `pub(crate)`
(a binary has no external API), but it's exempted from this toggle — a
binary's entry point isn't "someone's private helper," it's always shown.

### Navigating a large graph by hand

Every edge carries a small number at its midpoint: that's `callOrder`, the
position of that call in its caller's own source (see "Command reference" /
`mir_graph.py` for how it's derived — it's a static, MIR-order
approximation, not a guarantee about real execution order for code with
branches or loops). This is the *static* navigation layer — it's a
deliberately separate thing from replaying a trace (below); loading one
suppresses all of the following so it doesn't fight with replay's own
visited/current colors — a loaded trace keeps that suppression active until
you do something about it. Two toolbar buttons hand control back:
**"Switch to static"** hides the replay overlay (and un-suppresses this
layer) without touching the trace itself — click **"Switch to replay"**
(same button, relabeled) to bring the exact same step right back, no
progress lost; stepping again (`Step >`/`Play`) also switches back to
replay automatically, since stepping only makes sense if you want to see
it. **"Unload trace"** is the one-way version — clears the trace
completely, for when you're done with that run rather than just looking
away from it for a moment.

Focusing a node (clicking it, a doc-list entry, an edge, or a number key)
dims everything outside its immediate neighborhood and colors its own edges
by direction — **orange** for what it calls, **green** for what calls it —
instead of leaving the whole graph at uniform brightness.

**Double-clicking** a node hides everything else and pulls its direct
neighbors into a circle around it, spaced so they never overlap regardless
of how many there are, then fits the view to exactly that — no forced
zoom-out or scrolling to go find a neighbor the layout happened to place
elsewhere, and nothing else left on screen to clutter the view. A single
click never moves or hides anything; double-click is a separate, deliberate
"show me just this neighborhood" action, so the fast click-through-edges/
number-key navigation below stays light. Only one neighborhood is expanded
at a time — double-clicking elsewhere, clicking empty canvas, or "Show full
graph" all restore the exact original positions, visibility, and view.

If one of that node's direct neighbors is normally hidden by "Hide
untraced," it's temporarily revealed for this one focused view (still with
its usual dashed styling, so it's clear it's untraced) — restoring puts it
back to hidden, so the global toggle's state everywhere else is
unaffected. A neighbor reached only *through* an untraced function (two
hops away, not a direct connection) still stays hidden — this only reveals
genuine first-degree relationships.

Three ways to move from a focused node to one of its neighbors without
touching the doc list:

- **Click any edge** — jumps to whichever end isn't the currently focused
  node (the target if it's an outgoing call, the source if incoming); with
  no node focused yet, or on an edge unrelated to the current focus, it
  just follows the arrow to its target.
- **Press a number key** (`1`–`9`) — jumps straight to the outgoing call
  with that `callOrder`, without needing to click a specific thin edge in a
  dense area. Only single digits: a function with a tenth-or-later call has
  no shortcut past 9. Ignored while typing in the doc-search box above.
- **Back / Forward** buttons under the doc list — every focus, however it
  was triggered (graph click, doc-list click, edge click, number key),
  is recorded; navigating to a new node from a "Back" position drops
  whatever was ahead of it, the same as a browser tab's history.

Clicking **empty canvas** clears the highlight/dim, deselects, and collapses
any expanded neighborhood — the same "back to normal" state as **"Show full
graph"**, but without also resetting zoom/pan, for when you just want to
stop highlighting without losing your place in the view.

## Tracing log format

> **Status: mandatory, not descriptive.** This is not "what happens to
> work today" — it's the one format this tool supports. Don't try to
> adapt your own logging setup to look similar; instead, add the exact
> snippet below (unmodified) to your binary's `main()`. A formal, versioned
> schema for this contract is proposed in PROJECT.md §4, but the shape
> below is already fixed and won't change out from under you before that
> lands.

Add this to your binary's `main()`, before anything else runs, unmodified:

```rust
tracing_subscriber::fmt()
    .json()
    .with_span_events(tracing_subscriber::fmt::format::FmtSpan::NEW | tracing_subscriber::fmt::format::FmtSpan::CLOSE)
    .with_file(true)
    .with_line_number(true)
    .init();
```

Then instrument whichever functions you want to see in the replay with
`#[tracing::instrument]` (or `#[instrument(name = "...")]` for a custom
label — the tool resolves by source location, not by this name, see below).
Run the binary, redirect its output to a file, and load that file with
**"Load trace…"** in the viewer.

`.with_file(true).with_line_number(true)` is not optional: without it,
spans can only be matched by name, which silently fails for every method
(see "How a span is matched to a graph node" below) and can't tell two
call sites of the same function apart at all. There is no supported
"basic" mode with a smaller log line — this is the one contract
`trace_log.py` and the viewer's fallback parser both parse.

The target binary must write one JSON object per line (JSON Lines), which is
exactly what the snippet above produces via
[`tracing_subscriber`](https://docs.rs/tracing-subscriber)'s JSON formatter.
Each line is either a span **entry** event or a span **close** event:

- **Entry**: `{"filename": "...", "line_number": N, "span": {"name": "my_span", ...fields}, "spans": [ {"name": "caller"}, ... ]}`
  — `span.name` identifies the call (see "How a span is matched to a graph
  node" below — its literal text isn't actually load-bearing); `spans` is
  the ordered list of enclosing spans (the call stack at that point, root
  first); `filename`/`line_number` are `#[instrument]`'s own source
  location and only appear with `.with_file(true).with_line_number(true)`
  set (recommended — see below for why).
- **Close**: same shape, but `fields` additionally contains `"time.busy"`
  (a duration string like `"1.23ms"`, `"450µs"`, `"2.1s"`) — this is where
  per-call duration and iteration counts come from. `time.busy` is the
  only duration this tool reads or displays anywhere — it's wall-clock
  time the span was actually entered (i.e. really executing, including
  any nested calls), not a CPU-usage metric: measured directly with a
  deliberate `std::thread::sleep()` inside an instrumented function, and
  confirmed the sleep shows up entirely in `time.busy`, not in the
  sibling `time.idle` field tracing also reports. `time.idle` (time the
  span existed but wasn't the active context) is not used anywhere in
  this tool — it's near-zero for ordinary synchronous code and only
  becomes meaningful for `async fn` (out of scope for replay already, see
  "Known limitations").

### Optional: capturing a function's own internal state

The two entry/close events above only ever show a span's *entry*
arguments — useful, but silent about anything computed *during* the
call. Two ordinary `tracing` mechanisms, already emitted by the exact
same setup above with zero changes to it, let you see more, and the
viewer's "Execution context" panel (bottom-right, next to the replay
list) shows both automatically once a trace has them:

- **A value that doesn't exist yet at entry** — declare it as an empty
  field, fill it in once it's actually known:

  ```rust
  #[tracing::instrument(fields(doubled = tracing::field::Empty))]
  fn compute(x: i32) -> i32 {
      let doubled = x * 2;
      tracing::Span::current().record("doubled", doubled);
      doubled + 1
  }
  ```

  Shows up as `recordedFields` on that call's trace entry — the span's
  *current* fields as of its own close event, not its entry-time ones
  (`x` is still there too; `record()` only ever adds/updates fields, it
  never removes ones `#[instrument]` already captured). Only ever shows
  the *latest* value for a given field — recording it twice keeps just
  the second value, no history of the first.

- **A progression of values across multiple points in the call** — a
  plain event, logged from anywhere inside the function body:

  ```rust
  #[tracing::instrument]
  fn run(n: i32) -> i32 {
      let mut total = 0;
      for step in 0..n {
          total += step;
          tracing::info!(step, total, "running total after this step");
      }
      total
  }
  ```

  Shows up as `events` — a list of `{message, fields}`, one per
  `event!`/`info!`/... call, in the order they actually fired, across
  every invocation. Unlike `record()` above, this keeps every value along
  the way, not just the final one.

Both are entirely optional and additive: a trace with none of this still
parses exactly as it always has. See `codemap/schema/trace-event.schema.json`
for the third line shape a plain event produces (distinct from entry/close
— told apart by `fields.message`, since an event's own `span` field
reports its *enclosing* span's identity, never one of its own).

### Optional: telling concurrent calls apart

Add `.with_thread_ids(true)` (and, for readable names instead of raw
numbers, `.with_thread_names(true)`) to the same setup:

```rust
tracing_subscriber::fmt()
    .json()
    .with_span_events(...)
    .with_thread_ids(true)
    .init();
```

This fixes a real, confirmed correctness problem, not just a nice-to-have:
without it, if two threads' spans genuinely overlap (thread A opens a span,
thread B opens its own before A's closes), the parser has no way to tell
they're on different threads — it ends up treating B's span as *nested
inside* A's, which is wrong, and during replay produces a real, misleading
result (an edge stays visually "active" long after execution has actually
moved on, while the edge to what's really running never lights up at all).
With thread ids present, each thread gets its own independent stack, so
this corruption can't happen — a concurrent span with no real ancestor on
its own thread falls back to the same static-graph inference `main`'s own
direct children already use (see "How a span is matched to a graph node"
below): if the call graph shows exactly one function that could possibly
have called it, that's not a guess, so the edge from it lights up
correctly during replay. Only genuinely ambiguous cases (2+ static callers
of the same function) stay an honest "no known caller" (`stack: []`).

**What this does not do**: reconstruct *which specific thread* is running
concurrently with which, or show more than one thread's activity live at
once. `tracing` doesn't carry a span across a `thread::spawn` boundary on
its own (confirmed directly — a child function called from inside an
instrumented parent's own thread shows an empty ancestor list unless the
parent explicitly captures `tracing::Span::current()` before spawning and
re-enters it inside the new thread), so replay still steps through
concurrent spans one at a time, in trace order, same as everything else —
it just now correctly *attributes* each one to its real caller when the
static graph makes that unambiguous, instead of leaving it disconnected.
A genuinely simultaneous, multi-thread-aware replay view would need
changing every thread-spawning call site in the target code and a
different replay model entirely — deliberately out of scope for this tool
(see PROJECT.md §4).

One more correctness detail, separate from the attribution above: a span
that spawned other threads and is blocked on joining them (like
`concurrent_demo` above) stays visually *active* in the replay view for as
long as those threads are still running, rather than showing as "returned"
the moment replay steps past it. This isn't inferred or guessed — every
span carries `openSeq`/`closeSeq`, the real position of its own NEW/CLOSE
line in the raw log, and the viewer only marks a span "visited" once its
own `closeSeq` shows it actually closed by that point. For ordinary
synchronous code this is exactly the same as before (a span's own close
always precedes its next sibling's open there), so nothing changes; it
only ever produces a different, more accurate result for concurrent code,
where that guarantee doesn't hold.

The attribution above also carries the confirmed parent's *entire* own
ancestor chain, not just its bare name — `concurrent_demo`'s attributed
children resolve to `['main', 'concurrent_demo']`, not `['concurrent_demo']`
alone. This matters for more than depth accuracy: the replay view's
"unwinding" animation between steps decides how far back to animate by
comparing two spans' ancestor chains for a shared prefix, and a
truncated, one-name-only chain shares nothing with its own parent's real
chain even though one genuinely leads to the other — which showed up as a
real, visible bug (a "returning to main" flash firing on the very first
step into a spawned thread, before that thread's own edge had even lit up).

### How a span is matched to a graph node

`span.name` is **not** trusted as the match key by itself, for two reasons
confirmed by direct testing, not just theory: a method's *default*
instrumented name is just its bare name (`grand_total`), never this tool's
own `Type::method` node id (`Batch::grand_total`) — so plain name-matching
already fails for every method, with no customization involved at all —
and `#[instrument(name = "...")]` can rename a span to anything, free
functions included.

With `.with_file(true).with_line_number(true)` set (and `--doc`/`source_index.json`
generated, which is where the graph side of this comes from), `trace_log.py`
instead resolves each span by real source location: `#[instrument]` always
reports the line *it itself* starts on — never the `fn`/method line, and
never shifted by a multi-line attribute, further attributes stacked after
it, or comments of any kind in between (measured directly across all of
those shapes; the gap to the real item line ranged from 1 to 11 lines in
small test cases alone, ruling out a fixed tolerance window as a fix). The
resolver scans the actual source file forward from that line, skipping
exactly what Rust allows between an attribute and its item, to the true
`fn`/method line — which lines up exactly with what `source_index.json`
already records for that same item via `cargo doc`'s own source link. This
only works server-side (`trace_log.py`, via `/__codemap_parse_trace`) since
it needs to read the target project's own `.rs` files directly, something
the browser can't do for a file it wasn't handed directly — the viewer's
`parseTraceJsonl()` client-side fallback (used only if the server is
unreachable, e.g. `index.html` opened straight off disk) still matches by
name only, with the limitations above.

Known current limitations:

- If a span still doesn't resolve (no `source_index.json`, an unreadable
  source file, or a macro-generated `fn` with no literal source line to
  scan to), it falls back to matching by name — an ancestor-span entry in
  `spans` will only resolve if that same span also appeared elsewhere in
  the trace with its own `filename`/`line_number` (true for any span
  tracing itself emitted, since every span gets its own top-level entry
  when entered) to resolve from.
- Spans are deduplicated by (resolved node id, call site) — not by node id
  alone. A single static call site invoked repeatedly (a loop, or simply
  called again from a separate activation of the same caller) still
  collapses into one trace entry with an iteration count, as before. A
  callee reached from **N different** static call sites in the same
  caller (see `graph.json`'s own `callOrder` on each edge, from
  `mir_graph.py`) instead produces up to N separate entries, one per site
  — occurrences are assigned to sites in trace order (1st occurrence ->
  smallest `callOrder`, 2nd -> next, ...), wrapping around if there are
  more occurrences than known sites (e.g. one of the sites is itself in a
  loop) — an approximation in that specific mixed case, but still splits
  into the right *number* of distinct entries rather than merging them
  all into one. This needs `graph.json` (passed to `parse_trace()`
  server-side); with no graph available, every occurrence of a
  single-site relationship still aggregates exactly as before.
- If a traced function calls another traced function through an
  un-instrumented one in between (no `#[instrument]` on the middle
  function), the trace still correctly attributes the callee to its real,
  still-open ancestor — but the static graph has no direct edge between
  them (only the real, declared calls through the untraced function).
  Replay renders a distinct synthetic edge for this instead of showing
  nothing: dashed, its own muted-grey color, labeled "(untraced gap)" — it
  never claims a direct call happened, just that the trace confirms these
  two are related with something untraced in between. See `dummy-api::gap
  _demo`/`untraced_relay`/`gap_leaf` in the dummy-lib fixture, and
  PROJECT.md §2.12 for the full reasoning (including why a guessed
  multi-hop path through the graph was rejected in favor of this).

The small monospace text next to the "Rust Codemap" title (e.g.
`js:15:08:47 · trace_log.py:15:01:44 · __main__.py:15:07:25 · pid 744`) is
not decorative — it's the actual, on-disk last-modified time of the files
this specific server process is serving right now, fetched fresh on every
page load from `GET /__codemap_version` (never cached, never a hand-
maintained version number). If you've edited `trace_log.py` or
`__main__.py`, this won't reflect it until the Python process itself is
restarted (imports happen once at startup) — `index.html`'s own timestamp,
by contrast, updates on a plain page reload, since it's served fresh from
disk on every request. Added after a stale server process answered
requests with old code for hours despite looking freshly restarted — see
PROJECT.md §2.12's third follow-up.

## Command reference

```
python -m codemap run   --project <path> [--port 8787] [--no-browser] [--include-private]
python -m codemap graph --project <path> [--out <path>]
python -m codemap doc   --project <path> [--graph <path>] [--out <path>] [--include-private]
python -m codemap serve [--dir viewer] [--docs <path>] [--graph <path>] [--doc <path>] [--port 8787]
python -m codemap selfcheck [--project ../dummy-cli]
python -m codemap validate-trace <trace.jsonl>
```

`--include-private` (on `doc`/`run`) also documents non-`pub` items
(passes `--document-private-items` to `cargo doc`) so the viewer's "Show
private" toggle (hidden entirely if you didn't pass this) has something to
show. Off by default: rendering pages for every private item is a real
cost that scales with how many exist, on top of the type-checking `cargo
doc` already does either way — see [PROJECT.md](PROJECT.md) §2.9.

`selfcheck` and `validate-trace` are this tool's own internal checks, not
something you run against your own project: `selfcheck` builds the graph
for a known fixture (default `../dummy-cli`, this repo's own test fixture)
and asserts a fixed set of facts about it, specifically to catch a future
Rust toolchain upgrade silently changing MIR's text format (see
[PROJECT.md](PROJECT.md) §4, "MIR as the only extraction source");
`validate-trace` checks any trace.jsonl against the schema in
`codemap/schema/` — genuinely useful on your own trace too, to confirm it
actually matches the mandated format above.

`--out`/`--graph` (on `graph`/`doc`) default to `<target crate>/target/rust-codemap/<crate name>/...` — see
above. Run any subcommand with `--help` for details.

`run` always passes its own `--docs`/`--graph`/`--doc` to `serve`
automatically, pointing at exactly what it just generated: `--docs` at
`<target crate>/target/doc/` (see "Doc-driven graph focus" below for what
that's for), `--graph`/`--doc` at that run's `graph.json`/
`source_index.json` (served at fixed `/graph.json` / `/source_index.json`
URLs the viewer auto-loads on page load — see "Quick start"). Calling
`serve` directly needs these spelled out if you want the same thing
without regenerating — e.g. to reopen a graph you already built:

```sh
python -m codemap serve --graph /path/to/target/rust-codemap/<crate>/graph.json \
                         --doc /path/to/target/rust-codemap/<crate>/source_index.json \
                         --docs /path/to/target/doc
```

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

Node ids are qualified by the crate that actually defines them
(`crate::name` for a free function, `crate::Type::method` for a method) —
if two different crates in the closure define the same free function name
or the same `Type::method` pair, they still render as two distinct graph
nodes, not one collapsed together. Resolving a call site's actual target
crate isn't always as simple as reading an explicit qualifier off the MIR
text, though — see [PROJECT.md](PROJECT.md) §2.8 for how cross-crate calls
get resolved (and the one case, a third crate calling a method name shared
by two *other* crates with no qualifier of its own, that MIR text alone
genuinely can't disambiguate).

## How the call-graph is built

`codemap/mir_graph.py` parses MIR text (produced via `cargo build` with
`RUSTFLAGS=--emit=mir`, see above) with regular expressions — no AST, no
type-checker, no external tool beyond `rustc` itself. It handles a few
cases a naive "grep for calls" would miss:

- **Dynamic dispatch** (`&dyn Trait` calls): over-approximated by linking to
  *every* known implementation of that trait method across the whole
  dependency closure (not just the calling crate), since MIR alone can't
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
  replay does not, and this is an accepted limitation rather than a
  planned fix: a trace only exists because *something* ran and produced
  log output, and only a binary's `main()` is guaranteed to be that
  something (the replay animation's unwind-to-root also keys off finding a
  node whose id ends in `::main`). The same reasoning is why replay doesn't
  show more than one thread's activity *at once*, or replay `async fn` —
  the tool replays *log order* within a thread, which only maps onto a
  real call stack for ordinary synchronous, single-threaded execution.
  What it *can* do, without any propagation change in the target code: a
  spawned-thread span with no recorded ancestor gets attributed back to
  whichever function the static call graph shows as its one and only
  possible caller — not a guess, since with a single candidate that's the
  only answer that fits. `dapi::concurrent_demo` calling `thread_b`/
  `thread_c` in two real OS threads (see the dummy-lib fixture) is
  attributed and colored correctly this way, even though `tracing` itself
  never recorded that link. It's still just an inference from the graph,
  not a real trace-recorded fact: a function reachable from 2+ different
  static call sites stays an honest "no known caller" on the same
  ancestor-less hit, since which one actually called it that time genuinely
  isn't recoverable. See "Optional: telling concurrent calls apart" above
  for the correctness fix (per-thread stacks) this inference builds on:
  without thread ids, two genuinely concurrent spans can get parsed as if
  one were nested inside the other (they aren't) — confirmed as a real bug,
  not hypothetical; a concurrent span with no thread ids at all still parses
  safely too, it's just indistinguishable from ordinary single-threaded
  code, which is the original, still-present limitation.
- Cross-crate node-id collisions are resolved by qualifying every node id
  with its own crate (see "Multi-crate merging" above) — the one thing
  that's still just an approximation, not fully resolved, is a *third*
  crate calling a method name two *other* crates both happen to share,
  with no crate qualifier of its own in the MIR text to disambiguate by;
  that specific case fans out to every possible match rather than picking
  one, the same way an actual `&dyn Trait` call already does.
- All call edges are typed generically as `call` — the distinction between
  a direct call, a dynamic dispatch, and a loop are not yet recovered from
  MIR (the viewer already supports styling `dispatch`/`loop_call`/
  `trampoline` differently if the generator is later extended to emit them).
- There's no dedicated "call stack + timing" frame yet beyond the sidebar's
  flat execution-trace list.
- The real rustdoc page opens in a separate browser tab (see "Doc-driven
  graph focus" above), not embedded in the viewer — navigating a link on
  that page has no way to sync back to the doc list's selection or the
  graph's focus, since it's a plain, unrelated tab at that point.

## License

See [LICENSE](LICENSE).
