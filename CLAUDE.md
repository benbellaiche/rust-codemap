# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

An interactive call-graph viewer for Rust codebases, with execution replay
from real `tracing` logs. It is a standalone, generic tool: it must work
against **any** Rust binary crate, with zero hardcoded knowledge of that
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
python -m codemap run   --project <path> (--bin <name> | --lib)   # graph + doc + serve, one shot
python -m codemap graph --project <path> (--bin <name> | --lib)   # cargo rustc --emit=mir -> <target_dir>/rust-codemap/graph.json
python -m codemap doc   --project <path>                          # cargo doc -> <target_dir>/rust-codemap/source_index.json
python -m codemap trace --input <path-to-jsonl-log>                # -> trace.json (or <target_dir>/rust-codemap/ with --project)
python -m codemap serve                                             # http://localhost:8787/, serves viewer/ ONLY (no project data)
```

`--bin <name>` and `--lib` are mutually exclusive (argparse enforces this).
`<target_dir>` is the *target* crate's own `cargo metadata` target
directory, not anything under this repo — see "Design constraints" below.
The viewer never fetches project-specific files automatically as the
primary path; use its "Load graph…" / "Load doc index…" / "Load trace…"
buttons to pick up whatever `graph`/`doc`/`trace` (or `run`) just wrote.

There is no test suite yet. To sanity-check a change to `codemap/`, run the
commands above against a real instrumented Rust crate for `--bin` (any
crate with `#[instrument]`/`tracing` spans works) and a plain library crate
for `--lib` (there is no bundled fixture crate in this repo) and confirm
`graph`/`doc`/`trace` produce non-empty, sane output, then open the viewer,
use the "Load…" buttons, and check the graph renders and Play/Step works.
Note execution replay is only meaningful for `--bin` today (see
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
   from a CDN, no build step, no bundler) that fetches `graph.json` /
   `source_index.json` / `trace.json` from its own directory. The Python
   side and the viewer only communicate through those three JSON files —
   there is no other coupling.

### `codemap/mir_graph.py` — how the call-graph is actually extracted

This is the part most likely to need care when changed. It parses the
*text* output of `cargo rustc --bin <name> -- --emit=mir` with regexes —
there is no AST and no dependency on rustc's internals beyond the stability
of its MIR pretty-printer output. Load-bearing assumptions (all rely on
rustc behavior, not on any specific project):

- Free functions defined in the crate being compiled are printed **bare**
  (`fn name(...)`, no `::`), while anything from another crate is always
  fully path-qualified. This is what lets local free functions be
  recognized without knowing the crate's name.
- A module-qualified impl method (`fn mod::<impl at PATH:...>::name(...)`)
  is treated as local **unless** `PATH` resolves through cargo's dependency
  or toolchain caches (`is_local_impl`, checked via `.cargo`/`registry`/
  `.rustup`/`toolchains` substrings). This replaced an earlier hardcoded
  list of "our" module names — do not reintroduce a name-based allowlist
  here; extend the path-based heuristic instead. Known gap: `cargo vendor`
  dependencies aren't excluded by this (their path doesn't look external).
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
