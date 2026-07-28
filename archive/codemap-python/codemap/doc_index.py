"""doc_index.py -- Cross-reference `cargo doc` HTML output with a call-graph.

Everything is discovered dynamically from the HTML files cargo doc actually
produced (struct.Name.html, enum.Name.html, trait.Name.html, fn.name.html)
and from the node ids present in a graph.json -- no per-project name table.
"""
import re
from pathlib import Path

ITEM_FILE_RE = re.compile(r"^(struct|enum|trait|fn)\.([A-Za-z_]\w*)\.html$")
# cargo doc source links always look like ".../src/<crate_name>/<path>.rs.html#<line>".
# The crate-name segment is skipped generically -- never hardcoded.
SOURCE_LINK_RE = re.compile(r'href="[^"]*/src/[^/"]+/([^"]+?\.rs)\.html#(\d+)')


def strip_tags(html: str) -> str:
    return (re.sub(r"<[^>]+>", "", html)
            .replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
            .replace("&#xa7;", "").strip())


def extract_top(html: str):
    """Signature/doc/source-link from the top of an item page (struct, enum, trait or fn)."""
    sig, doc, file_, line = "", "", "", 0
    m_decl = re.search(r'<pre[^>]*class="rust item-decl"[^>]*>.*?</pre>', html, re.DOTALL)
    if not m_decl:
        return sig, doc, file_, line
    code_m = re.search(r"<code>(.*?)</code>", m_decl.group(0), re.DOTALL)
    if code_m:
        sig = re.sub(r"\s+", " ", strip_tags(code_m.group(1))).strip()
    # The item's own doc comment (if any) sits immediately after item-decl.
    # If a section header (Fields / Implementations / ...) shows up first,
    # there's no doc for the item itself -- don't fall through to some other
    # item's docblock further down the same page (e.g. one of its methods).
    tail = html[m_decl.end():m_decl.end() + 3000]
    h2_pos = tail.find("<h2")
    doc_m = re.search(r'<div[^>]*class="docblock"[^>]*>(.*?)</div>', tail, re.DOTALL)
    if doc_m and (h2_pos == -1 or doc_m.start() < h2_pos):
        doc = doc_m.group(1).strip()
    m = SOURCE_LINK_RE.search(html)
    if m: file_, line = m.group(1), int(m.group(2))
    return sig, doc, file_, line


def extract_method(html: str, anchor: str):
    """Signature/doc/source-link for a specific method anchor on a struct/trait page."""
    sig, doc, file_, line = "", "", "", 0
    idx = html.find(f'id="{anchor}"')
    if idx < 0: return sig, doc, file_, line
    chunk = html[max(0, idx - 10):idx + 2000]
    m = SOURCE_LINK_RE.search(chunk)
    if m: file_, line = m.group(1), int(m.group(2))
    m = re.search(r'<h4[^>]*class="code-header"[^>]*>(.*?)</h4>', chunk, re.DOTALL)
    if m: sig = re.sub(r"\s+", " ", strip_tags(m.group(1))).strip()
    m = re.search(r'<div[^>]*class="docblock"[^>]*>(.*?)</div>', html[idx:idx + 3000], re.DOTALL)
    if m: doc = m.group(1).strip()
    return sig, doc, file_, line


def _is_public(sig: str) -> bool:
    """True unless the scraped signature positively shows it isn't --
    a structural signal (the exact text rustdoc renders for the item's
    own declaration), not a separate lookup: rustdoc always prints the
    effective visibility qualifier verbatim (`pub fn ...`, `pub(crate) fn
    ...`, or no qualifier at all for private). `pub(crate)`/`pub(super)`/
    `pub(in ...)` are treated as "not public" here too, alongside truly
    private -- none of them are visible from *outside* the crate, which is
    what this toggle is actually distinguishing (`sig.startswith("pub ")`
    is false for all of them, only true for a bare, unrestricted `pub`).
    Only ever generated for pages that exist at all if `cargo doc
    --document-private-items` was used (see __main__.py's `--include-
    private`) -- without it, no private item has a page to scrape a
    signature from in the first place. An empty/unknown signature defaults
    to True: don't hide something that couldn't be classified, only hide
    what's positively identified as restricted."""
    return not sig or sig.startswith("pub ")


def _is_trait_impl_method(html: str, idx: int) -> bool:
    """True if the method anchor found at `idx` (see extract_method) falls
    under rustdoc's own "Trait Implementations" section of a type's page
    rather than its "Implementations" (inherent) one -- found by whichever
    of the two section headers' own anchors (`id="implementations"`,
    `id="trait-implementations"`, both stable rustdoc HTML conventions)
    appears closest before `idx`. This matters for `_is_public` above:
    a trait-impl method's own declaration NEVER carries an explicit `pub`
    (Rust doesn't allow re-declaring visibility inside a trait impl block
    at all -- it's exactly as visible as the trait + the implementing type
    already are), so the plain text heuristic alone would misclassify
    every trait method as "private", even one implementing an ordinary
    public trait on a public type -- confirmed as a real, not just
    theoretical, false positive on the dummy-lib fixture's own `Summable`
    trait (`Pair::total`, `Batch::total` both came back "private" before
    this check existed). A trait method found this way is always treated
    as public regardless of its own missing `pub` -- correct for the
    overwhelmingly common case (a public trait), and no worse than the
    plain heuristic would be for the rarer case of a *private* trait's
    own methods, which have no doc page to find in the first place unless
    `--document-private-items` was used, at which point they're at least
    consistently treated the same way genuinely-public trait impls are,
    not silently miscategorized as some third, unhandled state."""
    if idx < 0:
        return False
    inherent = html.rfind('id="implementations"', 0, idx)
    trait_impl = html.rfind('id="trait-implementations"', 0, idx)
    return trait_impl > inherent


def build_index(crates, graph: dict) -> dict:
    """crates: an iterable of (doc_root, src_root, crate_name) triples --
    one per crate whose docs should be cross-referenced (e.g. every member
    of the dependency closure the call-graph was merged from, see
    mir_graph.build_graph). doc_root is that crate's target/doc/<crate>/ ;
    src_root is that same crate's own src/ dir (needed so a source link
    resolves against the right crate, not whichever one happens to be the
    "primary" target); crate_name is what `cargo doc` named that directory
    (see codemap.crate_name -- may differ from the package name).

    graph: a parsed graph.json. Method-doc entries (below) are keyed
    exactly as `mir_graph.py` qualifies its own node ids
    (`crate::Type::method`) so a click on a graph node's method resolves
    to the one real doc page for that specific crate's type, not
    whichever crate's same-named type happened to be indexed. Whole-item
    (struct/enum/trait/free-fn) entries additionally get a crate-qualified
    key the same way (`crate::Name`) -- required for free functions, which
    are graph nodes themselves and must match `crate::free_fn` exactly;
    for struct/enum/trait (never graph nodes on their own, only their
    methods are) the *bare* name is also kept as a convenience key, but
    only for the first crate that defines it -- letting the viewer's
    signature-linkifier keep matching a bare type name found in rendered
    signature text (which never shows a crate qualifier) for the common,
    non-colliding case, without resurrecting the old silent last-crate-
    wins behavior for the colliding one: the second crate's same-named
    struct/enum/trait is still fully indexed under its own qualified key,
    just not reachable via the unqualified one."""

    def to_entry(sig, doc, file_, line, src_root, cname, kind, html_path, doc_root, anchor="", public=None):
        abs_path = str((src_root / file_).resolve()).replace("\\", "/") if file_ else ""
        vscode_url = f"vscode://file/{abs_path}:{line}:1" if line and abs_path else ""
        # Path of the actual `cargo doc` HTML page, relative to the shared
        # target/doc/ root (doc_root's own parent) -- e.g. "dapi/fn.foo.html".
        # A viewer showing this natively needs it served from that same
        # root (see `codemap serve --docs`), since relative asset links on
        # the page itself (css/fonts under a shared static.files/ dir one
        # level up) only resolve correctly when the mount point lines up
        # with doc_root's parent, not doc_root itself. `anchor` (methods
        # only -- their own page is the *type's* page) lets the viewer jump
        # straight to that method instead of the top of the type's page.
        doc_page = str(Path(cname) / html_path.relative_to(doc_root)).replace("\\", "/")
        return {
            "signature": sig,
            "docHtml": doc,
            "file": f"src/{file_}" if file_ else "",
            "line": line,
            # Same absolute path already computed for vscodePath, exposed on
            # its own -- lets trace_log.py match a tracing span back to this
            # node by real source location (file + line, scanned forward
            # past the #[instrument] attribute to the actual fn) instead of
            # by span name, which a method's default instrumented name
            # never matches (bare method name, no "Type::" qualifier) and
            # a custom `#[instrument(name = "...")]` can be anything at all.
            "absPath": abs_path,
            "vscodePath": vscode_url,
            "crate": cname,
            "kind": kind,
            "docPage": doc_page,
            "anchor": anchor,
            "public": _is_public(sig) if public is None else public,
        }

    # Discover every doc page cargo actually generated, across every crate,
    # keyed by (crate name, item name) -> (html path, crate's src/ dir,
    # kind, that crate's own doc_root) -- crate-qualified from the start,
    # so two crates' same-named items never overwrite each other here.
    pages = {}
    for doc_root, src_root, cname in crates:
        for html_path in doc_root.rglob("*.html"):
            m = ITEM_FILE_RE.match(html_path.name)
            if not m: continue
            kind, name = m.groups()
            pages[(cname, name)] = (html_path, src_root, kind, doc_root)

    index = {}

    # Whole-item doc (struct/enum/trait/free fn). Always indexed under its
    # crate-qualified key (`crate::Name`) -- the only key a free fn can use,
    # since it's also a graph node id and must match `mir_graph.py`'s own
    # `crate::free_fn` exactly. struct/enum/trait entries additionally get
    # the bare `Name` key too, but only from whichever crate is processed
    # first for that name (dict iteration order == insertion order here) --
    # this is what makes a type name found inside another signature
    # clickable without needing to know which crate's page to open; a
    # second crate's same-named type is still fully indexed under its own
    # qualified key, it just isn't the one the bare-name shortcut reaches.
    for (cname, name), (path, src_root, kind, doc_root) in pages.items():
        html = path.read_text(encoding="utf-8", errors="ignore")
        sig, doc, file_, line = extract_top(html)
        if not (sig or doc):
            continue
        entry = to_entry(sig, doc, file_, line, src_root, cname, kind, path, doc_root)
        index[f"{cname}::{name}"] = entry
        if kind != "fn" and name not in index:
            index[name] = entry

    # Method doc ("crate::Type::method"), for every such node actually
    # present in graph.json -- crate-qualified the same way mir_graph.py's
    # own node ids are, so this always resolves against the *specific*
    # crate's type page, never a same-named type in some other crate.
    for node in graph.get("nodes", []):
        nid = node["data"]["id"]
        if nid.count("::") < 2: continue  # a free fn ("crate::name"), not a method
        cname, type_name, method = nid.split("::", 2)
        entry = pages.get((cname, type_name))
        if not entry: continue
        path, src_root, _type_kind, doc_root = entry
        html = path.read_text(encoding="utf-8", errors="ignore")
        anchor = f"method.{method}"
        sig, doc, file_, line = extract_method(html, anchor)
        if not (sig or doc):
            anchor = f"tymethod.{method}"
            sig, doc, file_, line = extract_method(html, anchor)
        if sig or doc:
            # A trait-impl method's own text never carries `pub` (Rust
            # forbids re-declaring visibility there) -- _is_public alone
            # would misclassify every one of them as private, including an
            # ordinary public trait's. See _is_trait_impl_method's own
            # docstring for why this needs the HTML section it's under,
            # not just its own signature text.
            idx = html.find(f'id="{anchor}"')
            public = True if _is_trait_impl_method(html, idx) else None
            index[nid] = to_entry(sig, doc, file_, line, src_root, cname, "method", path, doc_root, anchor, public=public)

    return index
