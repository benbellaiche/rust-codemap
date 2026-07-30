# Tracing log format

To replay a real execution, your binary needs to emit its `tracing` spans
as JSON Lines, with source location and thread info attached, into their
own file. Do this once, in `main()`.

## 1. Set up the subscriber

At the very top of `main()`, before anything else runs, copy this function
as-is into your own project and call it there — it has no dependency on
anything else in the file it came from
(`examples/dummy-cli/src/main.rs`, which does exactly this):

```rust
/// Sets up the global `tracing` subscriber cargo-codemap's replay needs:
/// JSON Lines, one span/event per line, with source location, thread
/// identity, and the NEW/ENTER/EXIT/CLOSE span events all captured -- see
/// "Why each setup flag matters" below for what breaks without each one.
/// `path` is created (truncated if it already exists); `level` is the
/// minimum level captured at all.
fn init_tracing(path: impl AsRef<std::path::Path>, level: tracing::Level) {
    let path = path.as_ref();
    let trace_file = std::fs::File::create(path)
        .unwrap_or_else(|e| panic!("failed to create trace file {}: {e}", path.display()));
    tracing_subscriber::fmt()
        .json()
        .with_max_level(level)
        .with_span_events(
            tracing_subscriber::fmt::format::FmtSpan::NEW
                | tracing_subscriber::fmt::format::FmtSpan::ENTER
                | tracing_subscriber::fmt::format::FmtSpan::EXIT
                | tracing_subscriber::fmt::format::FmtSpan::CLOSE,
        )
        .with_file(true)
        .with_line_number(true)
        .with_thread_ids(true)
        .with_thread_names(true)
        .with_writer(move || trace_file.try_clone().expect("failed to clone trace file handle"))
        .init();
}
```

Call it once, at the very top of `main()`:

```rust
init_tracing("trace.jsonl", tracing::Level::INFO);
```

`level` is the minimum level captured at all — pass `tracing::Level::TRACE`
to keep everything, or a higher level to drop noisy spans from the trace
file. Needs `tracing-subscriber` as a dependency with its `json` feature
enabled (`tracing-subscriber = { version = "0.3", features = ["json"] }`),
alongside `tracing` itself.

No extra dependency beyond that — `.with_writer(...)` just takes a closure
returning anything that implements `Write`; `File::try_clone()` is a cheap
fd/handle duplication, not a copy of the file's contents. This is exactly
what `examples/dummy-cli/src/main.rs` does, including for its
`async_mono`/`async_multi` (tokio) cases — a plain synchronous file write
is fast enough that it's never been worth adding an async/non-blocking
writer (e.g. the `tracing-appender` crate) on top.

**Simpler alternative, if your binary doesn't log anything else:** drop
the `trace_file` line and the `.with_writer(...)` call, then redirect
stdout instead when you run it:

```sh
your-binary > trace.jsonl
```

Either way, this setup captures everything the viewer needs: every span's
entry/exit/close, its source location (so it can be matched to a graph
node), and thread identity (so concurrent and `async fn` code replay
correctly) — see "Why each setup flag matters" below for what breaks
without each one.

## 2. Instrument the functions you want to see

```rust
#[tracing::instrument]
fn my_function(x: i32) -> i32 {
    // ...
}
```

Add `#[tracing::instrument]` to every function you want to appear in the
replay. A function without it produces no span (it can still appear as a
node in the static call graph — instrumentation only affects replay).
By default, every parameter is captured as a field (using its `Debug`
output) — a few common variations:

- **A parameter that isn't worth logging** (doesn't implement `Debug`,
  is huge, or is just noise) — `skip`:

  ```rust
  #[tracing::instrument(skip(large_buffer))]
  fn process(large_buffer: &[u8], id: u32) {
      // ...
  }
  ```

  `skip_all` drops every parameter at once, if you'd rather add specific
  ones back explicitly (see `fields` below) than skip them one by one.

- **An extra field that isn't a parameter at all** — `fields`:

  ```rust
  #[tracing::instrument(fields(request_id = %req.id()))]
  fn handle(req: &Request) {
      // ...
  }
  ```

  `%` uses the value's `Display` output (`req.id()` here, not `req`
  itself); `?` uses `Debug` instead. Combine with `skip` to replace a
  noisy parameter with a cleaner derived field:

  ```rust
  #[tracing::instrument(skip(req), fields(request_id = %req.id()))]
  fn handle(req: &Request) {
      // ...
  }
  ```

These only affect what shows up as the span's *entry* fields. A value only
known partway through the call, or a running log of values across the
call, needs "Optional: capturing a function's own internal state" below.

## The JSON Lines format

The target binary writes one JSON object per line, exactly what the setup
above produces via
[`tracing_subscriber`](https://docs.rs/tracing-subscriber)'s JSON
formatter. Each line is a span **entry**, span **close**, or a plain
**event**:

- **Entry**: `{"filename": "...", "line_number": N, "span": {"name": "my_span", ...fields}, "spans": [ {"name": "caller"}, ... ]}`
  — `span.name` identifies the call (its literal text isn't actually
  load-bearing, see "How a span is matched to a graph node" below);
  `spans` is the ordered list of enclosing spans (the call stack at that
  point, root first); `filename`/`line_number` are `#[instrument]`'s own
  source location.
- **Close**: same shape, but `fields` additionally contains `"time.busy"`
  (a duration string like `"1.23ms"`, `"450µs"`, `"2.1s"`) — this is where
  per-call duration and iteration counts come from. `time.busy` is the
  only duration this tool reads or displays anywhere — it's wall-clock
  time the span was actually entered (i.e. really executing, including
  any nested calls), not a CPU-usage metric. The sibling `time.idle`
  field tracing also reports is not used anywhere in this tool.
- **Event**: a plain `tracing::event!`/`info!`/... call from inside an
  instrumented function's own body. Told apart from entry/close by
  `fields.message`; its own `span` field reports its *enclosing* span's
  identity, not one of its own. See
  `schema/trace-event.schema.json` for the exact shape.

## Why each setup flag matters

- **`.with_file(true).with_line_number(true)`**: without it, spans can
  only be matched by name, which silently fails for every method (see
  "How a span is matched to a graph node" below) and can't tell two call
  sites of the same function apart at all.
- **`.with_thread_ids(true)`/`.with_thread_names(true)`**: fixes a real,
  confirmed correctness problem, not just a nice-to-have. Without it, if
  two threads' spans genuinely overlap (thread A opens a span, thread B
  opens its own before A's closes), the parser has no way to tell they're
  on different threads — it ends up treating B's span as *nested inside*
  A's, which is wrong, and during replay produces a misleading result (an
  edge stays visually "active" long after execution has actually moved
  on). With thread ids present, each thread gets its own independent
  stack, so this can't happen — a concurrent span with no real ancestor on
  its own thread falls back to the same static-graph inference `main`'s
  own direct children already use: if the call graph shows exactly one
  function that could possibly have called it, that's not a guess, so the
  edge from it lights up correctly during replay. Only genuinely ambiguous
  cases (2+ static callers of the same function) stay an honest "no known
  caller" (`stack: []`).

  What this does *not* do: reconstruct *which specific thread* is running
  concurrently with which, or show more than one thread's activity live at
  once. `tracing` doesn't carry a span across a `thread::spawn` boundary on
  its own, so replay still steps through concurrent spans one at a time,
  in trace order — it just now correctly *attributes* each one to its
  real caller when the static graph makes that unambiguous, instead of
  leaving it disconnected.

  Separately: a span that spawned other threads and is blocked on joining
  them stays visually *active* in the replay view for as long as those
  threads are still running, rather than showing as "returned" the moment
  replay steps past it — every span carries `openSeq`/`closeSeq` (the real
  position of its own NEW/CLOSE line in the raw log), and the viewer only
  marks a span "visited" once its own `closeSeq` shows it actually closed
  by that point.

- **`FmtSpan::ENTER | FmtSpan::EXIT`** (alongside the always-needed
  `NEW | CLOSE`): fixes a real correctness problem for `async fn`, the
  same way thread ids do for genuinely concurrent OS threads. A sync fn's
  span is entered once and never exited again until it closes — but an
  `async fn` can be entered and exited many times over its own lifetime,
  once per executor poll, and a real `.await` suspension means the
  executor is free to run something else *entirely unrelated* on the same
  thread in the meantime. Without ENTER/EXIT, the parser has no way to
  tell "this span is merely suspended, not actually the active caller"
  from "this span is genuinely still running" — confirmed as a real,
  reproducible bug: two independent async fns run concurrently via
  `tokio::join!` came back with the shorter one wrongly attributed as
  nested under the longer one, even though they're siblings. With
  ENTER/EXIT present, the parser correctly tracks which span is
  *genuinely* active at any point, not just which one hasn't closed yet.

  Multi-threaded async runtimes work too (the default for most
  `#[tokio::main]` setups) — a task's own polls can migrate across
  different OS threads over its lifetime, and this is tracked correctly:
  a span that suspends on one worker thread and resumes on another still
  resolves correctly.

## Optional: capturing a function's own internal state

The entry/close events only ever show a span's *entry* arguments — silent
about anything computed *during* the call. Two ordinary `tracing`
mechanisms, already emitted by the setup above with zero further changes,
let you see more; the viewer's "Execution context" panel shows both
automatically once a trace has them:

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
  *current* fields as of its own close event. Only ever shows the
  *latest* value for a given field — recording it twice keeps just the
  second value, no history of the first.

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

## How a span is matched to a graph node

`span.name` is **not** trusted as the match key by itself, for two reasons
confirmed by direct testing: a method's *default* instrumented name is
just its bare name (`grand_total`), never this tool's own `Type::method`
node id (`Batch::grand_total`) — so plain name-matching already fails for
every method — and `#[instrument(name = "...")]` can rename a span to
anything, free functions included.

With `.with_file(true).with_line_number(true)` set (and `source_index.json`
generated, which is where the graph side of this comes from), `trace_log.rs`
instead resolves each span by real source location: `#[instrument]` always
reports the line *it itself* starts on, and the resolver scans the actual
source file forward from that line, skipping exactly what Rust allows
between an attribute and its item, to the true `fn`/method line — which
lines up exactly with what `source_index.json` already records for that
same item via `cargo doc`'s own source link. This only works server-side
(`trace_log.rs`, via `/__codemap_parse_trace`) since it needs to read the
target project's own `.rs` files directly — the viewer's
`parseTraceJsonl()` client-side fallback (used only if the server is
unreachable, e.g. `index.html` opened straight off disk) still matches by
name only, with the limitations below.

Known current limitations:

- If a span still doesn't resolve (no `source_index.json`, an unreadable
  source file, or a macro-generated `fn` with no literal source line to
  scan to), it falls back to matching by name.
- Spans are deduplicated by (resolved node id, call site) — not by node id
  alone. A single static call site invoked repeatedly (a loop, or simply
  called again from a separate activation of the same caller) still
  collapses into one trace entry with an iteration count. A callee
  reached from **N different** static call sites in the same caller
  instead produces up to N separate entries, one per site, via
  `graph.json`'s own `callOrder` on each edge.
- If a traced function calls another traced function through an
  un-instrumented one in between, the trace still correctly attributes
  the callee to its real, still-open ancestor — but the static graph has
  no direct edge between them. Replay renders a distinct synthetic edge
  for this instead of showing nothing: dashed, its own muted-grey color,
  labeled "(untraced gap)". See the `gap` case in `examples/dummy-cli`
  (`gap_entry` -> `gap_relay` (untraced) -> `gap_leaf`).

The small monospace text next to the "Rust Codemap" title in the viewer is
the on-disk last-modified time of the files the running server is
currently serving, fetched fresh on every page load — see
[viewer-guide.md](viewer-guide.md) "The version badge".
