# How it works

## Multi-crate merging

If the target crate depends on other crates that live locally (path
dependencies — e.g. sibling members of the same cargo workspace), their
call-graphs are merged in automatically: `build` resolves the target's
own transitive dependency graph via `cargo metadata`, compiles each local
dependency with `RUSTFLAGS=--emit=mir`, and merges every one's MIR into a
single graph (and docs every one of them too, so cross-references resolve
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
text, though. Resolution order for a plain (non-`&dyn Trait`) call:

1. MIR gave an explicit crate qualifier — resolve against that crate's
   own known ids only.
2. No qualifier — check the caller's own crate first (so a name existing
   in both the caller's own crate and elsewhere always resolves to the
   caller's own crate, never a coincidental match elsewhere).
3. Still nothing — search every other crate's known ids. Exactly one
   match resolves there. Two or more matches are genuinely ambiguous (a
   *third* crate calling a method name two *other* crates both happen to
   share, with no qualifier of its own to disambiguate by) and fan out to
   every match, the same over-approximation `&dyn Trait` calls already
   use.

## How the call-graph is built

`src/mir_graph.rs` parses MIR text (produced via `cargo build` with
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
  look external. Not yet handled.
- **"traced" flag**: each node records whether `#[instrument]`/`span!`/
  `event!` actually expanded into it (detected via the tracing crate's
  `Callsite` scaffolding appearing in its MIR body) — a purely structural
  signal, independent of any particular run, so it isn't fooled by "this
  input just didn't take that branch".
