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

Read [README.md](README.md) for the user-facing workflow and the current
tracing log format. Read [PROJECT.md](PROJECT.md) for design history: what's
already settled, what's still an open decision, and known limitations —
check it before re-deciding something that was already discussed, and
update it (not just this file) when a real decision is made.

## Commands

Run from the repo root. `--project <path>` always points at some *other*
Rust crate on disk (there is no target crate committed to this repo).

```sh
python -m codemap run   --project <path>                # graph + doc + serve, one shot -- auto-loads in the viewer, no clicks
python -m codemap graph --project <path>                 # -> <target_dir>/rust-codemap/<crate>/graph.json
python -m codemap doc   --project <path>                  # -> <target_dir>/rust-codemap/<crate>/source_index.json
python -m codemap serve --graph <path> --doc <path> [--docs <path>]  # re-serves already-generated output, re-read on every request
```

No `trace` subcommand anymore -- removed once "Load trace…" in the viewer
started accepting a raw log directly (parsed server-side via
`/__codemap_parse_trace`, which still calls `trace_log.py`) and the other
subcommands' own manual-load fallback (the old "Load graph…"/"Load doc
index…" buttons) was removed too, in favor of the `--graph`/`--doc` auto-
load above -- keeping a fourth, CLI-only, now-redundant path felt
inconsistent once neither of those applied to it anymore. `serve` with no
`--graph`/`--doc` now serves an empty shell: there is no other way to get
a graph/doc index into the viewer.

No `--bin`/`--lib`: dropped entirely once MIR generation stopped needing
cargo told which target to build (see "Multi-crate merging" below) — the
flag became purely vestigial and keeping it would just be clutter.
`<target_dir>` is the *target* crate's own `cargo metadata` target
directory, not anything under this repo — see "Design constraints" below.
`<crate>` is that crate's *actual compiled* name, via `crate_name()` --
**not** simply the package name with hyphens underscored: a `[lib] name`
override in Cargo.toml changes it, and assuming otherwise silently drops
that crate from the graph (real bug, see PROJECT.md §3 bug #6; `crate_name()`
checks the `[lib]` table first, falls back to the package-name derivation
only if there's no override). This subdirectory is **required** even for a
single-crate project: every member of a workspace shares one `target_dir`,
so without it, two crates in the same workspace would overwrite each
other's output at the same path (this happened too; see PROJECT.md §2.4
"Second round"). The viewer never
fetches project-specific files automatically as the primary path; use its
"Load graph…" / "Load doc index…" / "Load trace…" buttons to pick up
whatever `graph`/`doc`/`trace` (or `run`) just wrote.

There is no test suite yet. To sanity-check a change to `codemap/`, run the
commands above against:
- `../dummy-lib/{dummy-core,dummy-ops,dummy-api}` — a sibling repo
  purpose-built as a multi-crate (library-only) test fixture, see
  PROJECT.md §2.7 and its own README. Confirm `graph`/`doc` merge all 3
  crates when pointed at `dummy-api`, but only `{dummy-ops, dummy-core}`
  when pointed at `dummy-ops` (dummy-api depends on dummy-ops, not the
  reverse -- it must NOT show up).
- `../dummy-cli` — a standalone binary depending on `dummy-lib/dummy-api`
  by path, kept *outside* the dummy-lib workspace on purpose. Pointing at
  it should resolve to `{dummy-cli, dummy-api, dummy-ops, dummy-core}`;
  pointing at any `dummy-lib` crate should never pull `dummy-cli` in (it
  depends on them, they don't depend on it) -- this exact regression
  happened once already (PROJECT.md §3, bug #4). Also useful for the
  `[lib] name` case below, since `dummy-cli` itself has no override while
  everything it depends on does.
- All three `dummy-lib` crates additionally have a `[lib] name` override
  in their `Cargo.toml` (different from their package name) -- confirm
  `graph`/`doc` still find their `.mir`/doc output under the *overridden*
  name, not a name derived from the package name (PROJECT.md §3, bug #6).

For execution replay, use a real instrumented binary crate (e.g.
`../sandbox/tools-codemap` in the outer repo -- but see PROJECT.md §2.7 for
a caveat about that one's own workspace). Then open the viewer, use the
"Load…" buttons, and check the graph renders and Play/Step works. Note
execution replay is only meaningful for a binary target today (see
"Design constraints").

## Architecture

### Two independent halves

1. **`codemap/`** — a Python package, zero third-party dependencies
   (`requirements.txt` is deliberately empty). `__main__.py` is the CLI
   dispatcher (`python -m codemap <subcommand>`); `mir_graph.py`,
   `doc_index.py`, `trace_log.py` are pure parsing modules with no CLI/IO
   concerns of their own — each takes text/paths in and returns a plain
   dict/list out, so they're easy to reason about independent of argparse.
2. **`viewer/index.html`** — a single self-contained HTML file (Cytoscape.js
   plus the `cytoscape-dagre` layout extension and its `dagre` dependency,
   all from a CDN via plain `<script>` tags, no build step, no bundler,
   registered with `cytoscape.use(cytoscapeDagre)`) that fetches `graph.json` /
   `source_index.json` / `trace.json` from its own directory. The Python
   side and the viewer only communicate through those three JSON files —
   there is no other coupling.

### `codemap/__main__.py` — multi-crate dependency resolution

`local_dependency_closure()` is the piece that decides *which* crates get
merged into the graph. It is deliberately **not** "every member of the
workspace the target crate happens to live in" (`cargo metadata --no-deps`)
— that was the first design, and it was wrong: it swept in unrelated
sibling crates and, worse, client binaries that depend **on** the target
crate rather than the other way around (see PROJECT.md §3 bug #4, and
`dummy-cli` in §2.7, which exists specifically to catch a regression here).
Instead it calls `cargo metadata` **without** `--no-deps` to get the
dependency-resolution graph (`resolve.nodes[].deps`), then walks only the
*forward* edges from the target's own node, keeping just the `path+file://`
(local) ones. `cmd_graph` and `cmd_doc` both use this same closure — do not
let them drift apart; `cmd_doc` in particular needs, per crate in the
closure, that crate's *own* `src/` directory for resolving source links,
not the target crate's (`doc_index.build_index()` takes a list of
`(doc_root, src_root)` pairs for exactly this reason).

### `codemap/mir_graph.py` — how the call-graph is actually extracted

This is the part most likely to need care when changed. It parses MIR
*text* (produced via `cargo build -p <crate> ...` with
`RUSTFLAGS=--emit=mir`, one invocation per crate in the dependency closure
above, texts simply concatenated before parsing) with regexes — there is no
AST and no dependency on rustc's internals beyond the stability of its MIR
pretty-printer output. Load-bearing assumptions (all rely on rustc
behavior, not on any specific project):

- Free functions defined in the crate being compiled are always **local**
  (anything from another crate is fully path-qualified with that other
  crate's name) -- but MIR is *inconsistent* about whether it prints a
  local free function's own module path or not (`basics::add` right next
  to bare `compute`, both `pub fn` in their own submodule -- confirmed via
  the dummy-lib fixture, PROJECT.md §2.7). `RE_FREE_FN` and `normalize_call`
  both normalize to the bare name either way, so don't assume "bare = safe
  to match, qualified = something else" anywhere new.
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
  fan-out edges share the same `call_order`.
- Closures compile to their own top-level MIR item
  (`...::{closure#N}`). Calls made inside a closure body are reattributed to
  the *enclosing* function (`closure_owner_path`), so a call hidden inside
  `.map(...)` still produces an edge from the right node.
- Calls through `&dyn Trait` are over-approximated: MIR can't tell which
  concrete type is behind the pointer, so the edge fans out to *every*
  locally-known implementation of that method. This is a deliberate
  simplification, not a bug — expect it to look noisy on a project with a
  large trait hierarchy.
- The `traced` flag on each node is derived by checking whether the
  function's MIR body contains the tracing crate's `Callsite` scaffolding —
  i.e. whether `#[instrument]`/`span!`/`event!` actually expanded there.
  This is deliberately *not* based on any particular execution's trace
  file, because a single run only exercises the branches its input took;
  `traced == false` means "structurally can never produce a span," which is
  a stronger and more useful statement than "didn't show up in this trace."

### `codemap/doc_index.py` — cross-referencing cargo doc

Discovers doc pages purely by filename pattern
(`struct.Name.html`/`enum.Name.html`/`trait.Name.html`/`fn.name.html`) under
`target/doc/<crate>/`, keyed by the item's own name — no manual
name-to-file table. `SOURCE_LINK_RE` matches `.../src/<any-crate-name>/...`
generically; don't hardcode a crate name into this regex again (it was
found and fixed once already — see PROJECT.md §7).

### `codemap/trace_log.py` vs. the viewer's inline JS parser

The exact same tracing-log parsing logic (dedup by span name, `time.busy`
duration parsing, iteration counting) is implemented twice: once here in
Python, once as `parseTraceJsonl()` inside `viewer/index.html` (used by the
"Load trace…" file picker, so a raw log can be loaded without invoking the
CLI first). This duplication is known and tracked in PROJECT.md, not an
oversight — if you fix a bug in one, check whether it also applies to the
other.

### `viewer/index.html` — the pieces that aren't obvious from a skim

- "Load graph…" / "Load doc index…" / "Load trace…" are plain
  `<input type="file">` + `FileReader` pickers — they read straight off
  disk client-side, no fetch involved. This is deliberate: it's what lets
  `viewer/` stay pure static assets with zero project-specific files ever
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
  adding new visual states.
- Playback (`stepTo`, `playStep`, `computeReturnPath`/`flashReturnPath`) is
  a replay-from-scratch model: `stepTo(idx)` always clears everything and
  rebuilds state for spans `0..idx`, it never mutates incrementally. Stepping
  backward is therefore just `stepTo(idx - 1)`.
- Reaching the last span of a trace is a distinct state ("last-step",
  styled violet) from an ordinary "current" step; a further `Step >` (or
  letting `Play` run out) triggers `finishTraceToRoot()`, which animates an
  unwind all the way back to the `main` node and only then settles that
  path to the normal "visited" green. `main` here is not a hardcoded
  project assumption for a *binary* — `fn main` is the mandated name of
  every Rust binary's entry point. It is, however, a real gap for
  libraries: `finishTraceToRoot()` and the layout's `roots: ['main']` both
  still key off that exact name. The layout half is already conditional
  (`initCy`/`btn-relayout` check whether a `main` node exists before using
  it as the root, falling back to Cytoscape's own auto-detected roots
  otherwise); `finishTraceToRoot()` is not — replay on a library-derived
  graph is unexplored territory, see PROJECT.md §4.
- Type names inside a rendered signature are linkified (`linkifySignature`)
  only when that exact identifier is a key in `source_index.json` — never
  from a hardcoded list of "known types."

## Design constraints (read before changing behavior)

- **No target-project mapping, anywhere.** If you're tempted to add a list
  of module names, type names, or file paths specific to some target crate,
  stop — that has been actively removed twice already (see PROJECT.md §7).
  Prefer a structural signal derived from what rustc/cargo actually emit.
  A single, explicit, meaningful CLI parameter (like `--project`) is fine;
  a hidden internal list that has to be kept in sync is not.
- **This repo has no dependency on `be-quant`** (the outer repository that
  currently includes this repo as a git submodule, purely to keep a demo
  Rust crate for manual testing). Don't add path assumptions that only hold
  inside that outer repo's layout.
- Several things are intentionally *not yet* built (a dedicated
  call-stack/timing frame beyond the sidebar list; real rustdoc-style
  navigation instead of scraped signature/doc snippets) — these are open
  decisions in PROJECT.md §4, not gaps to silently fill in.
