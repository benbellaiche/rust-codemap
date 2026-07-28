# Using the viewer

Once `cargo codemap run` has opened the viewer in a browser, this is what
you can do with it.

## Replaying a real execution

> Currently exercised against binaries only — the replay animation unwinds
> back to a node named `main` at the end of a trace, which only a binary's
> entry point is guaranteed to have. Not yet adapted for library crates;
> see [limitations.md](limitations.md).

Run the target binary once (it must emit logs per the format in
[tracing-format.md](tracing-format.md), to some file, e.g.
`trace_output.jsonl`), then use **"Load trace…"** in the viewer to pick
that file directly — no CLI step needed, no separate command to convert it
first. The raw log is parsed server-side (`trace_log.rs`, via a
`/__codemap_parse_trace` endpoint); with no server to ask (e.g.
`index.html` opened straight off disk) it falls back to an equivalent
parser in the viewer itself.

Hit **Play** (or **Step >**) in the viewer to replay the run.

## Doc-driven graph focus

A crate with thousands of functions renders as an unreadable wall of nodes
if you just dump the whole call-graph at once (see
[limitations.md](limitations.md)). The left-hand **"Public API (doc
index)"** panel is the way around that: once `source_index.json` is loaded
(auto-loaded by `cargo codemap run`), it lists every `cargo doc`-documented
item, grouped by crate then by class/type (both come straight from
`doc_index.rs`'s output — no new mapping: a method's class is the `Type`
half of its `Type::method` node id, a free fn has none, and a type's own
entry acts as its own class heading, which is why its methods land right
under it).

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
   needs the doc HTML actually served, which `cargo codemap run` wires up
   automatically. Without a matching `docPage`, the button just stays
   disabled instead of opening a broken link.

An entry that isn't a node in the currently loaded graph (e.g. a struct's
own doc page — the type itself isn't a call-graph node, only its methods
are) still opens its native doc page, but can't pan the graph to it
(reported in the info panel, alongside its signature and source-file link
— same info any other entry gets, not a silent no-op).

Private (non-`pub`) items are always documented (`cargo doc
--document-private-items` runs unconditionally — a private
`#[instrument]`'d function still needs a doc-index entry or its span can't
resolve during replay), but stay hidden in the doc list by default; the
**"Show private"** button next to "Hide untraced" reveals them, same as
untraced nodes, and stays hidden itself if there's nothing for it to
affect. `main`'s own effective visibility is genuinely `pub(crate)`
(a binary has no external API), but it's exempted from this toggle — a
binary's entry point isn't "someone's private helper," it's always shown.

## Navigating a large graph by hand

Every edge carries a small number at its midpoint: that's `callOrder`, the
position of that call in its caller's own source (see
[architecture.md](architecture.md) for how it's derived — it's a static,
MIR-order approximation, not a guarantee about real execution order for
code with branches or loops). This is the *static* navigation layer — it's
a deliberately separate thing from replaying a trace (above); loading one
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

## The version badge

The small monospace text next to the "Rust Codemap" title (e.g.
`js:15:08:47 · trace_log.rs:15:01:44 · main.rs:15:07:25 · pid 744`) is
not decorative — it's the actual, on-disk last-modified time of the files
this specific server process is serving right now, fetched fresh on every
page load from `GET /__codemap_version` (never cached, never a hand-
maintained version number). If you've edited `trace_log.rs` or
`main.rs`, this won't reflect it until the server process itself is
restarted (compiled once at build time) — `index.html`'s own timestamp,
by contrast, updates on a plain page reload, since it's served fresh from
disk on every request. Added after a stale server process answered
requests with old code for hours despite looking freshly restarted.
