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


def _build_callers(graph: dict) -> dict:
    """callee id -> set of distinct caller ids, from graph.json's own edges
    (mir_graph.py, ground truth from MIR -- not this trace). Deliberately
    keyed on distinct *source*, not (source, callOrder): two call sites in
    the same caller still collapse to one entry here, since what matters
    for `implicit_parent` below is only "how many different functions could
    possibly have called this one at all", not how many times."""
    callers = {}
    for e in graph.get("edges", []):
        d = e.get("data", e)
        src, tgt = d.get("source"), d.get("target")
        if src and tgt:
            callers.setdefault(tgt, set()).add(src)
    return callers


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
    one static call site to this callee -- see below)/openSeq/closeSeq.

    `openSeq`/`closeSeq` are this invocation's own NEW/CLOSE line's position
    in the raw log (0-indexed, counting every line -- NEW, CLOSE, and EVENT
    alike), not anything derived from the resolved `new_events` list. They
    exist for one reason: the viewer's replay steps through spans in NEW
    order, and for ordinary synchronous, single-threaded code that order
    already IS real close order too (RAII guarantees a span's own CLOSE
    always comes before the next sibling's NEW) -- but that guarantee breaks
    for concurrent code. `concurrent_demo` calling `thread_b`/`thread_c` (see
    the `open_stacks` paragraph below) doesn't itself CLOSE until after BOTH
    threads have closed, since it's blocked on `.join()` -- its own CLOSE
    line comes last in the log, well after `thread_b`'s and `thread_c`'s NEW
    lines. Confirmed as a real, visible replay bug, not a theoretical one:
    without this, the viewer decided "already returned" purely from step
    index (has this span's NEW been stepped past), which colored
    `main -> concurrent_demo` green -- implying it had already returned --
    the moment replay reached `thread_b`, even though `concurrent_demo` was
    still genuinely running at that point. Comparing `closeSeq` (this
    span's) against another span's `openSeq` (whatever's currently being
    stepped to) tells the viewer whether that's actually true instead of
    assuming it from index order alone.

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

    Optional `"enter"`/`"exit"` lines (`FmtSpan::ENTER | FmtSpan::EXIT`,
    opt-in on the target's own subscriber setup, never emitted otherwise)
    relax the "ordinary synchronous, single-threaded execution" assumption
    above for one specific, real case: `async fn`. A sync fn's span is
    entered once (right after its own NEW) and never exited again until
    CLOSE, so ENTER/EXIT simply don't fire for it at all even when enabled
    -- nothing changes there. An `async fn` genuinely can be entered and
    exited multiple times across its own lifetime, once per executor poll,
    and a real `.await` suspension means the executor is free to run
    something else *entirely unrelated* on this same thread in the
    meantime -- without tracking this, that unrelated code would wrongly
    inherit the suspended span as its own ancestor (`own_stack` has no
    concept of "open but not actually active right now"). Confirmed as a
    real, reproducible bug on a scratch single-threaded-runtime crate: two
    independent `#[instrument]` async fns run via `tokio::join!`, one with
    a longer `sleep` than the other -- the shorter one came back attributed
    to the longer one (`stack: ["<the longer one>"]`) even though they're
    genuinely concurrent, not nested at all.

    Fixed by tracking a *second*, GLOBAL structure, `suspended_stack`
    (name -> stashed `(name, callOrder)` tuples, shared across ALL threads --
    unlike `open_stacks`, which stays per-thread): an "exit" pops the span
    off `open_stacks` (if it's genuinely on top -- it always should be) and
    stashes it; the next "enter" for that same name restores it from the
    stash, UNLESS it's already on top (the very first enter, which
    immediately follows NEW -- confirmed directly -- doesn't need
    restoring, NEW already pushed it). "close" resolves via the SAME
    real-source-location lookup as NEW (ENTER/EXIT/CLOSE all carry the same
    `filename`/`line_number`, confirmed directly), checking the stash first
    and falling back to the plain `open_stacks` pop otherwise -- covers
    both "this invocation suspended at least once before finishing" and
    "ENTER/EXIT wasn't enabled at all, nothing was ever stashed." Verified
    against a genuinely nested case too, not just siblings: an outer async
    fn awaiting an inner one (the inner with its own separate sleep) still
    correctly resolves `stack: ["outer"]` for the inner one, even while two
    other, unrelated async fns are also interleaving on the same thread at
    the same time.

    The stash is deliberately GLOBAL rather than per-thread: a
    multi-threaded runtime (e.g. the default `#[tokio::main]` flavor) can
    migrate the *same* logical task's own polls across different OS
    threads over its lifetime -- confirmed directly on a scratch crate
    (a `tokio::spawn`'d task under real scheduler pressure legitimately
    showed 3 distinct `threadId`s across its own ENTER/EXIT lines). A
    per-thread stash would stash a suspended span under the thread it
    exited on and never find it again once resumed elsewhere. Restoring by
    name only (not by thread) fixes this: the restored entry is pushed
    onto whichever thread's `open_stacks` entry the resuming "enter"
    actually happened on -- correct, since for that poll the invocation
    really is running there. `open_stacks` itself stays per-thread
    unchanged, since it tracks what's genuinely active *right now*, which
    is always thread-local at any given instant (unlike the already-solved
    concurrent-*threads* problem, §2.11, where each OS thread keeps its own
    separate identity for its whole life -- that distinction is exactly why
    `open_stacks` didn't need this same treatment).

    Known, accepted limitation (unchanged in category by this fix, only in
    scope): if the SAME async fn/call-site is invoked multiple times truly
    concurrently (not merely interleaved by suspension, but genuinely
    overlapping in flight at once), the name-keyed stash can't distinguish
    between them and may restore the wrong specific invocation's stashed
    tuple. This risk already existed in the per-thread version too (for
    same-name concurrent invocations sharing one thread); going global only
    widens its scope from "within one thread" to "across threads," not a
    new category of risk.

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

    `graph` also fills in an ancestor for spans that come back with no
    recorded parent at all on their own thread -- `main` itself is the
    original, most common case (it's never a tracing span -- it can't
    usefully be #[instrument]'d, since the span would start before
    `tracing_subscriber::init()`, called from inside main's own body, has
    even run -- so a function main calls directly gets no recorded ancestor
    at all), and a thread spawned with no explicit `tracing::Span::current()`
    propagation (see the `open_stacks` paragraph below) is the other --
    both leave the span's own `stack` empty, indistinguishable from a
    genuine second root. This was first "fixed" by unconditionally treating
    every ancestor-less span as a child of `main` specifically -- rejected
    on review: that's a guess, not a fact, and a real one it could get
    wrong -- a span reached through some *other*, also-untraced
    intermediate function (main -> helper -> this_span, with `helper`
    itself never instrumented) would be mislabeled as called directly by
    main, an edge that might not even exist in the static graph. Fixed
    properly instead, and generalized beyond just `main`: an ancestor-less
    span is only ever attributed to a parent when `graph` shows that parent
    is the *one and only* static caller of this span's function, anywhere
    in the whole project (`_build_callers` below) -- not a guess, since
    with exactly one possible caller and no real ancestor recorded, that
    caller is the only answer that fits the facts. A function reached from
    2+ different static call sites (e.g. `dcore::add`, called from both
    `dummy-ops` and `dummy-api`) stays unattributed on an empty-stack hit --
    which one actually called it this time genuinely isn't recoverable, so
    it's left an honest "unknown" rather than picked arbitrarily. This is
    also what correctly colors `concurrent_demo -> thread_b`/`-> thread_c`
    during replay (see the `open_stacks` paragraph below for why those two
    come back with an empty stack in the first place): `concurrent_demo` is
    each one's sole static caller, so both get attributed to it even though
    neither was actually nested under it in the trace -- still not a guess,
    the static graph has no other candidate either way. No graph, or 2+
    candidate callers, and the span's `stack` stays exactly what it always
    was: empty, not a guess.

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
    pushing its enclosing span's name onto the open-span stack a SECOND
    time -- never popped back off (only the real CLOSE pops, once),
    permanently corrupting the depth/ancestor chain of every span parsed
    afterward. Confirmed directly: added a scratch `tracing::info!()` call
    inside an instrumented function, and every span after it in the trace
    came back one level deeper than it actually was. Each open-span stack
    entry (see below) holds `(name, callOrder)` pairs, not bare names --
    letting an EVENT look up its enclosing span's own dedup key directly
    (the top of its own thread's stack) rather than needing a second,
    independently-advancing occurrence counter to stay in sync with the one
    NEW already used to assign that callOrder -- CLOSE reads its own
    call_order the same way now (by popping it back off the stack), rather
    than recomputing it via a separate counter that only worked by relying
    on NEW and CLOSE always advancing in lockstep.

    `open_stacks` (plural) is one stack *per thread id*, not a single
    global one -- keyed by each entry's own optional `threadId` field
    (present only if the target added `.with_thread_ids(true)`, see
    README.md's "Optional: telling concurrent calls apart"; absent
    entries all share one implicit key, so a trace with no thread ids at
    all behaves exactly as if there were only ever one stack, unchanged
    from before this existed). This matters for a real reason, not just
    tidiness: two threads' NEW events can genuinely interleave in the log
    (thread A opens a span, thread B opens its own before A's closes) --
    with a single shared stack, thread B's span would wrongly appear
    *nested inside* thread A's still-open one, an actual corruption
    (confirmed directly: `concurrent_demo` calling `thread_b`/`thread_c`
    concurrently with NO thread ids at all made `thread_c` come back
    nested under `thread_b`, not as its sibling under `concurrent_demo` --
    and during replay this produced a genuinely misleading result, not
    just a gap: the edge into `thread_b` stayed visually "active" long
    after execution moved to `thread_c`, whose own real edge never lit up
    at all). Per-thread stacks fix the corruption -- each thread's own
    spans nest correctly among themselves -- but do NOT reconstruct a
    cross-thread parent link that was never in the data to begin with: a
    span whose own thread has an empty stack when it starts gets no real
    ancestor from `tracing` itself, same as `main`'s own direct children --
    the `implicit_parent` mechanism above (the graph confirming a single
    static caller) is what fills this in when it can, not anything specific
    to threads; genuinely ambiguous cases (2+ static callers) still come
    back an honest `stack: []` rather than a guessed link to whatever
    thread happened to spawn it. Getting a *real*, trace-recorded link
    (as opposed to this static-graph inference) needs the target code to
    explicitly carry `tracing::Span::current()` across the `thread::spawn`
    boundary (confirmed empirically: `tracing` does not do this on its
    own) -- deliberately out of scope, see PROJECT.md §4.
    """
    line_index = _build_line_index(source_index) if source_index else {}
    edge_call_orders = _build_edge_call_orders(graph) if graph else {}
    callers_of = _build_callers(graph) if graph else {}
    scan_cache = {}  # (norm path, attr line) -> resolved line or None, this parse only

    def implicit_parent(name):
        """The one static caller of `name`, if -- and only if -- the graph
        shows exactly one anywhere in the whole project. See this
        function's own docstring above."""
        callers = callers_of.get(name)
        if callers and len(callers) == 1:
            return next(iter(callers))
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

    _NO_THREAD_ID = object()  # sentinel: groups every thread-id-less entry into one implicit stack

    def thread_key(entry):
        return entry.get("threadId", _NO_THREAD_ID)

    open_stacks = {}  # thread id (or the sentinel above) -> [(resolved name, callOrder), ...]
    # resolved name -> [(name, callOrder), ...] -- invocations that have EXIT-ed
    # (suspended mid-`.await`, still genuinely in flight) but haven't CLOSE-d yet.
    # See the ENTER/EXIT branches below for why this exists at all -- async fn only,
    # never touched for ordinary sync code (no ENTER/EXIT lines emitted there at all
    # unless the target opted into `FmtSpan::ENTER | FmtSpan::EXIT`).
    #
    # Deliberately GLOBAL, not per-thread like `open_stacks` -- a multi-threaded
    # async runtime (unlike a genuinely separate OS thread, which keeps one
    # identity for its whole life) can resume the SAME task's next poll on a
    # completely different worker thread than the one it suspended on.
    # Confirmed directly on a scratch crate (`tokio::spawn`'d task, default
    # multi-threaded runtime, real CPU/scheduling pressure from hundreds of
    # competing yielding tasks): the same async fn's own ENTER/EXIT lines
    # legitimately carried 3 different `threadId`s across its own lifetime.
    # A per-thread stash (this dict's first version) would stash the
    # suspended entry under the thread it exited on and never find it again
    # once resumed elsewhere -- not a wrong/misleading result (the code
    # degrades to the same honest "no known ancestor" as an untraced
    # function, §2.12), but a real, avoidable gap: a child call made during
    # a migrated poll would lose its correct real-nesting attribution for no
    # reason a global lookup can't fix. Keyed by name only, same one
    # (name, callOrder) shape as everywhere else in this file -- shares a
    # pre-existing, still-untested edge case with the per-thread version:
    # if the exact same call site's async fn is invoked multiple times
    # truly concurrently (not yet exercised by any fixture), the wrong
    # stashed entry could be restored. Not a new risk this introduces, just
    # widens an already-latent one from "within one thread" to "across
    # threads too."
    suspended_stack = {}
    full_path_by_name = {}  # name -> that span's own full ancestor chain + itself, from its most
    # recent NEW resolution -- see the `implicit_parent` branch below for why.
    new_events = []
    close_stats = {}  # (resolved name, callOrder or None) -> {count, total_us}
    recorded_by_key = {}  # same key -> [close-time span-field dict, ...], one per invocation
    events_by_key = {}  # same key -> [{"message", "fields"}, ...], across every invocation
    close_seq_by_key = {}  # same key -> entry_seq of THIS invocation's own close line
    for entry_seq, entry in enumerate(entries):
        fields = entry.get("fields", {})
        span_info = entry.get("span", {})
        raw_name = span_info.get("name", "")
        message = fields.get("message")
        own_stack = open_stacks.setdefault(thread_key(entry), [])

        if "time.busy" in fields:
            # A close always matches whatever's currently innermost ON
            # THIS SAME THREAD -- see this function's own docstring for
            # why a single shared stack across threads would be wrong
            # here, and a name-based lookup would be no better. Its
            # callOrder is whatever NEW already assigned this exact
            # invocation -- popped back off the stack, not recomputed.
            #
            # EXCEPT for an `async fn` with ENTER/EXIT tracked (see those
            # branches below): its own EXIT (suspending at the final
            # `.await` before returning) already popped it off `own_stack`
            # and stashed it in the GLOBAL `suspended_stack` (not
            # thread-scoped -- see that dict's own comment for why: the
            # resuming ENTER, and therefore this CLOSE too, can legitimately
            # land on a different thread than the one that exited) -- by
            # the time CLOSE arrives, `own_stack`'s own top is whatever's
            # genuinely still entered (some ancestor, if any), NOT this
            # invocation anymore. Resolved by real source location (same as
            # NEW/ENTER/EXIT) to find the right stash -- falls through to
            # the plain `own_stack.pop()` below whenever nothing's stashed
            # (the overwhelmingly common case: ordinary sync code, or
            # ENTER/EXIT simply not enabled at all), so this changes
            # nothing for anything that isn't async-with-ENTER/EXIT.
            resolved_close = resolve_id(entry.get("filename"), entry.get("line_number"))
            close_name = resolved_close or raw_name
            stash = suspended_stack.get(close_name)
            if stash:
                name, call_order = stash.pop()
            elif own_stack:
                name, call_order = own_stack.pop()
            else:
                name, call_order = raw_name, None
            key = (name, call_order)
            us = parse_duration(fields.get("time.busy", "0"))
            st = close_stats.setdefault(key, {"count": 0, "total_us": 0.0})
            st["count"] += 1
            st["total_us"] += us
            # This invocation's own real position in the raw log -- see
            # `openSeq`/`closeSeq` in the docstring below for what this is
            # for. Overwritten on each repeat invocation of the same key
            # (a loop), same as `close_stats` above -- only the latest
            # invocation's close position matters for that case, exactly
            # like `recorded_by_key` only ever needing the current state.
            close_seq_by_key[key] = entry_seq
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
            if own_stack:
                # `own_stack` itself only ever holds the raw names tracing
                # actually pushed -- it has no idea the span currently on
                # top of it (say, `gap_demo`) was itself attributed to a
                # further, "virtual" ancestor via `implicit_parent` below
                # (e.g. `main`, which is never pushed onto any own_stack at
                # all, since it's never a real span). Reading raw own_stack
                # names here would silently drop that ancestor for every
                # REAL descendant of an implicit_parent-resolved span --
                # confirmed as a real bug: `gap_leaf` (really nested under
                # `gap_demo`, tracing's own `spans` field agrees) came back
                # with `stack: ["gap_demo"]` / `depth: 1`, the exact same
                # depth as `gap_demo` itself (`stack: ["main"]`) -- so the
                # sidebar rendered them as siblings, not nested, even though
                # they genuinely are. Fixed by reusing `full_path_by_name`
                # for the innermost currently-open span instead of reading
                # own_stack's raw name directly: that entry's own chain was
                # already fully resolved (recursively including any
                # implicit_parent prefix) the moment IT was pushed, so this
                # composes correctly through any depth of real nesting.
                top_name, _co = own_stack[-1]
                stack = full_path_by_name.get(top_name, [nm for nm, _co in own_stack])
            else:
                stack = []
            if not stack:
                # Confirmed-by-the-static-graph case only -- see
                # implicit_parent's docstring. Doesn't touch `own_stack`
                # itself (that stays a pure record of what tracing actually
                # recorded on this one thread); this only affects what THIS
                # one span reports as its own ancestor. Inherits the
                # confirmed parent's OWN full chain (`full_path_by_name`),
                # not just that one name alone -- `stack` has to be a real
                # root-to-parent chain, the same shape a normally-nested
                # `own_stack`-derived one already is, or the viewer's own
                # `computeReturnPath` (which walks two spans' `stack`s
                # looking for a shared prefix) can't find any common
                # ancestor between e.g. `concurrent_demo` (`['main']`) and
                # `thread_b` (`['concurrent_demo']` alone, no `main`) and
                # wrongly concludes it must unwind all the way back to
                # `main` -- confirmed as a real, visible bug: the "return"
                # flash animation fired on the very first step into
                # `thread_b`, before `thread_b`'s own edge had even lit up.
                # Falls back to just `[confirmed]` if the parent's own NEW
                # hasn't been resolved yet (shouldn't happen in practice --
                # a function has to run before it can spawn a thread that
                # calls something else -- but no worse than before this
                # existed if it somehow does).
                confirmed = implicit_parent(name)
                if confirmed:
                    stack = full_path_by_name.get(confirmed, [confirmed])
            parent = stack[-1] if stack else None
            call_order = call_order_for(parent, name)
            ev = {
                "name": name,
                "depth": len(stack),
                "stack": stack,
                "fields": {k: v for k, v in span_info.items() if k != "name"},
                "openSeq": entry_seq,
            }
            if call_order is not None:
                ev["callOrder"] = call_order
            new_events.append(ev)
            own_stack.append((name, call_order))
            full_path_by_name[name] = stack + [name]
        elif message in ("enter", "exit"):
            # `async fn` only -- an ordinary sync fn's own span is entered
            # once (right after NEW) and never exited until CLOSE, so
            # ENTER/EXIT lines (opt-in via `FmtSpan::ENTER | FmtSpan::EXIT`,
            # never emitted otherwise) only ever show up at all for async
            # code, where the *real* poll-by-poll picture matters: a
            # suspended `.await` genuinely leaves the executor free to run
            # something else entirely on this same thread in the meantime.
            # Without tracking this, `own_stack` would keep a suspended
            # span "open" the whole time it's actually idle, and whatever
            # the executor happens to run next gets wrongly nested under
            # it -- confirmed as a real, reproducible bug on a real
            # single-threaded-runtime scratch crate (two `#[instrument]`
            # async fns, one with a longer sleep than the other, run via
            # `tokio::join!`): the shorter one came back with a nonexistent
            # `stack: ["<the longer one>"]` even though they're genuinely
            # concurrent, not nested.
            #
            # Resolved by real source location, exactly like NEW/CLOSE --
            # ENTER/EXIT lines carry the same `filename`/`line_number` as
            # NEW does (confirmed directly against a real trace), so this
            # is never a name-only guess.
            resolved = resolve_id(entry.get("filename"), entry.get("line_number"))
            name = resolved or raw_name
            if message == "exit":
                # Only pop if this span is genuinely on top -- it always
                # should be (a span can't exit out from under something
                # still entered above it), but never corrupt `own_stack` by
                # popping the wrong thing if that assumption somehow doesn't
                # hold.
                if own_stack and own_stack[-1][0] == name:
                    suspended_stack.setdefault(name, []).append(own_stack.pop())
            else:  # "enter"
                # The very FIRST enter for a given invocation immediately
                # follows its own NEW (confirmed directly) -- NEW already
                # pushed it, so this is a deliberate no-op then, not a
                # double-push. Only a *resuming* enter (this span was
                # previously exited and is now genuinely active again)
                # needs to restore it from the stash.
                already_on_top = own_stack and own_stack[-1][0] == name
                if not already_on_top:
                    stash = suspended_stack.get(name)
                    if stash:
                        # Pushed onto THIS thread's own_stack -- the one this
                        # ENTER actually happened on, which can genuinely
                        # differ from the thread it exited on (multi-threaded
                        # async runtime, confirmed directly on a scratch
                        # crate -- see `suspended_stack`'s own comment). For
                        # this poll, the invocation really IS running here.
                        own_stack.append(stash.pop())
                    # else: nothing to restore -- ENTER without a matching
                    # prior EXIT for this name (shouldn't happen in a
                    # well-formed trace); leave `own_stack` alone rather
                    # than fabricating an entry with a guessed callOrder.
        else:
            # EVENT: a plain tracing::event!/info!/... call from directly
            # inside the currently-innermost span's own body, on THIS
            # thread -- not a span itself (never pushed onto own_stack,
            # never assigned its own callOrder), just attached to
            # whatever IS currently innermost there.
            if own_stack:
                enclosing_key = own_stack[-1]
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
        close_seq = close_seq_by_key.get(key)
        if close_seq is not None:
            ev["closeSeq"] = close_seq
        recorded = recorded_by_key.get(key)
        if recorded:
            ev["recordedFields"] = recorded
        events = events_by_key.get(key)
        if events:
            ev["events"] = events
        deduped.append(ev)
    return deduped
