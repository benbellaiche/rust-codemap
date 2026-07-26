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

    Each entry also carries `recordedFields` and `events` when present --
    the two mechanisms for seeing a function's own internal state, not
    just its entry arguments (see README.md's "Tracing log format" for the
    exact code to inject):
    - `recordedFields`: one dict per invocation (`fields`, above, only ever
      reflects the FIRST invocation's own entry args), taken from that
      invocation's own CLOSE event -- which reports the span's *current*
      known fields at that point, not its entry-time ones. A plain
      `#[instrument]` never changes after entry, so this is usually
      identical to `fields` -- it only differs when the function declares
      an initially-empty field (`#[instrument(fields(x = tracing::field
      ::Empty))]`) and fills it in later via `tracing::Span::current()
      .record("x", value)`, which is the whole point: capturing a variable
      that didn't exist yet when the span was entered.
    - `events`: any `tracing::event!`/`info!`/... call made from directly
      inside the function body, in the order they fired, across every
      invocation -- `{"message", "fields"}` each. Unlike `record()` above
      (which only ever shows the LATEST value for a given field), a
      separate event line naturally captures a whole *progression* of
      values across multiple points in the function, not just one.

    Both need the line-classification fix described next to be usable at
    all: a bare event line's own `span.name` is just its ENCLOSING span's
    name (confirmed empirically -- an event doesn't have an identity of
    its own) -- indistinguishable from a genuine NEW event by name alone.
    Classification is by `fields.message` instead (a NEW/CLOSE line's own
    `message` is always exactly "new"/"close", a fixed tracing_subscriber
    marker -- see the JSON schema in codemap/schema/): CLOSE first
    (`"time.busy" in fields`, unchanged), then `message == "new"`, then
    anything else is an EVENT. Getting this wrong is not cosmetic: before
    this fix, a bare event fell into the same branch as a genuine NEW,
    pushing its enclosing span's name onto `open_stack` a SECOND time --
    never popped back off (only the real CLOSE pops, once), permanently
    corrupting the depth/ancestor chain of every span parsed afterward.
    Confirmed directly: added a scratch `tracing::info!()` call inside an
    instrumented function, and every span after it in the trace came back
    one level deeper than it actually was. `open_stack` itself now holds
    `(name, callOrder)` pairs, not bare names -- letting an EVENT look up
    its enclosing span's own dedup key directly (`open_stack[-1]`) rather
    than needing a second, independently-advancing occurrence counter to
    stay in sync with the one NEW already used to assign that callOrder --
    CLOSE reads its own call_order the same way now (by popping it back
    off the stack), rather than recomputing it via a separate counter that
    only worked by relying on NEW and CLOSE always advancing in lockstep.
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

    # Which occurrence (0-indexed) of this (parent, name) pair we're up to --
    # only ever needed for NEW (where a callOrder is actually assigned);
    # CLOSE and EVENT both read it back off `open_stack` instead of
    # tracking their own parallel counter (see this function's own
    # docstring for why a second counter was the wrong fix once EVENT
    # lines needed the exact same lookup CLOSE already did).
    new_occurrence = {}

    def call_order_for(parent, name):
        sites = edge_call_orders.get((parent, name))
        if not sites or len(sites) <= 1:
            return None
        idx = new_occurrence.get((parent, name), 0)
        new_occurrence[(parent, name)] = idx + 1
        return sites[idx % len(sites)]

    open_stack = []  # (resolved name, callOrder) pairs, outermost first
    new_events = []
    close_stats = {}  # (resolved name, callOrder or None) -> {count, total_us}
    recorded_by_key = {}  # same key -> [close-time span-field dict, ...], one per invocation
    events_by_key = {}  # same key -> [{"message", "fields"}, ...], across every invocation
    for entry in entries:
        fields = entry.get("fields", {})
        span_info = entry.get("span", {})
        raw_name = span_info.get("name", "")
        message = fields.get("message")

        if "time.busy" in fields:
            # A close always matches whatever's currently innermost -- see
            # this function's own docstring for why that's reliable and a
            # name-based lookup here would not be. Its callOrder is
            # whatever NEW already assigned this exact invocation --
            # popped back off the stack, not recomputed.
            if open_stack:
                name, call_order = open_stack.pop()
            else:
                name, call_order = raw_name, None
            key = (name, call_order)
            us = parse_duration(fields.get("time.busy", "0"))
            st = close_stats.setdefault(key, {"count": 0, "total_us": 0.0})
            st["count"] += 1
            st["total_us"] += us
            # The close event's own `span` reports this invocation's
            # *current* fields, which is where a value filled in mid-body
            # via Span::current().record(...) actually shows up -- entry
            # args (below, in the NEW branch) never change after the fact.
            recorded_by_key.setdefault(key, []).append(
                {k: v for k, v in span_info.items() if k != "name"}
            )
        elif message == "new":
            resolved = resolve_id(entry.get("filename"), entry.get("line_number"))
            name = resolved or raw_name
            stack = [nm for nm, _co in open_stack]
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
            call_order = call_order_for(parent, name)
            ev = {
                "name": name,
                "depth": len(stack),
                "stack": stack,
                "fields": {k: v for k, v in span_info.items() if k != "name"},
            }
            if call_order is not None:
                ev["callOrder"] = call_order
            new_events.append(ev)
            open_stack.append((name, call_order))
        else:
            # EVENT: a plain tracing::event!/info!/... call from directly
            # inside the currently-innermost span's own body -- not a span
            # itself (never pushed onto open_stack, never assigned its own
            # callOrder), just attached to whatever IS currently innermost.
            if open_stack:
                enclosing_key = open_stack[-1]
                events_by_key.setdefault(enclosing_key, []).append({
                    "message": message or "",
                    "fields": {k: v for k, v in fields.items() if k != "message"},
                })

    seen, deduped = set(), []
    for ev in new_events:
        key = (ev["name"], ev.get("callOrder"))
        if key in seen: continue
        seen.add(key)
        st = close_stats.get(key, {"count": 1, "total_us": 0.0})
        ev["iterations"] = st["count"]
        ev["duration_ms"] = round(st["total_us"] / 1000.0, 4)
        ev["total_ms"] = ev["duration_ms"]
        recorded = recorded_by_key.get(key)
        if recorded:
            ev["recordedFields"] = recorded
        events = events_by_key.get(key)
        if events:
            ev["events"] = events
        deduped.append(ev)
    return deduped
