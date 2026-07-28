"""mir_graph.py -- Build a call-graph from Rust MIR text dumps, one per crate
in a dependency closure, merged into one graph with crate-qualified node ids.

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
- A call site referencing another *local* crate in this project's own
  dependency closure is *sometimes* qualified with that crate's real name
  (confirmed empirically: `dcore::basics::add(...)`) and *sometimes* not
  (same kind of call, same crate pair, confirmed as plain `double(...)` for
  a call into `dops::combiner::double`) -- MIR's existing inconsistency
  about printing a *module* prefix (see `RE_FREE_FN`'s docstring) turns out
  to extend across crate boundaries too, not just within one crate. This is
  why node-id resolution below is a two-phase process (parse each crate on
  its own first, merge and resolve call targets globally after), not a
  single per-crate pass.

Nothing here refers to a specific project's module or type names; the only
project-specific input is the *list* of local crate names in the dependency
closure, itself derived from `cargo metadata` in `__main__.py`, never
hardcoded here.
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

## A module prefix before `<impl at ...>` is only present when the impl
## isn't at the crate root (e.g. `report::<impl at ...>`; a crate-root impl
## is just `<impl at ...>` with no prefix at all), and the Self type in the
## first parameter can itself be module-qualified (e.g. `&report::Item`,
## not just `&Item`) when it's defined in a submodule. Both prefixes are
## therefore optional / repeatable, not a single mandatory `\w+::`.
RE_IMPL_SELF = re.compile(r"fn (?:\w+::)*<impl at [^>]+>::(\w+)\(_1:\s*&(?:mut\s+)?(?:\w+::)*(\w+)")
RE_IMPL_CTOR = re.compile(r"fn (?:\w+::)*<impl at [^>]+>::(\w+)\(.*?\)\s*->\s*(?:\w+::)*(\w+)\s*\{")
RE_IMPL_SOURCE = re.compile(r"<impl at ([^:]+):")
# A free function's module path is sometimes printed (e.g. `basics::add`)
# and sometimes not (e.g. `compute`, for a function in its own `compute.rs`
# module) -- MIR isn't consistent about this. Normalize to the bare name
# either way, matching the (already relied-upon) unqualified form.
RE_FREE_FN = re.compile(r"^fn (?:\w+::)*(\w+)\(")
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


def is_local_impl(fn_prefix: str, extra_external_markers=()) -> bool:
    """True if a module-qualified `<impl at PATH:...>` resolves to a source
    file inside the target crate rather than a dependency/toolchain cache.

    `extra_external_markers`: absolute paths (or path fragments) that are
    external even though they don't look like a normal `.cargo`/`.rustup`
    cache -- e.g. a `cargo vendor` directory, which copies dependency source
    into the workspace itself. A vendored dependency's generic code still
    gets monomorphized straight into whatever local crate's MIR uses it, so
    without this it looks exactly like the target crate's own code (see
    codemap.find_vendor_dirs, which is what actually discovers these paths
    -- this module stays a pure MIR-text parser with no filesystem/cargo-
    config knowledge of its own)."""
    m = RE_IMPL_SOURCE.search(fn_prefix)
    if not m:
        return False
    path = m.group(1)
    return not any(marker in path for marker in (*EXTERNAL_PATH_MARKERS, *extra_external_markers))


def should_include(line: str, extra_external_markers=()) -> bool:
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
        return is_local_impl(fn_prefix, extra_external_markers)
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


def split_crate_hint(raw: str, known_crates):
    """If `raw`'s leading path segment names one of this project's own
    local crates (`known_crates` -- derived from `cargo metadata`'s
    dependency closure in `__main__.py`, never a hardcoded list), split it
    off and return `(crate_name, remainder)`; otherwise `(None, raw)`
    unchanged.

    This only catches the call sites where MIR *did* include an explicit
    crate qualifier -- confirmed empirically that it doesn't always
    (`dcore::basics::add(...)` vs. a bare `double(...)` for a call into
    `dops::combiner::double`, same kind of cross-crate call). The bare case
    is resolved later, across all crates at once, in `merge_crates()`."""
    if raw.startswith("<"):
        return None, raw
    m = re.match(r"(\w+)::(.+)$", raw)
    if m and m.group(1) in known_crates:
        return m.group(1), m.group(2)
    return None, raw


def normalize_call(raw: str):
    if "<dyn " in raw: return None
    # `<Type as Trait>::method` (e.g. operator-overload desugaring:
    # `v + i` compiles to `<&i32 as Add<i32>>::add`). `[^>]+` on either
    # side breaks the instant Type or Trait carries its own generic
    # argument (`Add<i32>` has a `>` before the wrapper's own closing
    # `>`) -- it silently fails to match, falls through to the
    # module::free_fn logic below, and "Add<i32>" one specific case cost
    # us here: it got misread by that fallback as `module::add`, an exact
    # collision with a real *local* `add` free function even though this
    # was std's operator trait, not ours. `.+` (matches `>` too) doesn't
    # have that problem -- greedy, so it finds the *last* " as " and the
    # *last* "::method" in the string, which is always the outermost
    # wrapper's own boundary regardless of what's nested inside Type/Trait.
    m = re.match(r"<(.+) as .+>::(\w+)$", raw)
    if m:
        type_part = m.group(1).split("::")[-1]
        # A leading `&`/`&mut ` (e.g. `&i32`) would otherwise leave an
        # empty first segment ("" before "i32") -- skip past it instead of
        # returning a blank type name.
        type_name = next((p for p in re.split(r"[<>&]|mut ", type_part) if p), type_part)
        return f"{type_name}::{m.group(2)}"
    # A generic fn's call site carries its monomorphized type argument(s) as
    # a turbofish (`generic_max::<i32>`, `generic_max::<f64>` for the same
    # definition) -- strip it, or the `<i32>` segment gets mistaken for a
    # nested module/free-fn name below and the real name is lost entirely.
    # One definition normalizes to one bare id (RE_FREE_FN strips generics
    # the same way), so every monomorphization correctly collapses back to
    # that same node instead of appearing to have no callers at all.
    raw = re.sub(r"::<[^<>]*>", "", raw)
    parts = raw.split("::")
    if not parts: return None
    if len(parts) == 1: return parts[0]
    # Two shapes reach here: "Type::method" (module::Type::method with the
    # module dropped) and "module::free_fn" (a free function's call site
    # keeping its module qualifier, while its own definition normalizes to
    # the bare name -- see RE_FREE_FN). Tell them apart the same way Rust's
    # own naming convention does: a Type starts uppercase, a module/crate
    # segment doesn't.
    last_two = parts[-2:]
    if last_two[0][:1].isupper():
        return "::".join(last_two)
    return last_two[-1]


def parse_mir(text: str, crate_name: str, known_crates=(), extra_external_markers=()):
    """Parse ONE crate's own MIR text in isolation. Returns a dict:
    {"local_ids": set[str], "edges": list[dict], "dyn_edges": list[dict],
     "traced": dict[str, bool]}.

    Every id in `local_ids`/`traced`/an edge's own `source` is qualified as
    `{crate_name}::<id>` -- e.g. `dcore::Item::describe`, `dapi::run_report`
    -- so that two different crates defining the same free function or
    `Type::method` pair no longer collide into a single node once merged
    (see `merge_crates`). An edge's `target` is deliberately left
    UNRESOLVED here (`callee_raw` + `crate_hint`, not a final id): this
    function only sees one crate's own text, and a call site's target may
    need a *different* crate's `local_ids` to resolve -- `merge_crates`
    does that, using every crate's results at once. Same reasoning for
    `dyn_edges` (a `&dyn Trait` fan-out target can just as easily be a
    different crate's trait impl, see the `dummy-ops::Batch`/
    `dummy-core::Pair` `Summable` fixture)."""
    local_ids = set()
    fn_def_lines = {}
    lines = text.splitlines()

    for line in lines:
        raw = line.strip()
        if not (raw.startswith("fn ") or re.match(r"fn \w+::", raw)):
            continue
        if "{" not in raw: continue
        if not should_include(raw, extra_external_markers): continue
        norm = normalize_def_line(raw)
        if norm:
            fn_def_lines[raw] = norm
            local_ids.add(f"{crate_name}::{norm}")

    # Map each tracked fn's qualified path (no params) -> its normalized id
    # (bare, not yet crate-prefixed -- only used below to attribute a
    # closure's calls back to its own enclosing fn, entirely within this
    # same crate), so calls made from within its closures can be attributed
    # back to it.
    owner_path_to_id = {qualified_path(raw): norm for raw, norm in fn_def_lines.items()}

    # A fn is "traced" if #[instrument]/span!/event! expanded into it: that
    # generates real MIR calls into the tracing callsite machinery (visible
    # here as "Callsite"). Derived straight from the MIR body -- no runtime
    # trace needed, and holds for any crate that uses the `tracing` crate.
    traced = {nid: False for nid in local_ids}

    edges = []
    dyn_edges = []
    # A raw sequence number per caller (every matched call term, in MIR
    # order -- NOT the final call_order): resolution against local_ids
    # can't happen until merge_crates() sees every crate at once, but MOST
    # matched call terms are noise that will never resolve to anything
    # local at all (#[instrument]'s own generated Span::new/Callsite::
    # interest/LevelFilter::current/... scaffolding calls, one full family
    # of them per instrumented fn) -- assigning the real, gap-free 1, 2, 3,
    # ... call_order has to happen AFTER merge_crates() drops those, or a
    # caller with plenty of tracing-generated noise before its first real
    # call ends up with call_order values like 19, 20 instead of 1, 2 (a
    # real bug caught here: confirmed on dummy-cli's own `main`, whose
    # first real call landed at call_order 9 before this fix, purely from
    # counting the `tracing_subscriber::fmt()` builder chain's own method
    # calls). merge_crates() re-numbers per source, in `_seq` order, once
    # only the calls that actually resolved to a real local target remain.
    seq_counter = {}  # caller id -> next raw sequence number
    current_fn_id = None
    brace_depth = 0

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("fn ") and "{" in stripped:
            matched_norm = fn_def_lines.get(stripped)
            if matched_norm is None:
                owner_path = closure_owner_path(stripped)
                matched_norm = owner_path_to_id.get(owner_path)
            current_fn_id = f"{crate_name}::{matched_norm}" if matched_norm else None
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

            # Call through a dyn Trait: MIR alone can't tell us which
            # concrete type is behind the trait object, so this fans out to
            # every local implementation of that method -- deferred to
            # merge_crates(), which is the only place that has every
            # crate's local_ids at once (an implementor can be in a
            # different crate than the caller, or than the trait itself).
            m_dyn = re.match(r"<dyn \w+ as \w+>::(\w+)$", raw_callee)
            if m_dyn:
                seq = seq_counter.get(current_fn_id, 0)
                seq_counter[current_fn_id] = seq + 1
                dyn_edges.append({"source": current_fn_id, "method": m_dyn.group(1), "_seq": seq})
                continue

            crate_hint, rest = split_crate_hint(raw_callee, known_crates)
            callee_raw = normalize_call(rest)
            if not callee_raw:
                continue
            # Resolution (same-crate vs. cross-crate, incl. the "MIR left
            # out the crate qualifier" case) needs every crate's local_ids
            # at once -- deferred to merge_crates(). callee_id == source is
            # genuine recursion (e.g. plain `factorial(n - 1)` inside
            # `factorial` itself) -- a real, meaningful self-edge once
            # resolved, not noise to filter out.
            seq = seq_counter.get(current_fn_id, 0)
            seq_counter[current_fn_id] = seq + 1
            edges.append({
                "source": current_fn_id, "callee_raw": callee_raw,
                "crate_hint": crate_hint, "_seq": seq,
            })

    return {"local_ids": local_ids, "edges": edges, "dyn_edges": dyn_edges, "traced": traced}


def merge_crates(per_crate: dict) -> dict:
    """`per_crate`: {crate_name: parse_mir(...) result}. Resolves every
    crate's deferred call targets against the UNION of every crate's own
    `local_ids`, and returns the final {"local_ids", "edges", "traced"}
    merged across the whole dependency closure -- `edges` here has the same
    shape `build_graph` has always emitted: {"source", "target", "call_order"}.

    Resolution order for a plain (non-dyn) call, `callee_raw` from crate C:
    1. `crate_hint` was set (MIR *did* qualify this call with a known local
       crate's name) -> resolve directly against that crate's own
       `local_ids`. Not found there -> drop (nothing in our closure
       matches; likely an unusual shape, treat like any other unresolved
       external call).
    2. No hint -> same-crate first: `C::callee_raw` in `local_ids`? This is
       the common case (an ordinary intra-crate call), and deliberately
       checked before any other crate, so a name that happens to exist in
       BOTH the caller's own crate and some other crate always resolves to
       the caller's own crate, not to a coincidental match elsewhere.
    3. Still nothing -> search every OTHER crate's `local_ids` for
       `other::callee_raw`. Exactly one match: resolve there (this is what
       a genuinely cross-crate call whose qualifier MIR left out --
       confirmed to happen, see `parse_mir`'s docstring -- needs). Two or
       more matches: genuinely ambiguous from MIR text alone -- fan out to
       every match, sharing one `call_order`, the same over-approximation
       already used for `&dyn Trait` calls (a real call site, an
       unresolvable target, so show every possibility rather than silently
       picking one or dropping it). Zero matches: external (std, a
       registry dependency, or a name that just doesn't exist in this
       closure) -> drop, as always.

    A `&dyn Trait` call fans out to every crate's implementation of that
    method (by `local_ids` suffix `::method`), excluding the call's own
    source -- same reasoning as before, just searching globally instead of
    within one crate's own `local_ids`.

    `call_order` itself is assigned in a final pass, *after* resolution --
    not the raw per-crate `_seq` matched-call-term order. Most matched call
    terms in an instrumented fn's MIR body are `#[instrument]`'s own
    generated scaffolding (`Span::new`, `Callsite::interest`,
    `LevelFilter::current`, ...), never resolve to anything local, and get
    dropped above -- if `call_order` were just `_seq` renumbered per crate
    at parse time (as it was before this function existed, when a single
    crate's own `local_ids` was all there was to check against), a caller
    with plenty of that scaffolding before its first *real* call would get
    call_order values like 19, 20 instead of 1, 2 (confirmed on dummy-cli's
    own `main`, whose builder-chain `tracing_subscriber::fmt()...init()`
    calls otherwise ate call_order 1-8 before its first real call). Fan-out
    edges from the same original call site (dyn-dispatch, or an ambiguous
    cross-crate match) share one `_seq` and must keep sharing one final
    `call_order` too -- grouping by `(source, _seq)` before renumbering,
    not renumbering every edge independently, is what preserves that."""
    all_local_ids = set()
    for result in per_crate.values():
        all_local_ids |= result["local_ids"]

    by_crate_ids = {c: r["local_ids"] for c, r in per_crate.items()}

    resolved = []  # {"source", "target", "_seq"} -- call_order assigned below
    for crate_name, result in per_crate.items():
        for e in result["edges"]:
            source, callee_raw, crate_hint = e["source"], e["callee_raw"], e["crate_hint"]
            target = None
            if crate_hint is not None:
                candidate = f"{crate_hint}::{callee_raw}"
                if candidate in by_crate_ids.get(crate_hint, ()):
                    target = candidate
            else:
                same_crate_candidate = f"{crate_name}::{callee_raw}"
                if same_crate_candidate in result["local_ids"]:
                    target = same_crate_candidate
                else:
                    matches = [f"{other}::{callee_raw}" for other, ids in by_crate_ids.items()
                               if other != crate_name and f"{other}::{callee_raw}" in ids]
                    if len(matches) == 1:
                        target = matches[0]
                    elif len(matches) > 1:
                        for m_id in matches:
                            resolved.append({"source": source, "target": m_id, "_seq": e["_seq"]})
                        continue
            if target:
                resolved.append({"source": source, "target": target, "_seq": e["_seq"]})

        for de in result["dyn_edges"]:
            targets = [nid for nid in all_local_ids
                       if nid.endswith(f"::{de['method']}") and nid != de["source"]]
            for nid in targets:
                resolved.append({"source": de["source"], "target": nid, "_seq": de["_seq"]})

    # Group by (source, _seq) -- one group per real call site that
    # survived resolution -- in `_seq` order, then assign 1, 2, 3, ... per
    # source across its own groups. Every edge in a group (a fan-out) gets
    # that same number.
    by_source = {}
    for e in resolved:
        by_source.setdefault(e["source"], {}).setdefault(e["_seq"], []).append(e)
    edges = []
    for source, by_seq in by_source.items():
        for order, seq in enumerate(sorted(by_seq), start=1):
            for e in by_seq[seq]:
                edges.append({"source": e["source"], "target": e["target"], "call_order": order})

    traced = {}
    for result in per_crate.values():
        traced.update(result["traced"])

    return {"local_ids": all_local_ids, "edges": edges, "traced": traced}


def build_graph(crate_texts, known_crates=None, extra_external_markers=()) -> dict:
    """`crate_texts`: {crate_name: mir_text} for every crate in the
    dependency closure. Parses each crate's MIR independently, merges, and
    returns a Cytoscape-ready {nodes, edges} dict with crate-qualified node
    ids (`crate::Type::method`, `crate::free_fn`).

    `known_crates` defaults to `crate_texts`'s own keys (the ordinary case:
    every crate we might cross-reference is one we also parsed). Accepting
    it separately, rather than always deriving it from `crate_texts`,
    leaves room for a future caller that knows about a local crate's name
    without having its MIR text on hand -- not used today, kept simple."""
    if known_crates is None:
        known_crates = set(crate_texts)
    per_crate = {
        cname: parse_mir(text, cname, known_crates, extra_external_markers)
        for cname, text in crate_texts.items()
    }
    merged = merge_crates(per_crate)
    local_ids, edges, traced = merged["local_ids"], merged["edges"], merged["traced"]
    all_nodes = set(local_ids)
    for e in edges:
        all_nodes.update([e["source"], e["target"]])
    return {
        "nodes": [
            {"data": {
                "id": n,
                "label": n,
                # A method id is "crate::Type::method" (2 "::"), a free fn
                # id is "crate::name" (1) -- the crate segment always adds
                # exactly one more "::" than the un-qualified id used to
                # have, so counting (not just checking presence) is what
                # keeps this distinction correct now that every id has at
                # least one "::" in it.
                "nodeType": "method" if n.count("::") >= 2 else "fn",
                "traced": traced.get(n, False),
            }}
            for n in sorted(all_nodes)
        ],
        "edges": [
            {"data": {
                "id": f"e{i}", "source": e["source"], "target": e["target"],
                "edgeType": "call", "callOrder": e["call_order"],
            }}
            for i, e in enumerate(edges)
        ],
    }
