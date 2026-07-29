//! doc_index.rs -- Rust port of `src/codemap/doc_index.py`. Cross-reference
//! `cargo doc` HTML output with a call-graph. Everything is discovered
//! dynamically from the HTML files cargo doc actually produced
//! (struct.Name.html, enum.Name.html, trait.Name.html, fn.name.html) and
//! from the node ids present in a graph.json -- no per-project name table.

use once_cell::sync::Lazy;
use regex::Regex;
use serde_json::{json, Map, Value};
use std::collections::HashMap;
use std::path::{Path, PathBuf};

static ITEM_FILE_RE: Lazy<Regex> =
    Lazy::new(|| Regex::new(r"^(struct|enum|trait|fn)\.([A-Za-z_]\w*)\.html$").unwrap());
// cargo doc source links always look like ".../src/<crate_name>/<path>.rs.html#<line>".
// The crate-name segment is skipped generically -- never hardcoded.
static SOURCE_LINK_RE: Lazy<Regex> =
    Lazy::new(|| Regex::new(r#"href="[^"]*/src/[^/"]+/([^"]+?\.rs)\.html#(\d+)"#).unwrap());
static TAG_RE: Lazy<Regex> = Lazy::new(|| Regex::new(r"<[^>]+>").unwrap());
static ITEM_DECL_RE: Lazy<Regex> =
    Lazy::new(|| Regex::new(r#"(?s)<pre[^>]*class="rust item-decl"[^>]*>.*?</pre>"#).unwrap());
static CODE_RE: Lazy<Regex> = Lazy::new(|| Regex::new(r"(?s)<code>(.*?)</code>").unwrap());
static DOCBLOCK_RE: Lazy<Regex> =
    Lazy::new(|| Regex::new(r#"(?s)<div[^>]*class="docblock"[^>]*>(.*?)</div>"#).unwrap());
static WHITESPACE_RE: Lazy<Regex> = Lazy::new(|| Regex::new(r"\s+").unwrap());
static CODE_HEADER_RE: Lazy<Regex> =
    Lazy::new(|| Regex::new(r#"(?s)<h4[^>]*class="code-header"[^>]*>(.*?)</h4>"#).unwrap());

/// Nearest valid char boundary at or before `idx` -- needed because every
/// slice bound below is a fixed byte offset (+2000/+3000/-10) applied to a
/// cargo-doc HTML page, which can land mid-character on any non-ASCII byte
/// sequence (an accented name in a doc comment, a unicode symbol, ...) and
/// panic `&html[a..b]` otherwise. `str::floor_char_boundary` is nightly-only,
/// hence this stable equivalent; walking backward from `idx` always
/// terminates since byte 0 is always a boundary.
fn floor_char_boundary(s: &str, idx: usize) -> usize {
    let mut idx = idx.min(s.len());
    while idx > 0 && !s.is_char_boundary(idx) {
        idx -= 1;
    }
    idx
}

fn strip_tags(html: &str) -> String {
    TAG_RE
        .replace_all(html, "")
        .replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&#xa7;", "")
        .trim()
        .to_string()
}

struct Extracted {
    sig: String,
    doc: String,
    file: String,
    line: u32,
}

/// Signature/doc/source-link from the top of an item page (struct, enum,
/// trait or fn).
fn extract_top(html: &str) -> Extracted {
    let mut out = Extracted { sig: String::new(), doc: String::new(), file: String::new(), line: 0 };
    let Some(m_decl) = ITEM_DECL_RE.find(html) else { return out };
    if let Some(code_m) = CODE_RE.captures(m_decl.as_str()) {
        out.sig = WHITESPACE_RE.replace_all(&strip_tags(&code_m[1]), " ").trim().to_string();
    }
    // The item's own doc comment (if any) sits immediately after
    // item-decl. If a section header (Fields / Implementations / ...)
    // shows up first, there's no doc for the item itself -- don't fall
    // through to some other item's docblock further down the same page.
    let tail_end = floor_char_boundary(html, m_decl.end() + 3000);
    let tail = &html[m_decl.end()..tail_end];
    let h2_pos = tail.find("<h2");
    if let Some(doc_m) = DOCBLOCK_RE.captures(tail) {
        let doc_start = doc_m.get(0).unwrap().start();
        if h2_pos.is_none() || doc_start < h2_pos.unwrap() {
            out.doc = doc_m[1].trim().to_string();
        }
    }
    if let Some(m) = SOURCE_LINK_RE.captures(html) {
        out.file = m[1].to_string();
        out.line = m[2].parse().unwrap_or(0);
    }
    out
}

/// Signature/doc/source-link for a specific method anchor on a
/// struct/trait page.
fn extract_method(html: &str, anchor: &str) -> Extracted {
    let mut out = Extracted { sig: String::new(), doc: String::new(), file: String::new(), line: 0 };
    let needle = format!(r#"id="{anchor}""#);
    let Some(idx) = html.find(&needle) else { return out };
    let chunk_start = floor_char_boundary(html, idx.saturating_sub(10));
    let chunk_end = floor_char_boundary(html, idx + 2000);
    let chunk = &html[chunk_start..chunk_end];
    if let Some(m) = SOURCE_LINK_RE.captures(chunk) {
        out.file = m[1].to_string();
        out.line = m[2].parse().unwrap_or(0);
    }
    if let Some(m) = CODE_HEADER_RE.captures(chunk) {
        out.sig = WHITESPACE_RE.replace_all(&strip_tags(&m[1]), " ").trim().to_string();
    }
    let doc_end = floor_char_boundary(html, idx + 3000);
    if let Some(m) = DOCBLOCK_RE.captures(&html[idx..doc_end]) {
        out.doc = m[1].trim().to_string();
    }
    out
}

/// True unless the scraped signature positively shows it isn't -- see the
/// Python original's docstring for the full rationale (rustdoc always
/// prints the effective visibility qualifier verbatim).
fn is_public(sig: &str) -> bool {
    sig.is_empty() || sig.starts_with("pub ")
}

/// True if the method anchor found at `idx` falls under rustdoc's own
/// "Trait Implementations" section rather than its "Implementations"
/// (inherent) one -- a trait-impl method's own declaration never carries
/// an explicit `pub` (Rust forbids re-declaring visibility there), so the
/// plain text heuristic alone would misclassify every one of them as
/// private, even one implementing an ordinary public trait on a public
/// type.
fn is_trait_impl_method(html: &str, idx: Option<usize>) -> bool {
    let Some(idx) = idx else { return false };
    let inherent = html[..idx].rfind(r#"id="implementations""#);
    let trait_impl = html[..idx].rfind(r#"id="trait-implementations""#);
    match (trait_impl, inherent) {
        (Some(t), Some(i)) => t > i,
        (Some(_), None) => true,
        _ => false,
    }
}

/// (doc_root, src_root, crate_name) triple -- one per crate whose docs
/// should be cross-referenced.
pub struct CrateDocs {
    pub doc_root: PathBuf,
    pub src_root: PathBuf,
    pub crate_name: String,
}

fn walk_html_files(dir: &Path, out: &mut Vec<PathBuf>) {
    let Ok(entries) = std::fs::read_dir(dir) else { return };
    for entry in entries.flatten() {
        let path = entry.path();
        if path.is_dir() {
            walk_html_files(&path, out);
        } else if path.extension().is_some_and(|e| e == "html") {
            out.push(path);
        }
    }
}

#[allow(clippy::too_many_arguments)]
fn to_entry(
    sig: &str,
    doc: &str,
    file: &str,
    line: u32,
    src_root: &Path,
    cname: &str,
    kind: &str,
    html_path: &Path,
    doc_root: &Path,
    anchor: &str,
    public_override: Option<bool>,
) -> Value {
    let abs_path = if !file.is_empty() {
        // `dunce::canonicalize`, not `std::fs::canonicalize` -- the std
        // version returns a `\\?\C:\...` verbatim path on Windows, which a
        // blind backslash->forward-slash replace mangles into `//?/C:/...`
        // (confirmed as a real bug: this silently broke every file+line
        // lookup in trace_log.rs, since the trace's own `filename` field
        // never carries that prefix, so the two never matched and every
        // span resolution quietly fell back to the raw span name). `dunce`
        // strips the prefix when it's safe to (the common case), matching
        // what Python's `Path.resolve()` produces.
        dunce::canonicalize(src_root.join(file))
            .map(|p| p.to_string_lossy().replace('\\', "/"))
            .unwrap_or_default()
    } else {
        String::new()
    };
    let vscode_url = if line > 0 && !abs_path.is_empty() {
        format!("vscode://file/{abs_path}:{line}:1")
    } else {
        String::new()
    };
    // Path of the actual cargo doc HTML page, relative to the shared
    // target/doc/ root (doc_root's own parent) -- e.g. "dapi/fn.foo.html".
    let rel = html_path.strip_prefix(doc_root).unwrap_or(html_path);
    let doc_page = Path::new(cname).join(rel).to_string_lossy().replace('\\', "/");
    json!({
        "signature": sig,
        "docHtml": doc,
        "file": if file.is_empty() { String::new() } else { format!("src/{file}") },
        "line": line,
        "absPath": abs_path,
        "vscodePath": vscode_url,
        "crate": cname,
        "kind": kind,
        "docPage": doc_page,
        "anchor": anchor,
        "public": public_override.unwrap_or_else(|| is_public(sig)),
    })
}

/// `crates`: one per crate whose docs should be cross-referenced (e.g.
/// every member of the dependency closure the call-graph was merged
/// from). `graph`: a parsed graph.json. See the Python original's own
/// docstring for the full key-shape rationale (crate-qualified keys,
/// first-crate-wins bare-name convenience key for struct/enum/trait).
pub fn build_index(crates: &[CrateDocs], graph: &Value) -> Map<String, Value> {
    // Discover every doc page cargo actually generated, across every
    // crate, keyed by (crate name, item name) -> (html path, src_root,
    // kind, doc_root).
    let mut pages: HashMap<(String, String), (PathBuf, PathBuf, String, PathBuf)> = HashMap::new();
    for c in crates {
        let mut html_files = Vec::new();
        walk_html_files(&c.doc_root, &mut html_files);
        for html_path in html_files {
            let Some(file_name) = html_path.file_name().and_then(|n| n.to_str()) else { continue };
            let Some(m) = ITEM_FILE_RE.captures(file_name) else { continue };
            let kind = m[1].to_string();
            let name = m[2].to_string();
            pages.insert(
                (c.crate_name.clone(), name),
                (html_path, c.src_root.clone(), kind, c.doc_root.clone()),
            );
        }
    }

    let mut index: Map<String, Value> = Map::new();

    // Whole-item doc (struct/enum/trait/free fn).
    for ((cname, name), (path, src_root, kind, doc_root)) in &pages {
        let Ok(html) = std::fs::read_to_string(path) else { continue };
        let ext = extract_top(&html);
        if ext.sig.is_empty() && ext.doc.is_empty() {
            continue;
        }
        let entry = to_entry(
            &ext.sig, &ext.doc, &ext.file, ext.line, src_root, cname, kind, path, doc_root, "", None,
        );
        index.insert(format!("{cname}::{name}"), entry.clone());
        if kind != "fn" && !index.contains_key(name) {
            index.insert(name.clone(), entry);
        }
    }

    // Method doc ("crate::Type::method"), for every such node actually
    // present in graph.json.
    if let Some(nodes) = graph.get("nodes").and_then(Value::as_array) {
        for node in nodes {
            let Some(nid) = node.get("data").and_then(|d| d.get("id")).and_then(Value::as_str) else { continue };
            if nid.matches("::").count() < 2 {
                continue; // a free fn ("crate::name"), not a method
            }
            let parts: Vec<&str> = nid.splitn(3, "::").collect();
            let (cname, type_name, method) = (parts[0], parts[1], parts[2]);
            let Some((path, src_root, _type_kind, doc_root)) =
                pages.get(&(cname.to_string(), type_name.to_string()))
            else {
                continue;
            };
            let Ok(html) = std::fs::read_to_string(path) else { continue };
            let mut anchor = format!("method.{method}");
            let mut ext = extract_method(&html, &anchor);
            if ext.sig.is_empty() && ext.doc.is_empty() {
                anchor = format!("tymethod.{method}");
                ext = extract_method(&html, &anchor);
            }
            if !ext.sig.is_empty() || !ext.doc.is_empty() {
                let idx = html.find(&format!(r#"id="{anchor}""#));
                let public = if is_trait_impl_method(&html, idx) { Some(true) } else { None };
                let entry = to_entry(
                    &ext.sig, &ext.doc, &ext.file, ext.line, src_root, cname, "method", path, doc_root,
                    &anchor, public,
                );
                index.insert(nid.to_string(), entry);
            }
        }
    }

    index
}
