"""trace_log.py -- Parse a `tracing_subscriber` JSON-lines log into replayable spans.

Expected input: one JSON object per line, as produced by
`tracing_subscriber::fmt::layer().json().with_span_events(FmtSpan::NEW | FmtSpan::CLOSE)`.
See README.md for the full format contract and current known limitations
(span names are used as the dedup key, so two call sites of the same
function currently collapse into one node).
"""
import json
import os
import re

_DURATION_RE = re.compile(r"([\d.]+)\s*([a-zµ]+)", re.IGNORECASE)


def parse_duration(s: str) -> float:
    """Parse a tracing `time.busy`-style duration string into microseconds."""
    s = s.strip()
    m = _DURATION_RE.match(s)
    if not m: return 0.0
    v, u = float(m.group(1)), m.group(2).lower()
    if "n" in u: return v / 1000.0
    if "µ" in s or "us" in u: return v
    if u.startswith("m"): return v * 1000.0
    if u.startswith("s"): return v * 1_000_000.0
    return v


def _norm_path(p: str) -> str:
    # os.path.normcase does the right platform-specific thing: lowercases +
    # backslash-normalizes on Windows (where the filesystem is case-
    # insensitive and rustc/tracing may report a different slash style than
    # doc_index.py's own path.resolve()), a no-op on case-sensitive POSIX --
    # don't lowercase unconditionally there, that would falsely conflate
    # genuinely distinct files.
    return os.path.normcase(os.path.normpath(p)) if p else p


def _build_line_index(source_index: dict) -> dict:
    """(normalized absolute file path, line) -> node id, from source_index.json's
    own per-node `absPath`/`line` (see doc_index.py) -- the graph side of the
    file+line reconciliation below."""
    index = {}
    for node_id, entry in source_index.items():
        abs_path, line = entry.get("absPath"), entry.get("line")
        if abs_path and line:
            index[(_norm_path(abs_path), line)] = node_id
    return index


def _scan_forward_to_code_line(filepath: str, attr_line: int) -> int | None:
    """`tracing`'s `#[instrument]` always reports the line the attribute
    itself starts on -- never the line of the `fn`/method it's attached to,
    regardless of whether the attribute spans multiple lines, is followed by
    other attributes, or has comments (line, block, or doc) between it and
    the item. Measured directly (a scratch crate covering all of those
    shapes) rather than assumed: the gap to the real item line ranged from 1
    to 11 lines even in small test cases, so a fixed tolerance window isn't
    viable -- this scans forward from `attr_line`, skipping exactly what
    Rust itself allows between an attribute and its item (blank lines,
    `//`/`///`/`//!` line comments, `/* */` block comments -- tracked across
    lines --, and further attributes -- tracked for their own possible
    multi-line `(`/`[` nesting), stopping at the first line that's actually
    code. That first code line is the true `fn`/method line, matching what
    `source_index.json` (via cargo doc's own source link) already records
    for that same item -- exactly, not approximately, in every shape tested.
    Returns None if the file can't be read or no code line is found (e.g. a
    macro-generated fn with no literal `fn` line at that location)."""
    try:
        with open(filepath, encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return None
    i = attr_line - 1  # attr_line is 1-indexed
    in_block_comment = False
    paren_depth = 0
    while i < len(lines):
        stripped = lines[i].strip()
        if in_block_comment:
            if "*/" in stripped:
                in_block_comment = False
            i += 1
            continue
        if paren_depth > 0:
            paren_depth += stripped.count("(") - stripped.count(")")
            paren_depth += stripped.count("[") - stripped.count("]")
            i += 1
            continue
        if not stripped or stripped.startswith("//"):
            i += 1
            continue
        if stripped.startswith("/*"):
            if "*/" not in stripped:
                in_block_comment = True
            i += 1
            continue
        if stripped.startswith("#[") or stripped.startswith("#!["):
            depth = stripped.count("(") - stripped.count(")") + stripped.count("[") - stripped.count("]")
            if depth > 0:
                paren_depth = depth
            i += 1
            continue
        return i + 1
    return None


def _find_main_id(graph: dict) -> str | None:
    """The one node, if any, whose id is this project's binary entry point
    (`crate::main`, or bare `main` as a defensive fallback for an older/
    unqualified graph) -- same reasoning as the viewer's own `isMainId()`:
    `fn main` is Rust's own mandated name for a binary's entry point, not a
    hardcoded project assumption, so this only ever matches something real.
    Returns None for a library-only graph (no such node exists there) --
    the caller already treats that as "nothing to do here"."""
    for n in graph.get("nodes", []):
        nid = n.get("data", n).get("id", "")
        if nid == "main" or nid.endswith("::main"):
            return nid
    return None


def _build_edge_call_orders(graph: dict) -> dict:
    """(caller id, callee id) -> sorted list of that pair's callOrder values,
    from graph.json's own edges (mir_graph.py assigns one per call site, see
    its own docstring). A pair with only one call site has a single-element
    list here -- deliberately not special-cased away, `_split_by_call_site`
    below treats "1 site" as the trigger for the old aggregate-into-one-
    entry behavior and "2+" as the trigger for splitting, so the graph is
    the single source of truth for which case applies, not a size check
    against however many times a name happened to repeat in this one trace."""
    m = {}
    for e in graph.get("edges", []):
        d = e.get("data", e)
        src, tgt, order = d.get("source"), d.get("target"), d.get("callOrder")
        if src and tgt and order is not None:
            m.setdefault((src, tgt), []).append(order)
    for k in m:
        m[k].sort()
    return m


def parse_trace(text: str, source_index: dict | None = None, graph: dict | None = None) -> list:
    """Returns a list of deduped span dicts: name/depth/stack/fields/iterations/
    duration_ms/callOrder (the last one only when the caller has more than
    one static call site to this callee -- see below).

    `source_index` (typically source_index.json's own contents), if given,
    resolves each span to its true graph node id via source location
    (file + the `#[instrument]` attribute's line, scanned forward to the
    real item line -- see _scan_forward_to_code_line) rather than trusting
    the span's own `name`. That trust would be misplaced even without any
    customization: a method's default instrumented name is just its bare
    name (no "Type::" qualifier), which never matches this tool's own
    "Type::method" node ids, and `#[instrument(name = "...")]` can rename
    it to anything at all. Falls back to the span's own name wherever
    nothing resolves (unreadable file, macro-generated fn, no
    `source_index` given at all) -- strictly additive, never regresses a
    name that already matched.

    Resolution is driven entirely by an explicitly maintained stack of
    currently-open spans, not by a raw-name -> resolved-id cache (an
    earlier version did this, and it was a real bug, not just a simplification:
    two different crates' functions can share the exact same *default*
    `#[instrument]` span name -- just the bare fn name, with no crate
    qualifier possible in that name at all -- e.g. two crates each with
    their own `make_and_describe`, confirmed on the dummy-lib/dummy-cli
    fixture's own cross-crate-collision test. A cache keyed by that bare
    name can only ever hold one resolved id per name, so the second crate's
    occurrence silently inherited the first's -- collapsing exactly the
    distinction crate-qualified node ids exist to preserve). Each "new"
    event resolves independently from its OWN `filename`/`line_number`
    (never reused across entries), then its id is pushed onto the open-span
    stack; a "close" event, by construction, always corresponds to
    whatever is currently on TOP of that stack -- tracing's span guards are
    RAII, so spans nest like a real call stack for ordinary synchronous,
    single-threaded execution (already this tool's whole documented scope,
    see README.md's "Known limitations") -- so a close is resolved by
    *popping*, never by re-deriving from its own (ambiguous, name-only)
    `spans`/`span.name` text at all. The `stack` recorded on each event is
    simply a snapshot of the open-span stack at that moment, for the same
    reason: it's the actual ancestor chain, not a name lookup that could
    collide the same way.

    `graph` (typically graph.json's own contents), if given, additionally
    tells genuinely different call sites of the same callee apart from one
    call site simply repeating in a loop -- a real, deliberate distinction,
    not the same problem as name resolution above. A repeated call from a
    single site (a loop) still collapses into one entry with an iteration
    count, as before. A callee reached from N>1 *different* static call
    sites of the same caller instead produces up to N separate entries, one
    per site, each carrying that site's own `callOrder` -- occurrences are
    assigned to sites in the order both appear (1st occurrence of this
    (caller, callee) pair -> the site with the smallest callOrder, 2nd ->
    the next, ...), wrapping back to the first site if there are more
    occurrences than sites (e.g. one of several sites is itself in a loop)
    -- an approximation in that specific mixed case (which site actually
    looped isn't recoverable from the trace alone), but still correctly
    splits into the right *number* of distinct sites rather than collapsing
    them all back into one, which is the point.

    `graph` also fixes a *specific*, narrow case of a broader gap: `main`
    itself is never a tracing span (it can't usefully be #[instrument]'d --
    the span would start before `tracing_subscriber::init()`, called from
    inside main's own body, has even run), so a function main calls
    directly gets NO recorded ancestor at all -- its own `stack` comes out
    empty, indistinguishable from a genuine second root. This was first
    "fixed" by unconditionally treating every ancestor-less span as a
    child of `main` -- rejected on review: that's a guess, not a fact, and
    a real one it could get wrong -- a span reached through some *other*,
    also-untraced intermediate function (main -> helper -> this_span, with
    `helper` itself never instrumented) would be mislabeled as called
    directly by main, an edge that might not even exist in the static
    graph. Fixed properly instead: an ancestor-less span is only ever
    attributed to `main` when `graph` *confirms* a direct static edge from
    main to it (`_find_main_id` + a lookup in `edge_call_orders`) -- never
    as a default. No graph, or no confirming edge, and the span's `stack`
    stays exactly what it always was: empty, not a guess.
    """
    line_index = _build_line_index(source_index) if source_index else {}
    edge_call_orders = _build_edge_call_orders(graph) if graph else {}
    main_id = _find_main_id(graph) if graph else None
    scan_cache = {}  # (norm path, attr line) -> resolved line or None, this parse only

    def implicit_root_parent(name):
        """Only `main` when the static graph proves main really does call
        `name` directly -- see this function's own docstring above."""
        if main_id and (main_id, name) in edge_call_orders:
            return main_id
        return None

    def resolve_id(filename, attr_line):
        if not filename or not attr_line:
            return None
        key = (_norm_path(filename), attr_line)
        if key not in scan_cache:
            scan_cache[key] = _scan_forward_to_code_line(filename, attr_line)
        resolved_line = scan_cache[key]
        if resolved_line is None:
            return None
        return line_index.get((_norm_path(filename), resolved_line))

    entries = []
    for line in text.splitlines():
        line = line.strip()
        if not line: continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        raw_name = entry.get("span", {}).get("name", "")
        if not raw_name: continue
        entries.append(entry)

    # Which occurrence (0-indexed) of this (parent, name) pair we're up to,
    # tracked separately for "new" and "close" events -- both advance once
    # per matching entry, in the same relative order tracing itself emitted
    # them in (spans nest properly, so a given invocation's own new/close
    # pair keeps the same rank among same-named siblings either way), which
    # is what lets a close event's aggregated duration land back on the
    # same split-out entry its own new event produced.
    new_occurrence = {}
    close_occurrence = {}

    def call_order_for(parent, name, counters):
        sites = edge_call_orders.get((parent, name))
        if not sites or len(sites) <= 1:
            return None
        idx = counters.get((parent, name), 0)
        counters[(parent, name)] = idx + 1
        return sites[idx % len(sites)]

    open_stack = []  # resolved ids of currently-open spans, outermost first
    new_events = []
    close_stats = {}  # (resolved name, callOrder or None) -> {count, total_us}
    for entry in entries:
        fields = entry.get("fields", {})
        span_info = entry.get("span", {})
        raw_name = span_info.get("name", "")

        if "time.busy" in fields:
            # A close always matches whatever's currently innermost -- see
            # this function's own docstring for why that's reliable and a
            # name-based lookup here would not be.
            name = open_stack.pop() if open_stack else raw_name
            parent = open_stack[-1] if open_stack else implicit_root_parent(name)
            call_order = call_order_for(parent, name, close_occurrence)
            us = parse_duration(fields.get("time.busy", "0"))
            st = close_stats.setdefault((name, call_order), {"count": 0, "total_us": 0.0})
            st["count"] += 1
            st["total_us"] += us
        else:
            resolved = resolve_id(entry.get("filename"), entry.get("line_number"))
            name = resolved or raw_name
            stack = list(open_stack)
            if not stack:
                # Confirmed-by-the-static-graph case only -- see
                # implicit_root_parent's docstring. Doesn't touch
                # `open_stack` itself (that stays a pure record of what
                # tracing actually recorded); this only affects what THIS
                # one span reports as its own ancestor.
                confirmed = implicit_root_parent(name)
                if confirmed:
                    stack = [confirmed]
            parent = stack[-1] if stack else None
            call_order = call_order_for(parent, name, new_occurrence)
            ev = {
                "name": name,
                "depth": len(stack),
                "stack": stack,
                "fields": {k: v for k, v in span_info.items() if k != "name"},
            }
            if call_order is not None:
                ev["callOrder"] = call_order
            new_events.append(ev)
            open_stack.append(name)

    seen, deduped = set(), []
    for ev in new_events:
        key = (ev["name"], ev.get("callOrder"))
        if key in seen: continue
        seen.add(key)
        st = close_stats.get(key, {"count": 1, "total_us": 0.0})
        ev["iterations"] = st["count"]
        ev["duration_ms"] = round(st["total_us"] / 1000.0, 4)
        ev["total_ms"] = ev["duration_ms"]
        deduped.append(ev)
    return deduped
