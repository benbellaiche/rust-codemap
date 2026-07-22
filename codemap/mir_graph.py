"""mir_graph.py -- Build a call-graph from a Rust MIR text dump.

Pure MIR-text parsing: no assumption about the target crate's module names,
directory layout, or dependency list. The only assumptions made are
properties of how rustc's MIR pretty-printer behaves for *any* crate:

- Items defined in the crate being compiled are printed either bare
  (`fn name(...)` for free functions) or, for impl methods, as
  `fn <mod>::<impl at PATH:...>::name(...)` where PATH points into the
  crate's own source tree.
- Items from other crates (std, core, alloc, or any dependency) are always
  printed fully path-qualified, and a dependency's embedded source path
  resolves through cargo's registry/git checkout cache.

Nothing here refers to a specific project's module or type names.
"""
import re

# Trait/derive machinery generated for almost any Rust type -- noise
# regardless of the target crate's own logic. Not project-specific: these
# are the standard derive/trait-impl method names Rust itself generates.
EXCLUDE_NAMES = {
    "fmt", "clone", "drop", "default", "deref", "deref_mut",
    "hash", "eq", "ne", "partial_eq", "lt", "le", "gt", "ge",
    "debug_fmt", "display_fmt",
    "from_residual", "branch", "from_output",
    "visit_str", "visit_u64", "visit_bytes", "visit_seq", "visit_map",
    "visit_identifier", "next_key", "next_value", "end",
    "serialize", "deserialize", "deserialize_struct", "expecting",
    "size_hint", "poll", "into_future",
}
# Whole-line substrings that mark generated/macro/std/dependency-internal
# code, independent of any specific target crate.
EXCLUDE_LINE = [
    "_serde", "serde_", "tracing::", "callsite", "subscriber", "Metadata::",
    "Interest::", "LevelFilter::", "Span::", "closure#", "promoted[",
    "std::", "core::", "alloc::", "panic::", "__D", "__A",
    "_::_serde", "__Field", "__Visitor", "__FieldVisitor",
]
# A module-qualified impl belongs to the crate under analysis unless its
# embedded source path clearly resolves through cargo's dependency or
# toolchain caches. This replaces any hardcoded list of "our" module names.
EXTERNAL_PATH_MARKERS = (".cargo", "registry", ".rustup", "toolchains")

RE_IMPL_SELF = re.compile(r"fn \w+::<impl at [^>]+>::(\w+)\(_1:\s*&(?:mut\s+)?(\w+)")
RE_IMPL_CTOR = re.compile(r"fn \w+::<impl at [^>]+>::(\w+)\(.*?\)\s*->\s*(\w+)\s*\{")
RE_IMPL_SOURCE = re.compile(r"<impl at ([^:]+):")
RE_FREE_FN = re.compile(r"^fn (\w+)\(")
RE_CALL_TERM = re.compile(r"= ([a-zA-Z_<][^(]*)\([^)]*\)\s*->\s*\[return:")
RE_CLOSURE_SUFFIX = re.compile(r"(::\{closure#\d+\})+$")

# Primitive/std return-or-arg types: can't be used as "the type this method
# belongs to" (e.g. `&str` for a `from_json(raw: &str)` constructor).
PRIMITIVES = {"str", "String", "bool", "f64", "f32", "i32", "i64", "u32",
              "u64", "usize", "isize", "u8", "i8", "u16", "i16", "char"}


def extract_fn_name(line: str):
    """Extract the short method/function name from a MIR definition line."""
    m = RE_IMPL_SELF.search(line)
    if m: return m.group(1)
    m = RE_IMPL_CTOR.search(line)
    if m: return m.group(1)
    m = RE_FREE_FN.match(line)
    if m: return m.group(1)
    return None


def is_local_impl(fn_prefix: str) -> bool:
    """True if a module-qualified `<impl at PATH:...>` resolves to a source
    file inside the target crate rather than a dependency/toolchain cache."""
    m = RE_IMPL_SOURCE.search(fn_prefix)
    if not m:
        return False
    path = m.group(1)
    return not any(marker in path for marker in EXTERNAL_PATH_MARKERS)


def should_include(line: str) -> bool:
    fn_name = extract_fn_name(line)
    if fn_name is None: return False
    if fn_name in EXCLUDE_NAMES: return False
    # Check EXCLUDE_LINE only on the prefix (before parameters), so a local
    # function isn't filtered out because of its parameter *types*.
    fn_prefix = line.split("(")[0] if "(" in line else line
    for excl in EXCLUDE_LINE:
        if excl in fn_prefix: return False
    if RE_FREE_FN.match(line): return True
    if "<impl at " in fn_prefix:
        return is_local_impl(fn_prefix)
    return False


def normalize_def_line(line: str):
    m = RE_IMPL_SELF.search(line)
    if m:
        method, type_name = m.group(1), m.group(2)
        if type_name not in PRIMITIVES:
            return f"{type_name}::{method}"
        # First arg is a primitive (e.g. &str for from_json) -> fall through
        # and try the return-type form below.

    m = RE_IMPL_CTOR.search(line)
    if m:
        method, ret_type = m.group(1), m.group(2)
        if not any(c in ret_type for c in "<>()") and ret_type not in PRIMITIVES:
            return f"{ret_type}::{method}"

    m = RE_FREE_FN.match(line)
    if m: return m.group(1)
    return None


def qualified_path(raw: str):
    """'fn X::Y(args) -> Z {' -> 'X::Y' (path only, no params)."""
    return raw[3:].split("(", 1)[0] if raw.startswith("fn ") else None


def closure_owner_path(raw: str):
    """If raw defines a closure, return the qualified path of its enclosing fn."""
    path = qualified_path(raw)
    if not path or "{closure#" not in path:
        return None
    return RE_CLOSURE_SUFFIX.sub("", path)


def normalize_call(raw: str):
    if "<dyn " in raw: return None
    m = re.match(r"<([^>]+) as [^>]+>::(\w+)$", raw)
    if m: return f"{m.group(1).split('::')[-1]}::{m.group(2)}"
    parts = raw.split("::")
    return "::".join(parts[-2:]) if len(parts) >= 2 else (parts[0] if parts else None)


def parse_mir(text: str):
    """Returns (local_ids: set[str], edges: list[(str, str)], traced: dict[str, bool])."""
    local_ids = set()
    fn_def_lines = {}
    lines = text.splitlines()

    for line in lines:
        raw = line.strip()
        if not (raw.startswith("fn ") or re.match(r"fn \w+::", raw)):
            continue
        if "{" not in raw: continue
        if not should_include(raw): continue
        norm = normalize_def_line(raw)
        if norm:
            fn_def_lines[raw] = norm
            local_ids.add(norm)

    # Map each tracked fn's qualified path (no params) -> its normalized id,
    # so calls made from within its closures can be attributed back to it.
    owner_path_to_id = {qualified_path(raw): norm for raw, norm in fn_def_lines.items()}

    # A fn is "traced" if #[instrument]/span!/event! expanded into it: that
    # generates real MIR calls into the tracing callsite machinery (visible
    # here as "Callsite"). Derived straight from the MIR body -- no runtime
    # trace needed, and holds for any crate that uses the `tracing` crate.
    traced = {nid: False for nid in local_ids}

    edges = []
    edge_set = set()
    current_fn_id = None
    brace_depth = 0

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("fn ") and "{" in stripped:
            matched_id = fn_def_lines.get(stripped)
            if matched_id is None:
                owner_path = closure_owner_path(stripped)
                matched_id = owner_path_to_id.get(owner_path)
            current_fn_id = matched_id
            brace_depth = stripped.count("{") - stripped.count("}")
            continue
        if current_fn_id is None: continue
        brace_depth += line.count("{") - line.count("}")
        if brace_depth <= 0:
            current_fn_id = None
            continue
        if "Callsite" in stripped:
            traced[current_fn_id] = True
        for m in RE_CALL_TERM.finditer(stripped):
            raw_callee = m.group(1).strip()

            # Call through a dyn Trait: over-approximate by linking to EVERY
            # local implementation of that method (MIR alone can't tell us
            # which concrete type is behind the trait object).
            m_dyn = re.match(r"<dyn \w+ as \w+>::(\w+)$", raw_callee)
            if m_dyn:
                method = m_dyn.group(1)
                for node_id in local_ids:
                    if node_id.endswith(f"::{method}") and node_id != current_fn_id:
                        key = (current_fn_id, node_id)
                        if key not in edge_set:
                            edge_set.add(key); edges.append(key)
                continue

            callee_id = normalize_call(raw_callee)
            if callee_id and callee_id in local_ids and callee_id != current_fn_id:
                key = (current_fn_id, callee_id)
                if key not in edge_set:
                    edge_set.add(key); edges.append(key)

    return local_ids, edges, traced


def build_graph(mir_text: str) -> dict:
    """Parse a MIR text dump into a Cytoscape-ready {nodes, edges} dict."""
    local_ids, edges, traced = parse_mir(mir_text)
    all_nodes = set(local_ids)
    for s, t in edges:
        all_nodes.update([s, t])
    return {
        "nodes": [
            {"data": {
                "id": n,
                "label": n,
                "nodeType": "method" if "::" in n else "fn",
                "traced": traced.get(n, False),
            }}
            for n in sorted(all_nodes)
        ],
        "edges": [
            {"data": {"id": f"e{i}", "source": s, "target": t, "edgeType": "call"}}
            for i, (s, t) in enumerate(edges)
        ],
    }
