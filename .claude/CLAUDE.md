# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

An interactive call-graph viewer for Rust codebases, with execution replay
from real `tracing` logs. It is a standalone, generic tool: it must work
against **any** Rust crate, binary or library, standalone or with local
(path) dependencies merged in, with zero hardcoded knowledge of that
crate's module names, type names, or file layout. This constraint ("zero
mapping") is the main design principle running through the codebase — see
"Design constraints" below before adding anything that special-cases a
target project.

Read [README.md](README.md) for the user-facing workflow and links to the
rest of `doc/` (command reference, tracing log format, viewer guide,
architecture, known limitations).

**Rust (`src/*.rs`, `Cargo.toml` at the repo root) is the primary,
actively-developed implementation, invoked as `cargo codemap` once
installed (`cargo install --path .`).** It originated as a faithful port of
a Python original, kept for reference only under
[archive/codemap-python/](archive/codemap-python/) (not actively developed —
see its own `codemap/__main__.py` docstring). `schema/` and `viewer/` at the
repo root are shared by both, not duplicated.

## Commands

Run from the repo root (`cargo run --` so this works straight from a
checkout without installing first; once installed via `cargo install
--path .`, drop `cargo run --` and call `cargo codemap <subcommand>`
directly). `--project <path>` points at the target Rust crate to analyze (there is no
target crate committed to this repo) -- defaults to `.`, same as `cargo
build`, so it's only needed when pointing at a crate other than the
current directory. Full flag list: [doc/commands.md](doc/commands.md).

**When testing a fix, use `cargo run --` or the freshly-built
`target/debug/cargo-codemap.exe` -- never `cargo codemap` itself unless
you just re-ran `cargo install --path .`.** The installed copy is a frozen
snapshot; a real, previously-hit confusion was concluding a fix didn't
work because it was actually tested through the stale installed binary,
not the just-rebuilt one. If you change `src/` and the user is testing via
`cargo codemap`, tell them to reinstall before reporting the fix as ready.

Build if graph/doc are missing or stale, then serve -- auto-loads in the
viewer, no clicks:

```sh
cargo run -- run --project <path>
```

Always regenerate `<target_dir>/cargo-codemap/<crate>/{graph.json,source_index.json}`:

```sh
cargo run -- build --project <path>
```

Check a trace against `schema/trace-*.schema.json`:

```sh
cargo run -- validate-trace <trace.jsonl>
```

`graph`/`doc` used to be separate subcommands; merged into one `build`
since they're never meaningfully used apart -- `doc`'s own cross-referencing
needs a `graph.json` to resolve method nodes against anyway. `serve` used to
be its own subcommand (re-serving already-generated output without
rebuilding); folded into `run`'s own staleness check instead -- `run` now
IS the "just serve" case whenever the existing output is already fresh, so
a separate always-just-serve command added nothing a fresh `run` didn't
already cover. The MIR-format canary (`selfcheck`) moved to a `cargo test`
(`selfcheck_dummy_cli`, a `#[test]` inside `src/main.rs`'s own `#[cfg(test)]
mod tests` -- run with a plain `cargo test`) since it's a maintainer
regression check, not something an end user running `cargo codemap` on
their own project ever needs -- run it after any change to
`mir_graph.rs`, and after any Rust toolchain upgrade (rustc's MIR
pretty-printer output isn't a stable, versioned format -- this canary is
what catches it silently changing shape).

No `trace` subcommand -- removed (in the original Python) once "Load
trace…" in the viewer started accepting a raw log directly (parsed
server-side via `/__codemap_parse_trace`) and the other subcommands' own
manual-load fallback (the old "Load graph…"/"Load doc index…" buttons) was
removed too, in favor of `run`'s own fixed-URL auto-load.

No `--bin`/`--lib`: dropped entirely once MIR generation stopped needing
cargo told which target to build (see "Multi-crate merging" below) — the
flag became purely vestigial and keeping it would just be clutter.
`<target_dir>` is the *target* crate's own `cargo metadata` target
directory, not anything under this repo — see "Design constraints" below.
`<crate>` is that crate's *actual compiled* name, via `crate_name()` --
**not** simply the package name with hyphens underscored: a `[lib] name`
override in Cargo.toml changes it, and assuming otherwise silently drops
that crate from the graph -- a real bug found this way; `crate_name()`
checks the `[lib]` table first, falls back to the package-name derivation
only if there's no override. This subdirectory is **required** even for a
single-crate project: every member of a workspace shares one `target_dir`,
so without it, two crates in the same workspace would overwrite each
other's output at the same path (this happened too). The viewer never
fetches project-specific files automatically as the primary path; use its
"Load graph…" / "Load doc index…" / "Load trace…" buttons to pick up
whatever `graph`/`doc`/`trace` (or `run`) just wrote.

`cargo test` (runs `selfcheck_dummy_cli` in `src/main.rs`'s own
`#[cfg(test)] mod tests` -- builds `graph` against `examples/dummy-cli` and
asserts a fixed set of structural facts about the result: specific nodes,
edges, the `traced` flag, `call_order` renumbering, the cross-crate
collision fix) and `validate-trace` (checks a trace.jsonl against
`schema/`) are real, repeatable automated checks -- run `cargo test` after
any change to `mir_graph.rs`/`main.rs`'s graph-building code, and after any
Rust toolchain upgrade (this is the canary that catches a future rustc
MIR-format change instead of it failing silently). Beyond that, to
sanity-check a change to `src/`, run the commands above against
`examples/dummy-cli` (`dummy-core` + `dummy-api` + `dummy-cli`, see its own
README): confirm `build` merges both `dummy-core`/`dummy-api` when pointed
at `dummy-cli`, and that pointing at `dummy-core` alone never pulls in
`dummy-api`/`dummy-cli` (it depends on them, they don't depend on it) --
this exact regression happened once already.

There used to be a separate, more exhaustive sibling fixture
(`dummy-lib`/`dummy-cli`, outside this repo) specifically for a `[lib]
name`-override regression and a 3-crate dependency chain -- that project
has since been deleted, and `examples/dummy-cli` doesn't currently have a
`[lib]` override case of its own; if that regression needs re-verifying,
add an override to one of `examples/dummy-core`/`dummy-api`'s `Cargo.toml`
temporarily rather than recreating the old sibling repo.

For execution replay, use any real instrumented binary crate. Then open
the viewer, use the
"Load…" buttons, and check the graph renders and Play/Step works. Note
execution replay is only meaningful for a binary target today (see
"Design constraints").

## Architecture

> Historical note: several sections below cite specific node names
> (`dapi::`/`dcore::`/`dops::`) from the original `dummy-lib`/`dummy-cli`
> sibling repo, used to confirm the design decisions they describe. That
> repo has since been deleted (see "Commands" above) -- the reasoning and
> decisions remain valid, the exact fixture just isn't there to re-run
> those specific examples against anymore. `examples/dummy-cli` (in this
> repo) covers similar cases under different node names.

### Two independent halves

1. **`src/codemap/`** — a Python package, zero third-party dependencies
   (`requirements.txt` is deliberately empty). `__main__.py` is the CLI
   dispatcher (`python -m codemap <subcommand>`); `mir_graph.py`,
   `doc_index.py`, `trace_log.py` are pure parsing modules with no CLI/IO
   concerns of their own — each takes text/paths in and returns a plain
   dict/list out, so they're easy to reason about independent of argparse.
   `src/codemap/schema/trace-{entry,close}.schema.json` are the written trace-
   format contract (plain, standard JSON Schema draft-07 -- readable by any
   real validator, not just this repo); `schema_check.py` is a small,
   dependency-free validator for exactly the subset of JSON Schema those
   two files use, so checking a trace against them (`validate-trace`)
   doesn't need pulling in a third-party `jsonschema` package just for
   that.
2. **`src/codemap/viewer/index.html`** — a single self-contained HTML file (Cytoscape.js
   plus the `cytoscape-dagre` layout extension and its `dagre` dependency,
   all from a CDN via plain `<script>` tags, no build step, no bundler,
   registered with `cytoscape.use(cytoscapeDagre)`) that fetches `graph.json` /
   `source_index.json` / `trace.json` from its own directory. The Python
   side and the viewer only communicate through those three JSON files —
   there is no other coupling.

### `src/codemap/__main__.py` — multi-crate dependency resolution

`local_dependency_closure()` is the piece that decides *which* crates get
merged into the graph. It is deliberately **not** "every member of the
workspace the target crate happens to live in" (`cargo metadata --no-deps`)
— that was the first design, and it was wrong: it swept in unrelated
sibling crates and, worse, client binaries that depend **on** the target
crate rather than the other way around -- a real regression found this
way (the dependency-direction check in "Commands" above, against
`examples/dummy-cli`, guards against it recurring). Instead it calls
`cargo metadata` **without** `--no-deps` to get the
dependency-resolution graph (`resolve.nodes[].deps`), then walks only the
*forward* edges from the target's own node, keeping just the `path+file://`
(local) ones. `cmd_graph` and `cmd_doc` both use this same closure — do not
let them drift apart; `cmd_doc` in particular needs, per crate in the
closure, that crate's *own* `src/` directory for resolving source links,
not the target crate's (`doc_index.build_index()` takes a list of
`(doc_root, src_root)` pairs for exactly this reason).

`LoggingHandler.do_GET` intercepts `/__codemap_version` before falling
through to the base class's static-file serving -- returns each of
`index.html`/`trace_log.py`/`__main__.py`'s real on-disk mtime (`Path(...)
.stat().st_mtime`, computed fresh per request, never cached) plus the
server's own PID. Exists purely to answer "which code is this process
actually running" at a glance (`src/codemap/viewer/index.html`'s `#version-badge`
fetches it once on load) -- added after a stale server process (a zombie
from hours earlier, still bound to the same port) answered requests with
old `trace_log.py` for a long debugging session despite every other signal
suggesting a fresh restart. Deliberately mtime-based, not a hand-maintained
version string -- there's no step to remember, so it can't itself drift
out of sync with what's actually on disk.

### `src/codemap/mir_graph.py` — how the call-graph is actually extracted

This is the part most likely to need care when changed. It parses MIR
*text* (produced via `cargo build -p <crate> ...` with
`RUSTFLAGS=--emit=mir`, one invocation per crate in the dependency closure
above) with regexes — there is no AST and no dependency on rustc's
internals beyond the stability of its MIR pretty-printer output (see
`python -m codemap selfcheck`, above, for the canary that watches this).

**Each crate's MIR is parsed on its own now, not concatenated into one
blob** (`parse_mir(text, crate_name, known_crates, ...)`, one call per
crate; `merge_crates(...)` combines the results). This changed specifically
to fix a real bug: two different crates defining the same free function or
`Type::method` pair used to silently collide into one graph node once
merged ("cross-crate node-id collision"). Every node id is
now qualified with the crate that actually defines it
(`dcore::Item::describe`, `dapi::Item::describe` -- two distinct nodes,
where there used to be one). A call site's *target* can't always be
resolved within its own crate's parse, though (see the next bullet) --
`parse_mir` defers that to `merge_crates`, which sees every crate's own
`local_ids` at once; `edges`/`dyn_edges` carry an unresolved `callee_raw`
(+ `crate_hint`, `_seq`) until then, not a final `target`/`call_order`.

- Free functions defined in the crate being compiled are always **local**
  (anything from another crate is fully path-qualified with that other
  crate's name) -- but MIR is *inconsistent* about whether it prints a
  local free function's own module path or not (`basics::add` right next
  to bare `compute`, both `pub fn` in their own submodule -- confirmed via
  the dummy-lib fixture), **and this inconsistency turns
  out to extend across crate boundaries too, not just within one crate**:
  confirmed on the same fixture, a call from `dummy-api` into
  `dummy-ops::combiner::double` prints as bare `double(...)` (no crate
  qualifier at all), while a call from the same function into
  `dummy-core::basics::add` prints as the fully qualified
  `dcore::basics::add(...)`. `RE_FREE_FN`/`normalize_call` both normalize
  to the bare name either way; `split_crate_hint` additionally strips a
  *known local crate's* name specifically when MIR does include it (never
  a hardcoded name -- `known_crates` comes from `cargo metadata`'s own
  dependency closure, same as everywhere else in this tool). When it
  doesn't, `merge_crates` falls back to searching every OTHER crate's own
  `local_ids` for an unqualified match: same-crate is always checked
  first (so a name existing in both the caller's own crate and some other
  crate resolves to the caller's own crate, not a coincidental match
  elsewhere); a single match elsewhere resolves there; two or more matches
  are genuinely ambiguous from MIR text alone (e.g. a *third* crate
  calling an ambiguous shared method name it has no way to qualify) and
  fan out to every match, sharing one `call_order` -- the same
  over-approximation already used for `&dyn Trait` calls, not a new kind
  of imprecision.
- Similarly, an impl block's module prefix before `<impl at ...>` is only
  present when the impl isn't at the crate root, and the `Self` type in a
  method's first parameter can itself be module-qualified (`&report::Item`,
  not just `&Item`) when that type lives in a submodule. Both prefixes are
  optional/repeatable in `RE_IMPL_SELF`/`RE_IMPL_CTOR` (`(?:\w+::)*`), not a
  single mandatory segment -- a crate-root impl silently vanished from the
  graph before this was fixed.
- A module-qualified impl method (`fn mod::<impl at PATH:...>::name(...)`)
  is treated as local **unless** `PATH` resolves through cargo's dependency
  or toolchain caches (`is_local_impl`, checked via `.cargo`/`registry`/
  `.rustup`/`toolchains` substrings, plus whatever `find_vendor_dirs()` in
  `__main__.py` found in a `.cargo/config.toml` `[source.*] directory =
  "..."` override). This replaced an earlier hardcoded list of "our" module
  names — do not reintroduce a name-based allowlist here; extend the
  path-based heuristic instead. `find_vendor_dirs()` returns *bare
  directory-name* markers (e.g. `"vendor"`), not resolved paths — a vendor
  dir lives inside the workspace, so rustc prints a workspace-relative
  path for it, the same shape a genuinely local crate's own path has, not
  an absolute one the way `.cargo`/`.rustup` naturally are. A resolved
  absolute path here would silently never match anything.
- `normalize_call` tells a "module::free_fn" call-site reference apart from
  a genuine "Type::method" one by Rust's own naming convention (lowercase
  first segment = module/crate qualifier -> drop it; uppercase = a real
  Type -> keep "Type::method"). This is why case matters if you're ever
  tempted to loosen this: it's not a stylistic nicety here, it's the only
  signal distinguishing the two shapes. It also strips a generic call's
  monomorphized turbofish (`generic_max::<i32>` -> `generic_max`) *before*
  that case check runs — do this stripping first if you touch this
  function, or a turbofish segment gets mistaken for one of the two shapes
  above and the real callee name is lost.
- Call-edge insertion has no `callee_id != current_fn_id` guard on the
  direct-call path — genuine recursion (`factorial` calling itself) is a
  real, meaningful self-edge, not noise. (The dyn-dispatch over-
  approximation path still excludes self, a separate judgment call.)
- Each edge carries a `call_order`: an integer that restarts at 1 for every
  *caller*, counting call sites in the order they're hit walking that
  caller's MIR body top-to-bottom. Edges are no longer deduplicated by
  `(caller, callee)` — calling the same callee from two different lines in
  the same function now produces two separate edges with two different
  `call_order` values, not one. This is only as accurate as MIR's own
  basic-block order is to real execution order: exact for straight-line
  code, not guaranteed once a function has branches/loops (MIR doesn't
  encode "which branch runs first"). A `&dyn Trait` fan-out (previous bullet)
  is one call site with an ambiguous target, not several calls — all of its
  fan-out edges share the same `call_order`. **`call_order` is assigned in
  `merge_crates`, in a final renumbering pass, not while scanning a
  crate's own MIR** -- most matched call terms in an instrumented fn's body
  are `#[instrument]`'s own generated scaffolding (`Span::new`,
  `Callsite::interest`, `LevelFilter::current`, ...) that never resolves to
  anything local; assigning the real 1, 2, 3, ... only after resolution
  drops those is what keeps a caller's first *real* call at `call_order 1`
  instead of somewhere in the high teens or twenties purely from counting
  noise (a real regression caught while building the crate-qualification
  fix above, confirmed on `dummy-cli`'s own `main`). `parse_mir` itself
  only assigns a raw per-caller `_seq` (every matched call term, resolved
  or not); edges/dyn-fan-outs sharing the same `(source, _seq)` share one
  final `call_order` once renumbered.
- Closures compile to their own top-level MIR item
  (`...::{closure#N}`). Calls made inside a closure body are reattributed to
  the *enclosing* function (`closure_owner_path`), so a call hidden inside
  `.map(...)` still produces an edge from the right node.
- Calls through `&dyn Trait` are over-approximated: MIR can't tell which
  concrete type is behind the pointer, so the edge fans out to *every*
  locally-known implementation of that method, **across the whole
  dependency closure, not just the calling crate** (confirmed on the
  dummy-lib fixture's `Summable` trait, implemented once in `dummy-ops` and
  once in `dummy-core` -- a call through `&dyn Summable` correctly fans out
  to both crates' implementations). This is a deliberate simplification,
  not a bug — expect it to look noisy on a project with a large trait
  hierarchy.
- The `traced` flag on each node is derived by checking whether the
  function's MIR body contains the tracing crate's `Callsite` scaffolding —
  i.e. whether `#[instrument]`/`span!`/`event!` actually expanded there.
  This is deliberately *not* based on any particular execution's trace
  file, because a single run only exercises the branches its input took;
  `traced == false` means "structurally can never produce a span," which is
  a stronger and more useful statement than "didn't show up in this trace."

### `src/codemap/doc_index.py` — cross-referencing cargo doc

Discovers doc pages purely by filename pattern
(`struct.Name.html`/`enum.Name.html`/`trait.Name.html`/`fn.name.html`) under
`target/doc/<crate>/`, keyed by `(crate name, item name)` — no manual
name-to-file table. `SOURCE_LINK_RE` matches `.../src/<any-crate-name>/...`
generically; don't hardcode a crate name into this regex again (it was
found and fixed once already). The final index is
keyed by crate-qualified id (`crate::Name`, `crate::Type::method`) to match
`mir_graph.py`'s own node ids exactly — required for a free function
(itself a graph node) and for a method (looked up against the *specific*
crate's own type page, never a same-named type in some other crate); a
struct/enum/trait entry additionally gets the bare `Name` key too, but only
from whichever crate is indexed first for that name, so a signature's
rendered type name (never crate-qualified in the HTML itself) can still be
linkified for the common, non-colliding case without resurrecting the old
silent last-crate-wins behavior for the colliding one.

Every entry also carries a `public` bool, used by the viewer's opt-in
"Show private" toggle (only meaningful when `codemap doc --include-
private` was used -- otherwise no private item has a page to have been
indexed from at all). Derived from the scraped signature text
(`sig.startswith("pub ")`) -- **except for methods**, where that alone is
wrong: a trait-impl method's own declaration never carries `pub` (Rust
forbids re-declaring visibility inside `impl Trait for Type { ... }`), so
the plain heuristic would flag every method of an ordinary *public* trait
as private. `_is_trait_impl_method()` disambiguates using rustdoc's own
stable section-header anchors (`id="implementations"` vs `id="trait-
implementations"` on the type's page) -- whichever appears closest before
the method's own anchor decides which case applies. Found and fixed by
testing against the dummy-lib fixture's real `Summable` trait, not assumed
correct from the heuristic alone.

### `src/codemap/trace_log.py` vs. the viewer's inline JS parser

The core tracing-log parsing logic (dedup by resolved span id, `time.busy`
duration parsing, iteration counting) is implemented twice: once here in
Python, once as `parseTraceJsonl()` inside `src/codemap/viewer/index.html`. This
duplication is known, not an oversight — if you fix a bug in one, check
whether it also applies to the other. The two are
no longer equivalent, though, and won't become so: only `trace_log.py` can
resolve a span to its true graph node id by real source location
(file+line, scanned past the `#[instrument]` attribute to the actual item
— see README.md "Tracing log format") rather than by span name, because
that needs reading the target project's own `.rs` files directly, which
only a local server process can do — a browser can't reach an arbitrary
path on disk just because it knows one. `parseTraceJsonl()` stays a
name-only fallback for when there's no server to ask (e.g. `index.html`
opened straight off disk); "Load trace…" already prefers a round trip to
`/__codemap_parse_trace` (`_handle_parse_trace` in `__main__.py`, which
loads `source_index.json` fresh per request) and only falls back to the
client-side parser if that request fails.

`trace_log.py` resolves the ancestor chain (`stack` on each event, and the
`parent` used for call-site dedup) from an internally maintained stack of
currently-open spans — pushed on each NEW event's own resolved id, popped
on CLOSE (which, by construction, always closes whatever is currently
innermost) — **not** by re-parsing an entry's own `spans` array text. This
replaced an earlier design that cached a raw span name -> resolved id
mapping globally: a real bug, not just a simplification, once crate-
qualified ids existed -- two different crates' functions can share the
exact same *default* `#[instrument]` span name (just the bare fn name, no
crate qualifier possible in that name at all), so a global cache kept by
name silently let the second crate's occurrence inherit the first's
resolution. Confirmed on the dummy-lib/dummy-cli fixture's own
`make_and_describe`/`describe` collision test before being fixed. Any
future change to this file should keep resolving from the
live open-span stack, not from `entry["spans"]`'s own text, for the same
reason.

Line classification is a real three-way split, not two: CLOSE
(`"time.busy" in fields`), NEW (`fields.message == "new"`), and EVENT
(anything else — a plain `tracing::event!`/`info!`/... call from inside an
instrumented function's own body, see
`src/codemap/schema/trace-event.schema.json`). An EVENT's own `span` field
reports its *enclosing* span's identity, not one of its own — before this
was recognized as a distinct case, an event line fell into the NEW branch
by mistake and pushed that enclosing name onto `open_stack` a second time,
permanently corrupting every subsequent span's depth/ancestor chain (only
the real CLOSE ever pops). Each open-span stack entry holds `(name,
callOrder)` pairs, not bare names, specifically so an EVENT can look up
its enclosing span's exact dedup key directly — CLOSE reads its own
callOrder the same way now too, instead of recomputing it via a second
counter. `time.busy` (never `time.idle`) is the only duration value read
anywhere in this file or the viewer — confirmed directly (a scratch crate
with a deliberate `std::thread::sleep()` and zero real CPU work still
reports the whole sleep as `time.busy`) that this is wall-clock "was the
span entered" time, not a CPU-usage/CPU-wait split; don't introduce
`time.idle` anywhere on the assumption it means "waiting," it doesn't
distinguish that from ordinary tracing/logging overhead in synchronous
code.

There isn't just one open-span stack, either — `open_stacks` (plural) is
keyed by each entry's own optional `threadId` field (`.with_thread_ids
(true)`, see [doc/tracing-format.md](doc/tracing-format.md)), one independent stack per
thread; entries with no `threadId` at all share one implicit key, so an
ordinary single-threaded trace behaves exactly as if there were only one
stack, same as before this existed. This is a real correctness fix, not
just tidiness: two threads' NEW events can genuinely interleave in the
log (thread A opens a span, thread B opens its own before A's closes) --
with a single shared stack, thread B's span would wrongly appear *nested
inside* thread A's still-open one (confirmed as a reproducible bug on
`dapi::concurrent_demo`, which exists specifically to catch a regression
here: without per-thread stacks, `thread_c` came back nested under
`thread_b` instead of as its sibling under `concurrent_demo`, and replay
visibly got stuck showing the wrong edge as "active"). This does NOT
reconstruct a real, trace-recorded parent link -- `tracing` does not
propagate span context across `thread::spawn` on its own (confirmed
empirically). What it does get, via `implicit_parent` (formerly
`implicit_root_parent`, main-only): a concurrent span
with no ancestor on its own thread is attributed to whichever function the
*static* call graph shows as its one and only possible caller, same
inference `main`'s own direct children already used, just no longer
hardcoded to check only `main` as the candidate. `concurrent_demo` is the
sole static caller of both `thread_b`/`thread_c`, so both resolve to
`stack: ['dapi::concurrent_demo']` and light up correctly during replay.
A function with 2+ static callers (`dcore::add`) still correctly reports
`stack: []` on an ancestor-less hit -- an honest "no known caller," not a
guess, since a single candidate is the only case that's actually certain.

Separately, every span also carries `openSeq`/`closeSeq` -- the 0-indexed
position of its own NEW/CLOSE line in the raw log (all lines counted, NEW/
CLOSE/EVENT alike), not derived from the resolved span list. `src/codemap/viewer/
index.html`'s `stepTo()` uses these instead of trusting step-index order
for "has this span returned yet": a span already stepped past (`i < idx`)
stays visually active if `span.closeSeq >= traceData[idx].openSeq` -- its
own real close hasn't happened yet by the time the current step opened.
For ordinary synchronous code index order and close order are always the
same (RAII), so this changes nothing there; for `concurrent_demo` (blocked
on `.join()`), it's the fix for a real, confirmed bug -- `main ->
concurrent_demo` was showing "returned" (green) the moment replay reached
`thread_b`, which is graphically false. `finishTraceToRoot()` (the extra
"Step >" past the end) sweeps every node still `.current`-classed at that
point and re-derives its own incoming edge from `span.stack` to settle it
green -- NOT by comparing `.style('line-color')` against a hex literal,
which silently never matches (Cytoscape's style getter always returns a
resolved `rgb(...)` string).

`implicit_parent`'s fallback (above) has to set `stack` to the confirmed
parent's ENTIRE chain, not just that one name -- `full_path_by_name` (name
-> that span's own resolved `stack + [name]`, updated on every NEW) is what
supplies it: `stack = full_path_by_name.get(confirmed, [confirmed])`.
Setting `stack = [confirmed]` alone (the first version of this fix) was a
real, confirmed bug, not a style nitpick: the viewer's `computeReturnPath()`
walks two spans' `stack`s looking for a shared prefix to decide how far to
animate an "unwinding" flash, and `concurrent_demo`'s `stack` (`['main']`)
shares nothing with `thread_b`'s truncated one (`['concurrent_demo']`
alone, no `main`) even though `concurrent_demo` genuinely IS its parent --
so it wrongly concluded "unwind all the way to main" and fired that flash
on the very first step into `thread_b`, before `thread_b`'s own edge had
even lit up. Confirmed directly by calling `computeReturnPath(concurrent
_demo, thread_b)` in-browser and seeing a non-empty result (should be `[]`,
"going deeper" -- not a return at all).

The NEW branch's `stack` computation has the SAME class of bug for the
non-empty-`own_stack` case too, not just `implicit_parent`'s fallback:
reading `own_stack`'s raw names directly loses any virtual prefix the
innermost open span itself inherited (e.g. `gap_demo`'s own `["main"]`,
from `implicit_parent` -- `main` is never pushed onto any real
`own_stack`). A REAL descendant of an `implicit_parent`-resolved span
(`gap_leaf`, genuinely nested under `gap_demo` -- tracing's own `spans`
field agrees) came back with `stack: ["gap_demo"]`, `depth: 1` -- same
depth as `gap_demo` itself, rendering as siblings in the sidebar instead of
nested, even though they aren't. Fixed the same way: `stack =
full_path_by_name.get(own_stack[-1][0], ...)` instead of `[nm for nm, _co
in own_stack]` -- the innermost open span's own chain was already fully
resolved (recursively, through any depth) the moment it was pushed, so
reusing it composes correctly. This changed `depth`/`stack` for EVERY
previously-nested span in the whole fixture, not just the gap one (each
gained an explicit `main` prefix it was silently missing) -- confirmed via
a full re-check of `parse_trace()`'s output against the entire trace, not
assumed from the one fixture that surfaced it.

`"enter"`/`"exit"` lines (`FmtSpan::ENTER | FmtSpan::EXIT`,
opt-in on the target -- never emitted for a plain `NEW | CLOSE` setup)
fix a real correctness gap for `async fn`: a sync fn's span enters once
and never exits until CLOSE, so `own_stack` tracking it purely via
NEW/CLOSE is already correct -- but an async fn can be entered/exited many
times (once per executor poll), and a real `.await` suspension means the
executor is free to run something *entirely unrelated* on the same thread
meanwhile. Without tracking this, that unrelated code wrongly inherits the
suspended span as its ancestor -- confirmed directly (`tokio::join!` on a
single-threaded runtime, two independent async fns with different sleep
durations): the shorter one came back nested under the longer one.
Tracked via a second, GLOBAL structure, `suspended_stack` (name ->
stashed `(name, callOrder)`, shared across ALL threads, not per-thread like
`own_stack`): "exit" pops the span off `own_stack` (if genuinely on top)
and stashes it; "enter" restores it from the stash *unless* it's already
on top (the first enter, immediately following its own NEW -- confirmed
directly -- never needs restoring). "close" now resolves via the same
real-source-location lookup as NEW/ENTER/EXIT (confirmed all four carry
the same `filename`/`line_number`), checking the stash first, falling back
to the plain `own_stack.pop()` otherwise -- which is *all* that happens
when ENTER/EXIT isn't enabled at all (ordinary sync code, the overwhelming
majority), so this is fully backward compatible. The stash is deliberately
global, not per-thread: a multi-threaded runtime can migrate the *same*
task's own polls across different OS threads over its lifetime -- confirmed
directly on a scratch crate (`tokio::spawn`'d task, real scheduler pressure)
showing 3 distinct `threadId`s across one task's own ENTER/EXIT lines. A
per-thread stash (the original, mono-thread-only version) would stash the
suspended entry under the thread it exited on and never find it again once
resumed elsewhere; restoring by name only fixes this, pushing the restored
entry onto whichever thread's `own_stack` the resuming enter actually
happened on. `own_stack` itself stays per-thread, unchanged -- what's
genuinely active *right now* is always thread-local, unlike the suspended
stash. Known, accepted limitation (same category as before, just wider
scope): the exact same async fn/call-site invoked truly concurrently (not
just interleaved by suspension) could restore the wrong stashed tuple --
already possible in the per-thread version for same-thread concurrent
invocations, not a new risk.

`btn-step`'s click handler reads `traceIdx` synchronously but the actual
`stepTo()` call is delayed (`animPlay`, or a much longer return-path
flash) -- a `stepBusy` flag (declared with `traceIdx` up top) guards
against a rapid second click re-reading that still-stale `traceIdx` and
silently computing the same `nextIdx` again (confirmed as a real,
pre-existing bug via a rapid-click Playwright test: 4 quick clicks left
the trace stuck on the first step, unrelated to any of the fixes above --
would affect any trace, not just concurrent ones). `btn-step-prev` never
needed this -- it calls `stepTo()` synchronously, no delay to race.

### `src/codemap/viewer/index.html` — the pieces that aren't obvious from a skim

- `stepTo()`'s edge lookup can come up empty even for a span with a real,
  confirmed ancestor -- an untraced function in between means the trace
  correctly attributes the callee to its still-open ancestor, but no
  direct edge exists in `graph.json` for that pair (see
  `dapi::gap_demo -> untraced_relay -> gap_leaf` in dummy-lib). Handled by
  `cy.add()`-ing a synthetic edge (`gap-${src}->${tgt}`, `edgeType: 'gap'`)
  right there in the loop -- dashed, its own muted-grey color (never one of
  the real `call`/`dispatch`/`loop_call`/`trampoline` colors, so it can't
  be mistaken for a declared call), colored active/visited same as a real
  edge otherwise. Reused if the same gap recurs within one render pass;
  torn down by `clearAll()`'s `cy?.edges('[edgeType = "gap"]').remove()`
  so it never survives past the step that created it. A rejected
  alternative: traversing the static graph for a real multi-hop path
  instead -- correct here, but ambiguous in general (multiple real paths
  between the same two nodes would mean guessing which one was actually
  taken, which this tool has never done, see `implicit_parent`).
- `expandNeighborhood()` (static-view double-click) reveals any first-
  degree neighbor that "Hide untraced" had hidden, specifically for this
  one focused view (`revealedByExpansion`, same remember/restore pattern
  as `hiddenByExpansion`) -- `neighborhood.filter(':hidden')` finds them,
  `.show()`s them, `restoreExpansion()` `.hide()`s them again on the way
  out. Deliberately scoped to `node.closedNeighborhood()` (real, direct
  edges only) -- a node reached only *through* an untraced one (2 hops
  away) stays hidden; this reveals genuine first-degree relationships, not
  a wider "show everything nearby."
- `expandNeighborhood()` never reads a neighbor's "original" position via
  live `.position()` -- always via `homePositions`, a separately-
  tracked map updated only after `layoutComponents()`/a manual drag.
  Confirmed as a real, 100%-reproducible bug otherwise: Cytoscape fires
  `tap`, `tap`, THEN `dbltap` for a real double-click (not `dbltap` alone),
  and each `tap` already runs `focusOnNode()` -> `restoreExpansion()` --
  so double-clicking an already-expanded node again reads a neighbor's
  position while its own restore-to-original animation (started moments
  earlier by the first `tap`) is still mid-flight, silently adopting a
  transient position as the new "home." `cy.elements().stop(true, true)`
  right before the read does NOT fix this (verified directly) -- jump-to-
  end doesn't reliably apply to an animation that hasn't rendered a frame
  yet. Don't reintroduce a live-position read here even for a seemingly
  unrelated tweak.
- "Load graph…" / "Load doc index…" / "Load trace…" are plain
  `<input type="file">` + `FileReader` pickers — they read straight off
  disk client-side, no fetch involved. This is deliberate: it's what lets
  `src/codemap/viewer/` stay pure static assets with zero project-specific files ever
  needing to sit next to `index.html`, even though `graph`/`doc`/`trace`
  write their output outside this repo entirely (see the CLI section
  above). `loadGraph(g)` tears down (`cy.destroy()`) and rebuilds the whole
  Cytoscape instance, so loading a second, unrelated graph mid-session
  works cleanly. `fetch('graph.json')`/`fetch('source_index.json')` on page
  load still exist as a silent-fail convenience fallback, not the primary
  path — don't repurpose them into the main flow.
- A node's "traced" state and an edge's "edgeType" are read directly from
  `graph.json` data attributes; the legend is built dynamically from
  whatever states/edge-types are actually present in the loaded graph (see
  `buildLegend()`) rather than being a fixed list — keep it that way when
  adding new visual states. The legend itself lives in `#legend-panel`, a
  toolbar-triggered popover (`#btn-legend`, same `position: fixed` +
  `.open`-class toggle `#log-panel` already uses) — not a permanent
  sidebar block anymore, freeing that space for
  Execution Trace + the newer `#exec-context` panel below it.
- `#info` (left sidebar) is purely static now — doc link, signature,
  source link, doc comment, via `docEntryHtml()`/`buildInfoHtml()`,
  regardless of whether a trace is loaded. Anything about the *current
  replay step specifically* (duration, entry arguments, and the two
  internal-state-capture mechanisms below) renders in `#exec-context`
  (right sidebar) instead, via `buildExecContextHtml()` — the left/right
  sidebar split is deliberately "static info" vs. "execution info," not
  an arbitrary layout choice. A trace entry's
  `recordedFields` (one snapshot per invocation, from that invocation's
  own CLOSE — see trace_log.py above) and `events` (a list of
  `{message, fields}`, one per `tracing::event!`/`info!`/... call, across
  every invocation, in firing order) are both optional and only rendered
  when present.
- Playback (`stepTo`, `playStep`, `computeReturnPath`/`flashReturnPath`) is
  a replay-from-scratch model: `stepTo(idx)` always clears everything and
  rebuilds state for spans `0..idx`, it never mutates incrementally. Stepping
  backward is therefore just `stepTo(idx - 1)`.
- Reaching the last span of a trace looks like an ordinary "current" step
  (a separate violet "last-step" state existed once but never actually
  showed -- `finishTraceToRoot`, below, overwrote it to "visited" the
  moment you tried to go one step further, which is the natural next thing
  to do, so it was removed rather than fixed); a further `Step >` (or
  letting `Play` run out) triggers `finishTraceToRoot()`, which animates an
  unwind all the way back to the `main` node and only then settles that
  path to the normal "visited" green. `main` here is not a hardcoded
  project assumption for a *binary* — `fn main` is the mandated name of
  every Rust binary's entry point; since node ids are now crate-qualified,
  the actual id is `<bin crate>::main`, found dynamically via the shared
  `mainNodeId()` helper (`.endsWith('::main')`, not a literal `'main'`
  string compare anywhere) rather than hardcoded per call site. It is,
  however, a real gap for libraries: `finishTraceToRoot()` and
  `layoutAsLevelGrid()`'s root detection both still rely on that helper
  finding *some* `::main` node at all. The layout half is already
  conditional (falls back to Cytoscape's own auto-detected zero-indegree
  roots if `mainNodeId()` finds nothing); `finishTraceToRoot()` is not —
  replay on a library-derived graph remains unexplored territory, and is
  an accepted, permanent limitation now, not a gap to eventually close.
- Type names inside a rendered signature are linkified (`linkifySignature`)
  only when that exact identifier is a key in `source_index.json` — never
  from a hardcoded list of "known types."

## Design constraints (read before changing behavior)

- **No target-project mapping, anywhere.** If you're tempted to add a list
  of module names, type names, or file paths specific to some target crate,
  stop — that has been actively removed twice already.
  Prefer a structural signal derived from what rustc/cargo actually emit.
  A single, explicit, meaningful CLI parameter (like `--project`) is fine;
  a hidden internal list that has to be kept in sync is not.
- **This repo has no dependency on `be-quant`** (the outer repository that
  currently includes this repo as a git submodule, purely to keep a demo
  Rust crate for manual testing). Don't add path assumptions that only hold
  inside that outer repo's layout.
- Several things are intentionally *not yet* built (a dedicated
  call-stack/timing frame beyond the sidebar list; real rustdoc-style
  navigation instead of scraped signature/doc snippets) — deliberate open
  decisions, not gaps to silently fill in. Cross-crate node-id collision
  and MIR-as-the-extraction-source, previously listed here too, are now
  settled — see this file's `mir_graph.py`/`cargo test` sections above.
