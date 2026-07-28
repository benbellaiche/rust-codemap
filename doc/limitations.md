# Known limitations

- Regex-based MIR parsing has been exercised against generics/
  monomorphization, recursion, nested/chained closures, chained iterator
  combinators, and `async fn` (a dedicated fixture, one function per shape)
  — two real bugs found and fixed (a generic call site's monomorphized
  turbofish, and recursion producing no self-edge); the rest already worked.
  Crate-root `impl` blocks, module-qualified `Self` types, and
  inconsistently-qualified free functions were bugs found earlier via the
  same kind of dedicated multi-crate test fixture and are also fixed.
  `cargo vendor`-based dependencies are
  handled too, though the specific failure mode this guards against
  (a vendored dependency's code being mistaken for the target crate's own)
  couldn't actually be reproduced on this toolchain to confirm it matters
  in practice — kept as a low-cost safeguard regardless.
- The static call-graph (`run`/`build`) works for both binaries and
  libraries, standalone or with local dependencies merged in. Execution
  replay does not, and this is an accepted limitation rather than a
  planned fix: a trace only exists because *something* ran and produced
  log output, and only a binary's `main()` is guaranteed to be that
  something (the replay animation's unwind-to-root also keys off finding a
  node whose id ends in `::main`). The same reasoning is why replay doesn't
  show more than one thread's activity *at once* — the tool replays *log
  order* within a thread, which only maps onto a real call stack for
  ordinary synchronous, single-threaded execution (`async fn` is now
  handled correctly too, single- or multi-threaded runtime alike, with
  ENTER/EXIT tracking — see
  [tracing-format.md](tracing-format.md) "Why each setup flag matters").
  What it *can* do, without any propagation change in the target code: a
  spawned-thread span with no recorded ancestor gets attributed back to
  whichever function the static call graph shows as its one and only
  possible caller — not a guess, since with a single candidate that's the
  only answer that fits. A span opened from a real `std::thread::spawn`'d
  OS thread with no recorded ancestor is attributed and colored correctly
  this way, even though `tracing` itself never recorded that link. It's
  still just an inference from the graph,
  not a real trace-recorded fact: a function reachable from 2+ different
  static call sites stays an honest "no known caller" on the same
  ancestor-less hit, since which one actually called it that time genuinely
  isn't recoverable. See [tracing-format.md](tracing-format.md) "Why each
  setup flag matters" for the correctness fix (per-thread stacks) this
  inference builds on: without thread ids, two genuinely
  concurrent spans can get parsed as if one were nested inside the other
  (they aren't) — confirmed as a real bug, not hypothetical; a concurrent
  span with no thread ids at all still parses safely too, it's just
  indistinguishable from ordinary single-threaded code, which is the
  original, still-present limitation.
- Cross-crate node-id collisions are resolved by qualifying every node id
  with its own crate (see [architecture.md](architecture.md) "Multi-crate
  merging") — the one thing that's still just an approximation, not fully
  resolved, is a *third* crate calling a method name two *other* crates
  both happen to share, with no crate qualifier of its own in the MIR text
  to disambiguate by; that specific case fans out to every possible match
  rather than picking one, the same way an actual `&dyn Trait` call
  already does.
- All call edges are typed generically as `call` — the distinction between
  a direct call, a dynamic dispatch, and a loop are not yet recovered from
  MIR (the viewer already supports styling `dispatch`/`loop_call`/
  `trampoline` differently if the generator is later extended to emit them).
- There's no dedicated "call stack + timing" frame yet beyond the sidebar's
  flat execution-trace list.
- The real rustdoc page opens in a separate browser tab (see
  [viewer-guide.md](viewer-guide.md) "Doc-driven graph focus"), not
  embedded in the viewer — navigating a link on that page has no way to
  sync back to the doc list's selection or the graph's focus, since it's a
  plain, unrelated tab at that point.
