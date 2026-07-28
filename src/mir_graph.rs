//! mir_graph.rs -- Rust port of `src/codemap/mir_graph.py`. Build a
//! call-graph from Rust MIR text dumps, one per crate in a dependency
//! closure, merged into one graph with crate-qualified node ids.
//!
//! Kept behaviorally identical to the Python original on purpose -- see
//! that file's own module docstring for the full rationale (rustc's MIR
//! pretty-printer inconsistencies this parser works around). Comments here
//! only restate the WHY where it isn't obvious from a skim; the Python
//! version remains the fuller reference.

use once_cell::sync::Lazy;
use regex::Regex;
use serde::Serialize;
use serde_json::Value;
use std::collections::{HashMap, HashSet};

// Trait/derive machinery generated for almost any Rust type -- noise
// regardless of the target crate's own logic.
const EXCLUDE_NAMES: &[&str] = &[
    "fmt", "clone", "drop", "default", "deref", "deref_mut",
    "hash", "eq", "ne", "partial_eq", "lt", "le", "gt", "ge",
    "debug_fmt", "display_fmt",
    "from_residual", "branch", "from_output",
    "visit_str", "visit_u64", "visit_bytes", "visit_seq", "visit_map",
    "visit_identifier", "next_key", "next_value", "end",
    "serialize", "deserialize", "deserialize_struct", "expecting",
    "size_hint", "poll", "into_future",
];

// Whole-line substrings that mark generated/macro/std/dependency-internal
// code, independent of any specific target crate.
const EXCLUDE_LINE: &[&str] = &[
    "_serde", "serde_", "tracing::", "callsite", "subscriber", "Metadata::",
    "Interest::", "LevelFilter::", "Span::", "closure#", "promoted[",
    "std::", "core::", "alloc::", "panic::", "__D", "__A",
    "_::_serde", "__Field", "__Visitor", "__FieldVisitor",
];

// A module-qualified impl belongs to the crate under analysis unless its
// embedded source path clearly resolves through cargo's dependency or
// toolchain caches.
const EXTERNAL_PATH_MARKERS: &[&str] = &[".cargo", "registry", ".rustup", "toolchains"];

// Primitive/std return-or-arg types: can't be used as "the type this method
// belongs to" (e.g. `&str` for a `from_json(raw: &str)` constructor).
const PRIMITIVES: &[&str] = &[
    "str", "String", "bool", "f64", "f32", "i32", "i64", "u32",
    "u64", "usize", "isize", "u8", "i8", "u16", "i16", "char",
];

// A module prefix before `<impl at ...>` is only present when the impl
// isn't at the crate root; the Self type in the first parameter can itself
// be module-qualified too -- both optional/repeatable, not a single
// mandatory `\w+::`.
static RE_IMPL_SELF: Lazy<Regex> = Lazy::new(|| {
    Regex::new(r"fn (?:\w+::)*<impl at [^>]+>::(\w+)\(_1:\s*&(?:mut\s+)?(?:\w+::)*(\w+)").unwrap()
});
static RE_IMPL_CTOR: Lazy<Regex> = Lazy::new(|| {
    Regex::new(r"fn (?:\w+::)*<impl at [^>]+>::(\w+)\(.*?\)\s*->\s*(?:\w+::)*(\w+)\s*\{").unwrap()
});
static RE_IMPL_SOURCE: Lazy<Regex> = Lazy::new(|| Regex::new(r"<impl at ([^:]+):").unwrap());
// A free function's module path is sometimes printed and sometimes not --
// MIR isn't consistent about this. Normalize to the bare name either way.
static RE_FREE_FN: Lazy<Regex> = Lazy::new(|| Regex::new(r"^fn (?:\w+::)*(\w+)\(").unwrap());
static RE_CALL_TERM: Lazy<Regex> =
    Lazy::new(|| Regex::new(r"= ([a-zA-Z_<][^(]*)\([^)]*\)\s*->\s*\[return:").unwrap());
static RE_CLOSURE_SUFFIX: Lazy<Regex> = Lazy::new(|| Regex::new(r"(::\{closure#\d+\})+$").unwrap());
static RE_DYN_CALL: Lazy<Regex> = Lazy::new(|| Regex::new(r"^<dyn \w+ as \w+>::(\w+)$").unwrap());
static RE_CRATE_HINT: Lazy<Regex> = Lazy::new(|| Regex::new(r"^(\w+)::(.+)$").unwrap());
static RE_TYPE_AS_TRAIT: Lazy<Regex> = Lazy::new(|| Regex::new(r"^<(.+) as .+>::(\w+)$").unwrap());
static RE_TURBOFISH: Lazy<Regex> = Lazy::new(|| Regex::new(r"::<[^<>]*>").unwrap());
static RE_TYPE_SPLIT: Lazy<Regex> = Lazy::new(|| Regex::new(r"[<>&]|mut ").unwrap());
static RE_FN_START: Lazy<Regex> = Lazy::new(|| Regex::new(r"^fn \w+::").unwrap());

/// Extract the short method/function name from a MIR definition line.
fn extract_fn_name(line: &str) -> Option<String> {
    if let Some(c) = RE_IMPL_SELF.captures(line) {
        return Some(c[1].to_string());
    }
    if let Some(c) = RE_IMPL_CTOR.captures(line) {
        return Some(c[1].to_string());
    }
    if let Some(c) = RE_FREE_FN.captures(line) {
        return Some(c[1].to_string());
    }
    None
}

/// True if a module-qualified `<impl at PATH:...>` resolves to a source
/// file inside the target crate rather than a dependency/toolchain cache.
/// `extra_external_markers`: e.g. a `cargo vendor` directory -- see
/// `find_vendor_dirs` in main.rs, which is what discovers these paths; this
/// module stays a pure MIR-text parser with no filesystem/cargo-config
/// knowledge of its own.
fn is_local_impl(fn_prefix: &str, extra_external_markers: &[String]) -> bool {
    let Some(c) = RE_IMPL_SOURCE.captures(fn_prefix) else {
        return false;
    };
    let path = &c[1];
    !EXTERNAL_PATH_MARKERS
        .iter()
        .any(|m| path.contains(m))
        && !extra_external_markers.iter().any(|m| path.contains(m.as_str()))
}

fn should_include(line: &str, extra_external_markers: &[String]) -> bool {
    let Some(fn_name) = extract_fn_name(line) else {
        return false;
    };
    if EXCLUDE_NAMES.contains(&fn_name.as_str()) {
        return false;
    }
    // Check EXCLUDE_LINE only on the prefix (before parameters), so a local
    // function isn't filtered out because of its parameter *types*.
    let fn_prefix = line.split('(').next().unwrap_or(line);
    if EXCLUDE_LINE.iter().any(|excl| fn_prefix.contains(excl)) {
        return false;
    }
    if RE_FREE_FN.is_match(line) {
        return true;
    }
    if fn_prefix.contains("<impl at ") {
        return is_local_impl(fn_prefix, extra_external_markers);
    }
    false
}

fn normalize_def_line(line: &str) -> Option<String> {
    if let Some(c) = RE_IMPL_SELF.captures(line) {
        let method = &c[1];
        let type_name = &c[2];
        if !PRIMITIVES.contains(&type_name) {
            return Some(format!("{type_name}::{method}"));
        }
        // First arg is a primitive (e.g. &str for from_json) -> fall
        // through and try the return-type form below.
    }
    if let Some(c) = RE_IMPL_CTOR.captures(line) {
        let method = &c[1];
        let ret_type = &c[2];
        if !ret_type.contains(['<', '>', '(', ')']) && !PRIMITIVES.contains(&ret_type) {
            return Some(format!("{ret_type}::{method}"));
        }
    }
    if let Some(c) = RE_FREE_FN.captures(line) {
        return Some(c[1].to_string());
    }
    None
}

/// 'fn X::Y(args) -> Z {' -> 'X::Y' (path only, no params).
fn qualified_path(raw: &str) -> Option<&str> {
    let rest = raw.strip_prefix("fn ")?;
    Some(rest.split_once('(').map(|(p, _)| p).unwrap_or(rest))
}

/// If `raw` defines a closure, return the qualified path of its enclosing fn.
fn closure_owner_path(raw: &str) -> Option<String> {
    let path = qualified_path(raw)?;
    if !path.contains("{closure#") {
        return None;
    }
    Some(RE_CLOSURE_SUFFIX.replace(path, "").to_string())
}

/// If `raw`'s leading path segment names one of this project's own local
/// crates, split it off and return `(Some(crate), remainder)`; otherwise
/// `(None, raw)` unchanged. Only catches call sites where MIR *did* include
/// an explicit crate qualifier -- the bare case is resolved later, across
/// all crates at once, in `merge_crates`.
fn split_crate_hint<'a>(raw: &'a str, known_crates: &HashSet<String>) -> (Option<String>, &'a str) {
    if raw.starts_with('<') {
        return (None, raw);
    }
    if let Some(c) = RE_CRATE_HINT.captures(raw) {
        let crate_seg = c.get(1).unwrap().as_str();
        if known_crates.contains(crate_seg) {
            return (Some(crate_seg.to_string()), c.get(2).unwrap().as_str());
        }
    }
    (None, raw)
}

fn normalize_call(raw: &str) -> Option<String> {
    if raw.contains("<dyn ") {
        return None;
    }
    // `<Type as Trait>::method` (operator-overload desugaring etc.) -- `.+`
    // (not `[^>]+`) on both sides, greedy, so it finds the *last* " as "
    // and the *last* "::method", the outermost wrapper's own boundary
    // regardless of what generic args are nested inside Type/Trait.
    if let Some(c) = RE_TYPE_AS_TRAIT.captures(raw) {
        let type_part = c[1].split("::").last().unwrap_or(&c[1]);
        // A leading `&`/`&mut ` would otherwise leave an empty first
        // segment -- skip past it instead of returning a blank type name.
        let type_name = RE_TYPE_SPLIT
            .split(type_part)
            .find(|p| !p.is_empty())
            .unwrap_or(type_part);
        return Some(format!("{type_name}::{}", &c[2]));
    }
    // A generic fn's call site carries its monomorphized type argument(s)
    // as a turbofish -- strip it, or it gets mistaken for a nested
    // module/free-fn name below and the real name is lost entirely.
    let raw = RE_TURBOFISH.replace_all(raw, "");
    let parts: Vec<&str> = raw.split("::").collect();
    if parts.is_empty() {
        return None;
    }
    if parts.len() == 1 {
        return Some(parts[0].to_string());
    }
    // Two shapes reach here: "Type::method" and "module::free_fn". Tell
    // them apart the same way Rust's own naming convention does: a Type
    // starts uppercase, a module/crate segment doesn't.
    let last_two = &parts[parts.len() - 2..];
    if last_two[0].chars().next().is_some_and(|c| c.is_uppercase()) {
        Some(last_two.join("::"))
    } else {
        Some(last_two[last_two.len() - 1].to_string())
    }
}

#[derive(Debug)]
pub struct RawEdge {
    pub source: String,
    pub callee_raw: String,
    pub crate_hint: Option<String>,
    pub seq: usize,
}

#[derive(Debug)]
pub struct DynEdge {
    pub source: String,
    pub method: String,
    pub seq: usize,
}

#[derive(Debug, Default)]
pub struct ParsedCrate {
    pub local_ids: HashSet<String>,
    pub edges: Vec<RawEdge>,
    pub dyn_edges: Vec<DynEdge>,
    pub traced: HashMap<String, bool>,
}

/// Parse ONE crate's own MIR text in isolation. Every id is qualified as
/// `{crate_name}::<id>` so two crates defining the same free function or
/// `Type::method` pair don't collide into one node once merged. An edge's
/// `target` is deliberately left unresolved (`callee_raw` + `crate_hint`):
/// this function only sees one crate's own text, and a call site's target
/// may need a *different* crate's `local_ids` to resolve -- `merge_crates`
/// does that, using every crate's results at once.
pub fn parse_mir(
    text: &str,
    crate_name: &str,
    known_crates: &HashSet<String>,
    extra_external_markers: &[String],
) -> ParsedCrate {
    let mut local_ids = HashSet::new();
    let mut fn_def_lines: HashMap<String, String> = HashMap::new();
    let lines: Vec<&str> = text.lines().collect();

    for line in &lines {
        let raw = line.trim();
        let looks_like_fn_start = raw.starts_with("fn ") || RE_FN_START.is_match(raw);
        if !looks_like_fn_start {
            continue;
        }
        if !raw.contains('{') {
            continue;
        }
        if !should_include(raw, extra_external_markers) {
            continue;
        }
        if let Some(norm) = normalize_def_line(raw) {
            local_ids.insert(format!("{crate_name}::{norm}"));
            fn_def_lines.insert(raw.to_string(), norm);
        }
    }

    // Map each tracked fn's qualified path (no params) -> its normalized id
    // (bare, not yet crate-prefixed), so calls made from within its
    // closures can be attributed back to it.
    let owner_path_to_id: HashMap<String, String> = fn_def_lines
        .iter()
        .filter_map(|(raw, norm)| qualified_path(raw).map(|p| (p.to_string(), norm.clone())))
        .collect();

    // A fn is "traced" if #[instrument]/span!/event! expanded into it --
    // visible here as a real MIR call into the tracing callsite machinery.
    let mut traced: HashMap<String, bool> = local_ids.iter().map(|id| (id.clone(), false)).collect();

    let mut edges = Vec::new();
    let mut dyn_edges = Vec::new();
    let mut seq_counter: HashMap<String, usize> = HashMap::new();
    let mut current_fn_id: Option<String> = None;
    let mut brace_depth: i32 = 0;

    for line in &lines {
        let stripped = line.trim();
        if stripped.starts_with("fn ") && stripped.contains('{') {
            let matched_norm = fn_def_lines.get(stripped).cloned().or_else(|| {
                closure_owner_path(stripped).and_then(|owner| owner_path_to_id.get(&owner).cloned())
            });
            current_fn_id = matched_norm.map(|norm| format!("{crate_name}::{norm}"));
            brace_depth = stripped.matches('{').count() as i32 - stripped.matches('}').count() as i32;
            continue;
        }
        let Some(ref cur_id) = current_fn_id else { continue };
        brace_depth += line.matches('{').count() as i32 - line.matches('}').count() as i32;
        if brace_depth <= 0 {
            current_fn_id = None;
            continue;
        }
        if stripped.contains("Callsite") {
            traced.insert(cur_id.clone(), true);
        }
        for m in RE_CALL_TERM.captures_iter(stripped) {
            let raw_callee = m[1].trim();

            // Call through a dyn Trait: fans out to every local
            // implementation of that method, deferred to merge_crates.
            if let Some(dc) = RE_DYN_CALL.captures(raw_callee) {
                let seq = *seq_counter.get(cur_id).unwrap_or(&0);
                seq_counter.insert(cur_id.clone(), seq + 1);
                dyn_edges.push(DynEdge {
                    source: cur_id.clone(),
                    method: dc[1].to_string(),
                    seq,
                });
                continue;
            }

            let (crate_hint, rest) = split_crate_hint(raw_callee, known_crates);
            let Some(callee_raw) = normalize_call(rest) else {
                continue;
            };
            let seq = *seq_counter.get(cur_id).unwrap_or(&0);
            seq_counter.insert(cur_id.clone(), seq + 1);
            edges.push(RawEdge {
                source: cur_id.clone(),
                callee_raw,
                crate_hint,
                seq,
            });
        }
    }

    ParsedCrate { local_ids, edges, dyn_edges, traced }
}

struct ResolvedEdge {
    source: String,
    target: String,
    seq: usize,
}

pub struct MergedGraph {
    pub local_ids: HashSet<String>,
    pub edges: Vec<(String, String, u32)>, // source, target, call_order
    pub traced: HashMap<String, bool>,
}

/// Resolves every crate's deferred call targets against the UNION of every
/// crate's own `local_ids`. Resolution order for a plain (non-dyn) call:
/// 1. `crate_hint` set -> resolve against that crate's own `local_ids` only.
/// 2. No hint -> same-crate first (checked before any other crate, so a
///    name existing in both the caller's own crate and elsewhere always
///    resolves to the caller's own crate).
/// 3. Still nothing -> search every OTHER crate's `local_ids`. Exactly one
///    match -> resolve there. 2+ matches -> genuinely ambiguous, fan out
///    to every match (same over-approximation as `&dyn Trait`). Zero -> drop.
pub fn merge_crates(per_crate: &HashMap<String, ParsedCrate>) -> MergedGraph {
    let mut all_local_ids: HashSet<String> = HashSet::new();
    for result in per_crate.values() {
        all_local_ids.extend(result.local_ids.iter().cloned());
    }

    let mut resolved: Vec<ResolvedEdge> = Vec::new();
    for (crate_name, result) in per_crate {
        for e in &result.edges {
            let mut target: Option<String> = None;
            if let Some(hint) = &e.crate_hint {
                let candidate = format!("{hint}::{}", e.callee_raw);
                if per_crate
                    .get(hint)
                    .is_some_and(|r| r.local_ids.contains(&candidate))
                {
                    target = Some(candidate);
                }
            } else {
                let same_crate_candidate = format!("{crate_name}::{}", e.callee_raw);
                if result.local_ids.contains(&same_crate_candidate) {
                    target = Some(same_crate_candidate);
                } else {
                    let matches: Vec<String> = per_crate
                        .iter()
                        .filter(|(other, _)| *other != crate_name)
                        .filter_map(|(other, r)| {
                            let candidate = format!("{other}::{}", e.callee_raw);
                            r.local_ids.contains(&candidate).then_some(candidate)
                        })
                        .collect();
                    if matches.len() == 1 {
                        target = Some(matches[0].clone());
                    } else if matches.len() > 1 {
                        for m_id in matches {
                            resolved.push(ResolvedEdge { source: e.source.clone(), target: m_id, seq: e.seq });
                        }
                        continue;
                    }
                }
            }
            if let Some(t) = target {
                resolved.push(ResolvedEdge { source: e.source.clone(), target: t, seq: e.seq });
            }
        }

        for de in &result.dyn_edges {
            for nid in all_local_ids
                .iter()
                .filter(|nid| nid.ends_with(&format!("::{}", de.method)) && **nid != de.source)
            {
                resolved.push(ResolvedEdge { source: de.source.clone(), target: nid.clone(), seq: de.seq });
            }
        }
    }

    // Group by (source, seq) -- one group per real call site that survived
    // resolution -- in seq order, then assign 1, 2, 3, ... per source
    // across its own groups. Every edge in a group (a fan-out) gets the
    // same number.
    let mut by_source: HashMap<String, HashMap<usize, Vec<ResolvedEdge>>> = HashMap::new();
    for e in resolved {
        by_source
            .entry(e.source.clone())
            .or_default()
            .entry(e.seq)
            .or_default()
            .push(e);
    }
    let mut edges = Vec::new();
    for (_source, by_seq) in by_source {
        let mut seqs: Vec<usize> = by_seq.keys().copied().collect();
        seqs.sort_unstable();
        for (order, seq) in seqs.into_iter().enumerate() {
            let order = (order + 1) as u32;
            for e in &by_seq[&seq] {
                edges.push((e.source.clone(), e.target.clone(), order));
            }
        }
    }

    let mut traced = HashMap::new();
    for result in per_crate.values() {
        traced.extend(result.traced.iter().map(|(k, v)| (k.clone(), *v)));
    }

    MergedGraph { local_ids: all_local_ids, edges, traced }
}

#[derive(Serialize)]
struct NodeData {
    id: String,
    label: String,
    #[serde(rename = "nodeType")]
    node_type: &'static str,
    traced: bool,
}
#[derive(Serialize)]
struct Node {
    data: NodeData,
}
#[derive(Serialize)]
struct EdgeData {
    id: String,
    source: String,
    target: String,
    #[serde(rename = "edgeType")]
    edge_type: &'static str,
    #[serde(rename = "callOrder")]
    call_order: u32,
}
#[derive(Serialize)]
struct Edge {
    data: EdgeData,
}

/// `crate_texts`: {crate_name: mir_text} for every crate in the dependency
/// closure. Parses each crate's MIR independently, merges, and returns a
/// Cytoscape-ready {nodes, edges} JSON value with crate-qualified node ids.
pub fn build_graph(
    crate_texts: &HashMap<String, String>,
    extra_external_markers: &[String],
) -> Value {
    let known_crates: HashSet<String> = crate_texts.keys().cloned().collect();
    let per_crate: HashMap<String, ParsedCrate> = crate_texts
        .iter()
        .map(|(cname, text)| {
            (
                cname.clone(),
                parse_mir(text, cname, &known_crates, extra_external_markers),
            )
        })
        .collect();
    let merged = merge_crates(&per_crate);

    let mut all_nodes: HashSet<String> = merged.local_ids.clone();
    for (source, target, _) in &merged.edges {
        all_nodes.insert(source.clone());
        all_nodes.insert(target.clone());
    }
    let mut sorted_nodes: Vec<&String> = all_nodes.iter().collect();
    sorted_nodes.sort();

    let nodes: Vec<Node> = sorted_nodes
        .into_iter()
        .map(|n| Node {
            data: NodeData {
                id: n.clone(),
                label: n.clone(),
                // A method id is "crate::Type::method" (2 "::"), a free fn
                // id is "crate::name" (1).
                node_type: if n.matches("::").count() >= 2 { "method" } else { "fn" },
                traced: *merged.traced.get(n).unwrap_or(&false),
            },
        })
        .collect();

    let edges: Vec<Edge> = merged
        .edges
        .iter()
        .enumerate()
        .map(|(i, (source, target, call_order))| Edge {
            data: EdgeData {
                id: format!("e{i}"),
                source: source.clone(),
                target: target.clone(),
                edge_type: "call",
                call_order: *call_order,
            },
        })
        .collect();

    serde_json::json!({ "nodes": nodes, "edges": edges })
}
