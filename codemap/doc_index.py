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


def build_index(crates, graph: dict) -> dict:
    """crates: an iterable of (doc_root, src_root) pairs -- one per crate
    whose docs should be cross-referenced (e.g. every member of the
    workspace the call-graph was merged from, see mir_graph.build_graph).
    doc_root is that crate's target/doc/<crate>/ ; src_root is that same
    crate's own src/ dir (needed so a source link resolves against the
    right crate, not whichever one happens to be the "primary" target).

    graph: a parsed graph.json (only its node ids are used). Node ids
    aren't crate-qualified (see mir_graph), so if the same name exists in
    two crates' docs, the same collision the call-graph itself has applies
    here too -- last crate processed wins, silently."""

    def to_entry(sig, doc, file_, line, src_root):
        abs_path = str((src_root / file_).resolve()).replace("\\", "/") if file_ else ""
        vscode_url = f"vscode://file/{abs_path}:{line}:1" if line and abs_path else ""
        return {
            "signature": sig,
            "docHtml": doc,
            "file": f"src/{file_}" if file_ else "",
            "line": line,
            "vscodePath": vscode_url,
        }

    # Discover every doc page cargo actually generated, across every crate,
    # keyed by item name -> (html path, that crate's own src/ dir).
    pages = {}
    for doc_root, src_root in crates:
        for html_path in doc_root.rglob("*.html"):
            m = ITEM_FILE_RE.match(html_path.name)
            if not m: continue
            _kind, name = m.groups()
            pages[name] = (html_path, src_root)

    index = {}

    # Whole-item doc (struct/enum/trait/free fn), keyed by its own name --
    # this is what makes a type name found inside another signature clickable.
    for name, (path, src_root) in pages.items():
        html = path.read_text(encoding="utf-8", errors="ignore")
        sig, doc, file_, line = extract_top(html)
        if sig or doc:
            index[name] = to_entry(sig, doc, file_, line, src_root)

    # Method doc ("Type::method"), for every such node actually present in graph.json.
    for node in graph.get("nodes", []):
        nid = node["data"]["id"]
        if "::" not in nid: continue
        type_name, method = nid.split("::", 1)
        entry = pages.get(type_name)
        if not entry: continue
        path, src_root = entry
        html = path.read_text(encoding="utf-8", errors="ignore")
        sig, doc, file_, line = extract_method(html, f"method.{method}")
        if not (sig or doc):
            sig, doc, file_, line = extract_method(html, f"tymethod.{method}")
        if sig or doc:
            index[nid] = to_entry(sig, doc, file_, line, src_root)

    return index
