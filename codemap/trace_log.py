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


def parse_trace(text: str, source_index: dict | None = None) -> list:
    """Returns a list of deduped span dicts: name/depth/stack/fields/iterations/duration_ms.

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

    Two passes: the first builds a raw-name -> resolved-id map from
    whichever entries carry their own `filename`/`line_number` (every span
    does, on its own "new" event); the second applies that map to both a
    span's own name *and* every ancestor name in its `spans` stack, since
    the ancestor list only ever repeats bare names with no location info of
    its own to resolve independently -- reusing the mapping already built
    from that same ancestor's own top-level entry elsewhere in the trace.
    """
    line_index = _build_line_index(source_index) if source_index else {}
    scan_cache = {}  # (norm path, attr line) -> resolved line or None, this parse only

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

    entries, name_map = [], {}
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
        if raw_name not in name_map:
            resolved = resolve_id(entry.get("filename"), entry.get("line_number"))
            if resolved:
                name_map[raw_name] = resolved

    def resolved_name(n):
        return name_map.get(n, n)

    new_events = []
    close_stats = {}  # resolved name -> {count, total_us}
    for entry in entries:
        fields = entry.get("fields", {})
        span_info = entry.get("span", {})
        span_stack = entry.get("spans", [])
        name = resolved_name(span_info.get("name", ""))

        if "time.busy" in fields:
            us = parse_duration(fields.get("time.busy", "0"))
            st = close_stats.setdefault(name, {"count": 0, "total_us": 0.0})
            st["count"] += 1
            st["total_us"] += us
        else:
            new_events.append({
                "name": name,
                "depth": len(span_stack),
                "stack": [resolved_name(s.get("name", "")) for s in span_stack],
                "fields": {k: v for k, v in span_info.items() if k != "name"},
            })

    seen, deduped = set(), []
    for ev in new_events:
        if ev["name"] in seen: continue
        seen.add(ev["name"])
        st = close_stats.get(ev["name"], {"count": 1, "total_us": 0.0})
        ev["iterations"] = st["count"]
        ev["duration_ms"] = round(st["total_us"] / 1000.0, 4)
        ev["total_ms"] = ev["duration_ms"]
        deduped.append(ev)
    return deduped
