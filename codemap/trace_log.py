"""trace_log.py -- Parse a `tracing_subscriber` JSON-lines log into replayable spans.

Expected input: one JSON object per line, as produced by
`tracing_subscriber::fmt::layer().json().with_span_events(FmtSpan::NEW | FmtSpan::CLOSE)`.
See README.md for the full format contract and current known limitations
(span names are used as the dedup key, so two call sites of the same
function currently collapse into one node).
"""
import json
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


def parse_trace(text: str) -> list:
    """Returns a list of deduped span dicts: name/depth/stack/fields/iterations/duration_ms."""
    new_events = []
    close_stats = {}  # name -> {count, total_us}

    for line in text.splitlines():
        line = line.strip()
        if not line: continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        fields = entry.get("fields", {})
        span_info = entry.get("span", {})
        span_stack = entry.get("spans", [])
        name = span_info.get("name", "")
        if not name: continue

        if "time.busy" in fields:
            us = parse_duration(fields.get("time.busy", "0"))
            st = close_stats.setdefault(name, {"count": 0, "total_us": 0.0})
            st["count"] += 1
            st["total_us"] += us
        else:
            new_events.append({
                "name": name,
                "depth": len(span_stack),
                "stack": [s.get("name", "") for s in span_stack],
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
