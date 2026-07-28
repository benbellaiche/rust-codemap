//! trace_log.rs -- Rust port of `src/codemap/trace_log.py`. Parse a
//! `tracing_subscriber` JSON-lines log into replayable spans.
//!
//! Kept behaviorally identical to the Python original -- see that file's
//! own module docstring for the FULL rationale (this is the one with the
//! most accumulated subtle bug fixes: the global suspended-span stash for
//! async fn, the implicit_parent static-graph fallback, per-thread
//! open-span stacks, call-site splitting, NEW/CLOSE/ENTER/EXIT/EVENT line
//! classification). Comments here restate the WHY only where truly
//! load-bearing for a reader trying to modify this file; the Python
//! version remains the fuller narrative reference.

use once_cell::sync::Lazy;
use regex::Regex;
use serde_json::{json, Map, Value};
use std::collections::{HashMap, HashSet};

static DURATION_RE: Lazy<Regex> = Lazy::new(|| Regex::new(r"(?i)([\d.]+)\s*([a-zµ]+)").unwrap());

/// Parse a tracing `time.busy`-style duration string into microseconds.
fn parse_duration(s: &str) -> f64 {
    let s = s.trim();
    let Some(m) = DURATION_RE.find(s).and_then(|_| DURATION_RE.captures(s)) else { return 0.0 };
    let v: f64 = m[1].parse().unwrap_or(0.0);
    let u = m[2].to_lowercase();
    if u.contains('n') {
        return v / 1000.0;
    }
    if s.contains('µ') || u.contains("us") {
        return v;
    }
    if u.starts_with('m') {
        return v * 1000.0;
    }
    if u.starts_with('s') {
        return v * 1_000_000.0;
    }
    v
}

/// `os.path.normcase(os.path.normpath(p))` equivalent: lowercase +
/// backslash-normalize on Windows (case-insensitive filesystem, and
/// rustc/tracing may report a different slash style than doc_index.py's
/// own path.resolve()), a no-op on case-sensitive POSIX.
fn norm_path(p: &str) -> String {
    if p.is_empty() {
        return String::new();
    }
    #[cfg(windows)]
    {
        p.replace('/', "\\").to_lowercase()
    }
    #[cfg(not(windows))]
    {
        p.to_string()
    }
}

/// (normalized absolute file path, line, node id) triples, from
/// source_index.json's own per-node `absPath`/`line`. A `Vec`, not a
/// `HashMap` keyed by (path, line): a trace's own `filename` isn't always
/// already absolute (see `resolve_id`'s suffix-match fallback below), so
/// exact-key lookup alone isn't enough.
fn build_line_index(source_index: &Value) -> Vec<(String, u64, String)> {
    let mut index = Vec::new();
    let Some(obj) = source_index.as_object() else { return index };
    for (node_id, entry) in obj {
        let abs_path = entry.get("absPath").and_then(Value::as_str);
        let line = entry.get("line").and_then(Value::as_u64);
        if let (Some(abs_path), Some(line)) = (abs_path, line) {
            if !abs_path.is_empty() && line > 0 {
                index.push((norm_path(abs_path), line, node_id.clone()));
            }
        }
    }
    index
}

/// `tracing`'s `#[instrument]` always reports the line the attribute itself
/// starts on -- never the line of the fn/method it's attached to. Scans
/// forward from `attr_line`, skipping blank lines, `//`/`///`/`//!` line
/// comments, `/* */` block comments (tracked across lines), and further
/// attributes (tracked for their own possible multi-line `(`/`[` nesting),
/// stopping at the first line that's actually code -- matching what
/// source_index.json (via cargo doc's own source link) already records for
/// that same item. Returns None if the file can't be read or no code line
/// is found.
fn scan_forward_to_code_line(filepath: &str, attr_line: u64) -> Option<u64> {
    let text = std::fs::read_to_string(filepath).ok()?;
    let lines: Vec<&str> = text.lines().collect();
    let mut i = attr_line.saturating_sub(1) as usize; // attr_line is 1-indexed
    let mut in_block_comment = false;
    let mut paren_depth: i64 = 0;
    while i < lines.len() {
        let stripped = lines[i].trim();
        if in_block_comment {
            if stripped.contains("*/") {
                in_block_comment = false;
            }
            i += 1;
            continue;
        }
        if paren_depth > 0 {
            paren_depth += stripped.matches('(').count() as i64 - stripped.matches(')').count() as i64;
            paren_depth += stripped.matches('[').count() as i64 - stripped.matches(']').count() as i64;
            i += 1;
            continue;
        }
        if stripped.is_empty() || stripped.starts_with("//") {
            i += 1;
            continue;
        }
        if stripped.starts_with("/*") {
            if !stripped.contains("*/") {
                in_block_comment = true;
            }
            i += 1;
            continue;
        }
        if stripped.starts_with("#[") || stripped.starts_with("#![") {
            let depth = stripped.matches('(').count() as i64 - stripped.matches(')').count() as i64
                + stripped.matches('[').count() as i64 - stripped.matches(']').count() as i64;
            if depth > 0 {
                paren_depth = depth;
            }
            i += 1;
            continue;
        }
        return Some((i + 1) as u64);
    }
    None
}

/// callee id -> set of distinct caller ids, from graph.json's own edges.
/// Deliberately keyed on distinct *source*, not (source, callOrder): what
/// matters for `implicit_parent` is only "how many different functions
/// could possibly have called this one at all", not how many times.
fn build_callers(graph: &Value) -> HashMap<String, HashSet<String>> {
    let mut callers: HashMap<String, HashSet<String>> = HashMap::new();
    let Some(edges) = graph.get("edges").and_then(Value::as_array) else { return callers };
    for e in edges {
        let d = e.get("data").unwrap_or(e);
        let (Some(src), Some(tgt)) = (
            d.get("source").and_then(Value::as_str),
            d.get("target").and_then(Value::as_str),
        ) else { continue };
        callers.entry(tgt.to_string()).or_default().insert(src.to_string());
    }
    callers
}

/// (caller id, callee id) -> sorted list of that pair's callOrder values.
fn build_edge_call_orders(graph: &Value) -> HashMap<(String, String), Vec<u64>> {
    let mut m: HashMap<(String, String), Vec<u64>> = HashMap::new();
    let Some(edges) = graph.get("edges").and_then(Value::as_array) else { return m };
    for e in edges {
        let d = e.get("data").unwrap_or(e);
        let src = d.get("source").and_then(Value::as_str);
        let tgt = d.get("target").and_then(Value::as_str);
        let order = d.get("callOrder").and_then(Value::as_u64);
        if let (Some(src), Some(tgt), Some(order)) = (src, tgt, order) {
            m.entry((src.to_string(), tgt.to_string())).or_default().push(order);
        }
    }
    for v in m.values_mut() {
        v.sort_unstable();
    }
    m
}

/// (resolved name, callOrder-or-None) -- the dedup key used throughout.
type SpanKey = (String, Option<u64>);

struct CloseStat {
    count: u64,
    total_us: f64,
}

/// Sentinel thread key: groups every thread-id-less entry into one implicit stack.
const NO_THREAD_ID: &str = "\0__no_thread_id__";

fn thread_key(entry: &Value) -> String {
    entry
        .get("threadId")
        .and_then(Value::as_str)
        .map(str::to_string)
        .unwrap_or_else(|| NO_THREAD_ID.to_string())
}

struct SpanEvent {
    name: String,
    depth: usize,
    stack: Vec<String>,
    fields: Map<String, Value>,
    open_seq: usize,
    call_order: Option<u64>,
}

/// See the Python original's own module docstring for the full rationale
/// behind every mechanism here (openSeq/closeSeq, source-location
/// resolution, the GLOBAL suspended-span stash for async fn, per-thread
/// open-span stacks, the implicit_parent static-graph fallback,
/// recordedFields/events, call-site splitting via callOrder).
pub fn parse_trace(text: &str, source_index: Option<&Value>, graph: Option<&Value>) -> Vec<Value> {
    let line_index = source_index.map(build_line_index).unwrap_or_default();
    let edge_call_orders = graph.map(build_edge_call_orders).unwrap_or_default();
    let callers_of = graph.map(build_callers).unwrap_or_default();
    let mut scan_cache: HashMap<(String, u64), Option<u64>> = HashMap::new();

    // The one static caller of `name`, if -- and only if -- the graph
    // shows exactly one anywhere in the whole project.
    let implicit_parent = |name: &str, callers_of: &HashMap<String, HashSet<String>>| -> Option<String> {
        let callers = callers_of.get(name)?;
        if callers.len() == 1 {
            callers.iter().next().cloned()
        } else {
            None
        }
    };

    let mut resolve_id = |filename: Option<&str>, attr_line: Option<u64>| -> Option<String> {
        let (filename, attr_line) = (filename?, attr_line?);
        if filename.is_empty() || attr_line == 0 {
            return None;
        }
        let norm_filename = norm_path(filename);
        // The trace's own `filename` (rustc's `file!()`, embedded by
        // `#[instrument]`) is usually already absolute -- but it's relative
        // to the *compilation root* in general, which is the workspace
        // root once the target crate is a cargo workspace member, not
        // necessarily that crate's own directory. A relative filename
        // therefore won't match source_index.json's absPath directly;
        // resolve it to the real absolute path via a suffix match against
        // source_index's own entries first (unambiguous in practice: a
        // multi-segment relative path like "dummy-api/src/lib.rs" is not
        // going to coincidentally suffix-match some unrelated file).
        let real_path = if line_index.iter().any(|(p, _, _)| *p == norm_filename) {
            norm_filename
        } else {
            match line_index.iter().find(|(p, _, _)| p.ends_with(&norm_filename)) {
                Some((p, _, _)) => p.clone(),
                None => norm_filename,
            }
        };
        let key = (real_path, attr_line);
        let resolved_line = *scan_cache
            .entry(key.clone())
            .or_insert_with(|| scan_forward_to_code_line(&key.0, attr_line));
        let resolved_line = resolved_line?;
        line_index
            .iter()
            .find(|(p, l, _)| *p == key.0 && *l == resolved_line)
            .map(|(_, _, id)| id.clone())
    };

    // Only lines with a real, non-empty span.name matter -- anything else
    // (malformed JSON, a line with no span at all) is silently skipped,
    // same as the Python original.
    let mut entries: Vec<Value> = Vec::new();
    for line in text.lines() {
        let line = line.trim();
        if line.is_empty() {
            continue;
        }
        let Ok(entry) = serde_json::from_str::<Value>(line) else { continue };
        let raw_name = entry
            .get("span")
            .and_then(|s| s.get("name"))
            .and_then(Value::as_str)
            .unwrap_or("");
        if raw_name.is_empty() {
            continue;
        }
        entries.push(entry);
    }

    // Which occurrence (0-indexed) of this (parent, name) pair we're up to
    // -- only ever needed for NEW.
    let mut new_occurrence: HashMap<(Option<String>, String), usize> = HashMap::new();
    let mut call_order_for = |parent: Option<&str>, name: &str| -> Option<u64> {
        let Some(parent) = parent else { return None };
        let sites = edge_call_orders.get(&(parent.to_string(), name.to_string()))?;
        if sites.len() <= 1 {
            return None;
        }
        let key = (Some(parent.to_string()), name.to_string());
        let idx = *new_occurrence.get(&key).unwrap_or(&0);
        new_occurrence.insert(key, idx + 1);
        Some(sites[idx % sites.len()])
    };

    let mut open_stacks: HashMap<String, Vec<(String, Option<u64>)>> = HashMap::new();
    // Deliberately GLOBAL, not per-thread like `open_stacks` -- a
    // multi-threaded async runtime can resume the SAME task's next poll on
    // a completely different worker thread than the one it suspended on.
    // See trace_log.py's own docstring for the full empirical confirmation
    // (§2.14 in PROJECT.md).
    let mut suspended_stack: HashMap<String, Vec<(String, Option<u64>)>> = HashMap::new();
    let mut full_path_by_name: HashMap<String, Vec<String>> = HashMap::new();
    let mut new_events: Vec<SpanEvent> = Vec::new();
    let mut close_stats: HashMap<SpanKey, CloseStat> = HashMap::new();
    let mut recorded_by_key: HashMap<SpanKey, Vec<Value>> = HashMap::new();
    let mut events_by_key: HashMap<SpanKey, Vec<Value>> = HashMap::new();
    let mut close_seq_by_key: HashMap<SpanKey, usize> = HashMap::new();

    for (entry_seq, entry) in entries.iter().enumerate() {
        let empty_map = Map::new();
        let fields = entry.get("fields").and_then(Value::as_object).unwrap_or(&empty_map);
        let empty_span = Map::new();
        let span_info = entry.get("span").and_then(Value::as_object).unwrap_or(&empty_span);
        let raw_name = span_info.get("name").and_then(Value::as_str).unwrap_or("");
        let message = fields.get("message").and_then(Value::as_str);
        let tkey = thread_key(entry);

        if fields.contains_key("time.busy") {
            // A close always matches whatever's currently innermost ON
            // THIS SAME THREAD -- EXCEPT for an async fn with ENTER/EXIT
            // tracked, whose own EXIT already stashed it in the GLOBAL
            // `suspended_stack` (see that field's own comment above).
            let resolved_close = resolve_id(
                entry.get("filename").and_then(Value::as_str),
                entry.get("line_number").and_then(Value::as_u64),
            );
            let close_name = resolved_close.unwrap_or_else(|| raw_name.to_string());
            let own_stack = open_stacks.entry(tkey.clone()).or_default();
            let (name, call_order) = if let Some(stash) = suspended_stack.get_mut(&close_name) {
                if let Some(popped) = stash.pop() {
                    popped
                } else if let Some(popped) = own_stack.pop() {
                    popped
                } else {
                    (raw_name.to_string(), None)
                }
            } else if let Some(popped) = own_stack.pop() {
                popped
            } else {
                (raw_name.to_string(), None)
            };
            let key: SpanKey = (name, call_order);
            let us = parse_duration(fields.get("time.busy").and_then(Value::as_str).unwrap_or("0"));
            let st = close_stats.entry(key.clone()).or_insert(CloseStat { count: 0, total_us: 0.0 });
            st.count += 1;
            st.total_us += us;
            close_seq_by_key.insert(key.clone(), entry_seq);
            let recorded: Map<String, Value> =
                span_info.iter().filter(|(k, _)| *k != "name").map(|(k, v)| (k.clone(), v.clone())).collect();
            recorded_by_key.entry(key).or_default().push(Value::Object(recorded));
        } else if message == Some("new") {
            let resolved = resolve_id(
                entry.get("filename").and_then(Value::as_str),
                entry.get("line_number").and_then(Value::as_u64),
            );
            let name = resolved.unwrap_or_else(|| raw_name.to_string());
            let own_stack = open_stacks.entry(tkey.clone()).or_default();
            let mut stack: Vec<String> = if let Some((top_name, _)) = own_stack.last() {
                full_path_by_name
                    .get(top_name)
                    .cloned()
                    .unwrap_or_else(|| own_stack.iter().map(|(nm, _)| nm.clone()).collect())
            } else {
                Vec::new()
            };
            if stack.is_empty() {
                // Confirmed-by-the-static-graph case only -- see
                // implicit_parent's own comment above.
                if let Some(confirmed) = implicit_parent(&name, &callers_of) {
                    stack = full_path_by_name
                        .get(&confirmed)
                        .cloned()
                        .unwrap_or_else(|| vec![confirmed]);
                }
            }
            let parent = stack.last().cloned();
            let call_order = call_order_for(parent.as_deref(), &name);
            let fields_no_name: Map<String, Value> =
                span_info.iter().filter(|(k, _)| *k != "name").map(|(k, v)| (k.clone(), v.clone())).collect();
            new_events.push(SpanEvent {
                name: name.clone(),
                depth: stack.len(),
                stack: stack.clone(),
                fields: fields_no_name,
                open_seq: entry_seq,
                call_order,
            });
            let own_stack = open_stacks.entry(tkey).or_default();
            own_stack.push((name.clone(), call_order));
            let mut full_path = stack;
            full_path.push(name.clone());
            full_path_by_name.insert(name, full_path);
        } else if message == Some("enter") || message == Some("exit") {
            let resolved = resolve_id(
                entry.get("filename").and_then(Value::as_str),
                entry.get("line_number").and_then(Value::as_u64),
            );
            let name = resolved.unwrap_or_else(|| raw_name.to_string());
            let own_stack = open_stacks.entry(tkey).or_default();
            if message == Some("exit") {
                // Only pop if this span is genuinely on top.
                if own_stack.last().is_some_and(|(n, _)| n == &name) {
                    let popped = own_stack.pop().unwrap();
                    suspended_stack.entry(name).or_default().push(popped);
                }
            } else {
                // The very FIRST enter for a given invocation immediately
                // follows its own NEW -- deliberate no-op then, not a
                // double-push. Only a *resuming* enter needs to restore it.
                let already_on_top = own_stack.last().is_some_and(|(n, _)| n == &name);
                if !already_on_top {
                    if let Some(stash) = suspended_stack.get_mut(&name) {
                        if let Some(popped) = stash.pop() {
                            // Pushed onto THIS thread's own_stack -- the
                            // one this ENTER actually happened on, which
                            // can genuinely differ from the thread it
                            // exited on.
                            own_stack.push(popped);
                        }
                    }
                }
            }
        } else {
            // EVENT: attached to whatever IS currently innermost on this
            // thread, never pushed onto own_stack itself.
            let own_stack = open_stacks.entry(tkey).or_default();
            if let Some(enclosing_key) = own_stack.last().cloned() {
                let fields_no_message: Map<String, Value> =
                    fields.iter().filter(|(k, _)| *k != "message").map(|(k, v)| (k.clone(), v.clone())).collect();
                events_by_key.entry(enclosing_key).or_default().push(json!({
                    "message": message.unwrap_or(""),
                    "fields": fields_no_message,
                }));
            }
        }
    }

    let mut seen: HashSet<SpanKey> = HashSet::new();
    let mut deduped = Vec::new();
    for ev in new_events {
        let key: SpanKey = (ev.name.clone(), ev.call_order);
        if seen.contains(&key) {
            continue;
        }
        seen.insert(key.clone());
        let (count, total_us) = close_stats
            .get(&key)
            .map(|s| (s.count, s.total_us))
            .unwrap_or((1, 0.0));
        let duration_ms = (total_us / 1000.0 * 10000.0).round() / 10000.0;
        let mut obj = Map::new();
        obj.insert("name".into(), json!(ev.name));
        obj.insert("depth".into(), json!(ev.depth));
        obj.insert("stack".into(), json!(ev.stack));
        obj.insert("fields".into(), Value::Object(ev.fields));
        obj.insert("openSeq".into(), json!(ev.open_seq));
        if let Some(co) = ev.call_order {
            obj.insert("callOrder".into(), json!(co));
        }
        obj.insert("iterations".into(), json!(count));
        obj.insert("duration_ms".into(), json!(duration_ms));
        obj.insert("total_ms".into(), json!(duration_ms));
        if let Some(&close_seq) = close_seq_by_key.get(&key) {
            obj.insert("closeSeq".into(), json!(close_seq));
        }
        if let Some(recorded) = recorded_by_key.get(&key) {
            obj.insert("recordedFields".into(), json!(recorded));
        }
        if let Some(events) = events_by_key.get(&key) {
            obj.insert("events".into(), json!(events));
        }
        deduped.push(Value::Object(obj));
    }
    deduped
}
