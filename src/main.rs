//! cargo-codemap -- the primary implementation, invoked via `cargo codemap`.
//! Originated as a Rust port of `python -m codemap`; the Python original is
//! kept for reference under `archive/codemap-python/`, not actively
//! developed -- see that file's own module docstring for background on the
//! subcommands this ported from.

mod doc_index;
mod mir_graph;
mod schema_check;
mod trace_log;

use clap::{Parser, Subcommand};
use serde_json::{json, Value};
use std::collections::HashMap;
use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::sync::Arc;

#[derive(Parser)]
#[command(name = "cargo-codemap", about = "Interactive call-graph viewer for Rust codebases, with execution replay from real tracing logs.")]
struct Cli {
    #[command(subcommand)]
    command: Cmd,
}

#[derive(Subcommand)]
enum Cmd {
    /// Build (if the graph/doc index are missing or stale) and serve the viewer
    Run {
        /// Path to the target crate (dir containing Cargo.toml)
        #[arg(long, default_value = ".")]
        project: String,
        #[arg(long, default_value_t = 8787)]
        port: u16,
        #[arg(long)]
        no_browser: bool,
    },
    /// Generate the graph + doc index for a project (always, unconditionally)
    Build {
        /// Path to the target crate (dir containing Cargo.toml)
        #[arg(long, default_value = ".")]
        project: String,
    },
    /// Check a trace.jsonl against the written trace-format schema
    ValidateTrace { trace: String },
}

// ── cargo metadata / manifest helpers ──────────────────────────────────

fn cargo_metadata(manifest: &Path) -> Value {
    let out = Command::new("cargo")
        .args(["metadata", "--no-deps", "--format-version", "1", "--manifest-path"])
        .arg(manifest)
        .output()
        .expect("failed to run cargo metadata");
    if !out.status.success() {
        eprintln!("{}", String::from_utf8_lossy(&out.stderr));
        std::process::exit(1);
    }
    serde_json::from_slice(&out.stdout).expect("cargo metadata did not return valid JSON")
}

fn require_manifest(project: &str) -> PathBuf {
    let manifest = dunce::canonicalize(project)
        .unwrap_or_else(|_| PathBuf::from(project))
        .join("Cargo.toml");
    if !manifest.exists() {
        eprintln!("ERROR: no Cargo.toml at {}", manifest.display());
        std::process::exit(1);
    }
    manifest
}

fn read_package_name(manifest: &Path) -> String {
    let text = fs::read_to_string(manifest).unwrap_or_default();
    let doc: toml::Value = text.parse().unwrap_or_else(|e| {
        eprintln!("ERROR: could not parse {}: {e}", manifest.display());
        std::process::exit(1);
    });
    doc.get("package")
        .and_then(|p| p.get("name"))
        .and_then(toml::Value::as_str)
        .map(str::to_string)
        .unwrap_or_else(|| {
            eprintln!("ERROR: could not find [package] name = \"...\" in {}", manifest.display());
            std::process::exit(1);
        })
}

/// The actual compiled crate name: `[lib] name` if the manifest overrides
/// it, else `[package]` name with hyphens turned into underscores (cargo's
/// own default when there's no override). Some real projects override
/// this on every crate -- confirmed to silently break MIR/doc lookup
/// otherwise, since cargo then names the artifact after the override, not
/// the package name.
fn crate_name(manifest: &Path) -> String {
    let text = fs::read_to_string(manifest).unwrap_or_default();
    if let Ok(doc) = text.parse::<toml::Value>() {
        if let Some(name) = doc.get("lib").and_then(|l| l.get("name")).and_then(toml::Value::as_str) {
            return name.to_string();
        }
    }
    read_package_name(manifest).replace('-', "_")
}

/// Bare directory-name marker(s) for wherever any `.cargo/config.toml` in
/// scope (walking upward from the manifest, plus $CARGO_HOME) redirects a
/// `[source.*]` to via `directory = "..."` -- i.e. wherever `cargo vendor`
/// put its copy of external dependency source. Passed to mir_graph as
/// extra "this path is external" markers.
fn find_vendor_dirs(manifest: &Path) -> Vec<String> {
    let mut candidates = Vec::new();
    let mut cur = dunce::canonicalize(manifest.parent().unwrap_or(Path::new("."))).unwrap_or_default();
    loop {
        candidates.push(cur.join(".cargo").join("config.toml"));
        candidates.push(cur.join(".cargo").join("config"));
        match cur.parent() {
            Some(p) if p != cur => cur = p.to_path_buf(),
            _ => break,
        }
    }
    let cargo_home = std::env::var("CARGO_HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|_| dirs_home().join(".cargo"));
    candidates.push(cargo_home.join("config.toml"));
    candidates.push(cargo_home.join("config"));

    let mut markers = Vec::new();
    for config_path in candidates {
        let Ok(text) = fs::read_to_string(&config_path) else { continue };
        let Ok(doc) = text.parse::<toml::Value>() else { continue };
        let Some(table) = doc.as_table() else { continue };
        for (key, value) in table {
            if !key.starts_with("source.") {
                continue;
            }
            if let Some(dir) = value.get("directory").and_then(toml::Value::as_str) {
                let name = Path::new(&dir.replace('\\', "/"))
                    .file_name()
                    .and_then(|n| n.to_str())
                    .unwrap_or("")
                    .to_string();
                if !name.is_empty() && !markers.contains(&name) {
                    markers.push(name);
                }
            }
        }
    }
    markers
}

fn dirs_home() -> PathBuf {
    std::env::var("USERPROFILE")
        .or_else(|_| std::env::var("HOME"))
        .map(PathBuf::from)
        .unwrap_or_default()
}

/// Every package in the target crate's own transitive dependency graph
/// that's local (a path dependency, not crates.io/a git registry) --
/// including the target crate itself. Deliberately *not* "every member of
/// the workspace" -- see the Python original's own docstring for the real
/// bug this avoids (a client binary depending ON this crate getting swept
/// in just because `cargo build --workspace` would build it too).
fn local_dependency_closure(manifest: &Path) -> Vec<Value> {
    let out = Command::new("cargo")
        .args(["metadata", "--format-version", "1", "--manifest-path"])
        .arg(manifest)
        .output()
        .expect("failed to run cargo metadata");
    if !out.status.success() {
        eprintln!("{}", String::from_utf8_lossy(&out.stderr));
        std::process::exit(1);
    }
    let meta: Value = serde_json::from_slice(&out.stdout).expect("cargo metadata did not return valid JSON");
    let resolve = &meta["resolve"];
    let root = resolve.get("root").and_then(Value::as_str);
    let Some(root) = root else {
        eprintln!(
            "ERROR: {} has no resolvable root package (pointing --project at a virtual workspace manifest?)",
            manifest.display()
        );
        std::process::exit(1);
    };

    let nodes_by_id: HashMap<&str, &Value> = resolve["nodes"]
        .as_array()
        .unwrap()
        .iter()
        .map(|n| (n["id"].as_str().unwrap(), n))
        .collect();

    let mut closure: std::collections::HashSet<String> = std::collections::HashSet::new();
    let mut stack = vec![root.to_string()];
    while let Some(pid) = stack.pop() {
        if closure.contains(&pid) {
            continue;
        }
        closure.insert(pid.clone());
        if let Some(node) = nodes_by_id.get(pid.as_str()) {
            for dep in node["deps"].as_array().unwrap() {
                stack.push(dep["pkg"].as_str().unwrap().to_string());
            }
        }
    }

    let packages_by_id: HashMap<&str, &Value> = meta["packages"]
        .as_array()
        .unwrap()
        .iter()
        .map(|p| (p["id"].as_str().unwrap(), p))
        .collect();

    closure
        .iter()
        .filter(|pid| pid.starts_with("path+file://"))
        .filter_map(|pid| packages_by_id.get(pid.as_str()).map(|p| (*p).clone()))
        .collect()
}

/// <target_directory>/rust-codemap/<crate_name>/ -- nested under the
/// crate's own name, not just "rust-codemap/", since a workspace's members
/// all share one target_directory.
fn default_out_dir(manifest: &Path) -> PathBuf {
    let target_dir = PathBuf::from(cargo_metadata(manifest)["target_directory"].as_str().unwrap());
    target_dir.join("rust-codemap").join(crate_name(manifest))
}

fn write_json(path: &Path, data: &Value) {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent).unwrap();
    }
    fs::write(path, serde_json::to_string_pretty(data).unwrap()).unwrap();
}

// ── graph ────────────────────────────────────────────────────────────────

/// A lib's .mir carries a hash suffix (name-<hash>.mir); a bin's doesn't
/// (name.mir). Match on the crate's own name first -- picking "whatever
/// .mir is newest" breaks the moment some OTHER crate in the same shared
/// target/deps dir got rebuilt more recently. mtime only breaks ties among
/// this crate's own artifacts.
fn find_mir_file(deps_dir: &Path, name: &str) -> Option<PathBuf> {
    let Ok(entries) = fs::read_dir(deps_dir) else { return None };
    let mut candidates: Vec<(PathBuf, std::time::SystemTime)> = Vec::new();
    let bare = format!("{name}.mir");
    let prefix = format!("{name}-");
    for entry in entries.flatten() {
        let path = entry.path();
        let Some(fname) = path.file_name().and_then(|f| f.to_str()) else { continue };
        let matches = fname == bare || (fname.starts_with(&prefix) && fname.ends_with(".mir"));
        if matches {
            if let Ok(meta) = entry.metadata() {
                if let Ok(mtime) = meta.modified() {
                    candidates.push((path, mtime));
                }
            }
        }
    }
    candidates.sort_by(|a, b| b.1.cmp(&a.1));
    candidates.into_iter().next().map(|(p, _)| p)
}

fn cmd_graph(project: &str, out: Option<&str>) -> PathBuf {
    let manifest = require_manifest(project);
    let target_dir = PathBuf::from(cargo_metadata(&manifest)["target_directory"].as_str().unwrap());
    let out_path = out.map(PathBuf::from).unwrap_or_else(|| default_out_dir(&manifest).join("graph.json"));

    let packages = local_dependency_closure(&manifest);
    let members: Vec<(String, String)> = packages
        .iter()
        .map(|pkg| {
            let pkg_name = pkg["name"].as_str().unwrap().to_string();
            let manifest_path = PathBuf::from(pkg["manifest_path"].as_str().unwrap());
            (pkg_name, crate_name(&manifest_path))
        })
        .collect();
    println!(
        "Local dependency closure: {}",
        members.iter().map(|(n, _)| n.as_str()).collect::<Vec<_>>().join(", ")
    );

    println!("Building with RUSTFLAGS=--emit=mir ...");
    let mut build_args = vec!["build".to_string(), "--manifest-path".to_string(), manifest.to_string_lossy().to_string()];
    for (pkg_name, _) in &members {
        build_args.push("-p".to_string());
        build_args.push(pkg_name.clone());
    }
    let status = Command::new("cargo")
        .args(&build_args)
        .env("RUSTFLAGS", "--emit=mir")
        .status()
        .expect("failed to run cargo build");
    if !status.success() {
        std::process::exit(1);
    }

    let deps_dir = target_dir.join("debug").join("deps");
    let mut crate_texts: HashMap<String, String> = HashMap::new();
    for (pkg_name, cname) in &members {
        let Some(mir_path) = find_mir_file(&deps_dir, cname) else {
            println!("  WARNING: no .mir found for {pkg_name} ({cname}) -- skipping");
            continue;
        };
        let size_kb = fs::metadata(&mir_path).map(|m| m.len() / 1024).unwrap_or(0);
        println!("  {pkg_name}: {} ({size_kb} KB)", mir_path.file_name().unwrap().to_string_lossy());
        crate_texts.insert(cname.clone(), fs::read_to_string(&mir_path).unwrap_or_default());
    }
    if crate_texts.is_empty() {
        eprintln!("ERROR: no .mir file found for any dependency-closure member under {}", deps_dir.display());
        std::process::exit(1);
    }

    let vendor_dirs = find_vendor_dirs(&manifest);
    let graph = mir_graph::build_graph(&crate_texts, &vendor_dirs);
    write_json(&out_path, &graph);
    let traced = graph["nodes"].as_array().unwrap().iter().filter(|n| n["data"]["traced"] == true).count();
    println!(
        "OK {}  ({} nodes, {} edges, {traced} traced, {} crate(s) merged)",
        out_path.display(),
        graph["nodes"].as_array().unwrap().len(),
        graph["edges"].as_array().unwrap().len(),
        crate_texts.len()
    );
    out_path
}

// ── doc ──────────────────────────────────────────────────────────────────

fn cmd_doc(project: &str, graph_arg: Option<&str>, out: Option<&str>) -> PathBuf {
    let manifest = require_manifest(project);
    let target_dir = PathBuf::from(cargo_metadata(&manifest)["target_directory"].as_str().unwrap());
    let default_dir = default_out_dir(&manifest);
    let graph_path = graph_arg.map(PathBuf::from).unwrap_or_else(|| default_dir.join("graph.json"));
    let out_path = out.map(PathBuf::from).unwrap_or_else(|| default_dir.join("source_index.json"));

    let packages = local_dependency_closure(&manifest);
    let mut doc_cmd = vec!["doc".to_string(), "--no-deps".to_string(), "--manifest-path".to_string(), manifest.to_string_lossy().to_string()];
    for pkg in &packages {
        doc_cmd.push("-p".to_string());
        doc_cmd.push(pkg["name"].as_str().unwrap().to_string());
    }
    // Always on, not a flag: a private #[instrument]'d fn with no doc page
    // has no source_index entry, so trace_log can't resolve its span to a
    // qualified node id -- it silently drops out of replay instead of
    // erroring, which is worse than always paying for private-item docs.
    doc_cmd.push("--document-private-items".to_string());
    println!("Running cargo doc --no-deps --document-private-items ...");
    let status = Command::new("cargo").args(&doc_cmd).status().expect("failed to run cargo doc");
    if !status.success() {
        std::process::exit(1);
    }

    let mut crates = Vec::new();
    for pkg in &packages {
        let manifest_path = PathBuf::from(pkg["manifest_path"].as_str().unwrap());
        let cname = crate_name(&manifest_path);
        let doc_root = target_dir.join("doc").join(&cname);
        if doc_root.exists() {
            crates.push(doc_index::CrateDocs {
                doc_root,
                src_root: manifest_path.parent().unwrap().join("src"),
                crate_name: cname,
            });
        } else {
            println!("  WARNING: no doc output for {} ({cname}) -- skipping", pkg["name"].as_str().unwrap());
        }
    }
    if crates.is_empty() {
        eprintln!("ERROR: no cargo doc output found under {}", target_dir.join("doc").display());
        std::process::exit(1);
    }

    let graph: Value = if graph_path.exists() {
        serde_json::from_str(&fs::read_to_string(&graph_path).unwrap()).unwrap_or(json!({"nodes": []}))
    } else {
        json!({"nodes": []})
    };

    let index = doc_index::build_index(&crates, &graph);
    let private_count = index.values().filter(|e| e["public"] == false).count();
    write_json(&out_path, &Value::Object(index.clone()));
    println!(
        "OK {}  ({} entries, {private_count} private, {} crate(s) cross-referenced)",
        out_path.display(),
        index.len(),
        crates.len()
    );
    out_path
}

// ── build (graph + doc, always together) ───────────────────────────────────

fn cmd_build(project: &str) -> (PathBuf, PathBuf) {
    let graph_path = cmd_graph(project, None);
    let doc_path = cmd_doc(project, Some(graph_path.to_string_lossy().as_ref()), None);
    (graph_path, doc_path)
}

// ── run's staleness check ───────────────────────────────────────────────────

/// True if `graph.json`/`source_index.json` are missing, or any `.rs` file
/// under a dependency-closure member's `src/` is newer than both -- cheap
/// enough to check on every `run` (a metadata call + a directory walk, no
/// compilation) so `run` only pays for a real `build` when one is actually
/// needed, same idea as `cargo run` only recompiling on a real source change.
fn needs_build(manifest: &Path, graph_path: &Path, doc_path: &Path) -> bool {
    let (Ok(graph_meta), Ok(doc_meta)) = (fs::metadata(graph_path), fs::metadata(doc_path)) else {
        return true;
    };
    let (Ok(graph_mtime), Ok(doc_mtime)) = (graph_meta.modified(), doc_meta.modified()) else {
        return true;
    };
    let oldest_output = graph_mtime.min(doc_mtime);

    let packages = local_dependency_closure(manifest);
    for pkg in &packages {
        let Some(manifest_path) = pkg["manifest_path"].as_str() else { continue };
        let src_root = PathBuf::from(manifest_path).parent().unwrap().join("src");
        if newest_rs_mtime(&src_root).is_some_and(|t| t > oldest_output) {
            return true;
        }
    }
    false
}

fn newest_rs_mtime(dir: &Path) -> Option<std::time::SystemTime> {
    let mut newest = None;
    let Ok(entries) = fs::read_dir(dir) else { return None };
    for entry in entries.flatten() {
        let path = entry.path();
        if path.is_dir() {
            if let Some(t) = newest_rs_mtime(&path) {
                newest = newest.max(Some(t));
            }
        } else if path.extension().is_some_and(|e| e == "rs") {
            if let Ok(t) = entry.metadata().and_then(|m| m.modified()) {
                newest = newest.max(Some(t));
            }
        }
    }
    newest
}

// ── validate-trace ───────────────────────────────────────────────────────

fn schema_dir() -> PathBuf {
    // Shared with the Python original (archive/codemap-python/) -- not
    // duplicated. Resolved relative to this crate's own manifest dir
    // (`CARGO_MANIFEST_DIR`, baked in at compile time), not cwd-relative.
    Path::new(env!("CARGO_MANIFEST_DIR")).join("schema")
}

fn viewer_dir() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("viewer")
}

fn cmd_validate_trace(trace: &str) -> bool {
    let entry_schema: Value = serde_json::from_str(&fs::read_to_string(schema_dir().join("trace-entry.schema.json")).unwrap()).unwrap();
    let close_schema: Value = serde_json::from_str(&fs::read_to_string(schema_dir().join("trace-close.schema.json")).unwrap()).unwrap();
    let event_schema: Value = serde_json::from_str(&fs::read_to_string(schema_dir().join("trace-event.schema.json")).unwrap()).unwrap();

    let text = fs::read_to_string(trace).unwrap_or_else(|e| {
        eprintln!("ERROR: could not read {trace}: {e}");
        std::process::exit(1);
    });
    let mut total = 0;
    let mut all_ok = true;
    for (i, line) in text.lines().enumerate() {
        let line = line.trim();
        if line.is_empty() {
            continue;
        }
        total += 1;
        let obj: Value = match serde_json::from_str(line) {
            Ok(v) => v,
            Err(e) => {
                println!("line {}: not valid JSON ({e})", i + 1);
                all_ok = false;
                continue;
            }
        };
        let message = obj.get("fields").and_then(|f| f.get("message")).and_then(Value::as_str);
        let schema = match message {
            Some("new") => &entry_schema,
            Some("close") => &close_schema,
            _ => &event_schema,
        };
        let errors = schema_check::validate(&obj, schema, "$");
        if !errors.is_empty() {
            all_ok = false;
            println!("line {}: {} error(s)", i + 1, errors.len());
            for e in &errors {
                println!("  {e}");
            }
        }
    }
    let verdict = if all_ok { "OK" } else { "FAILED" };
    println!("{verdict}: {total} line(s) checked against schema/trace-{{entry,close,event}}.schema.json");
    all_ok
}

// ── serve ────────────────────────────────────────────────────────────────

struct ServeCtx {
    dir: PathBuf,
    docs_root: Option<PathBuf>,
    graph_path: Option<PathBuf>,
    doc_path: Option<PathBuf>,
}

fn mime_type(path: &Path) -> &'static str {
    match path.extension().and_then(|e| e.to_str()).unwrap_or("") {
        "html" => "text/html; charset=utf-8",
        "js" => "text/javascript; charset=utf-8",
        "css" => "text/css; charset=utf-8",
        "json" => "application/json",
        "svg" => "image/svg+xml",
        "png" => "image/png",
        "jpg" | "jpeg" => "image/jpeg",
        "gif" => "image/gif",
        "ico" => "image/x-icon",
        "woff" => "font/woff",
        "woff2" => "font/woff2",
        "ttf" => "font/ttf",
        "txt" => "text/plain; charset=utf-8",
        _ => "application/octet-stream",
    }
}

/// Resolve a request path against a base directory, refusing to escape it
/// (no `..` traversal) -- the `translate_path` equivalent.
fn resolve_static_path(base: &Path, req_path: &str) -> Option<PathBuf> {
    let mut path = req_path.split('?').next().unwrap_or(req_path);
    if path == "/" || path.is_empty() {
        path = "/index.html";
    }
    let rel = path.trim_start_matches('/');
    let mut resolved = base.to_path_buf();
    for seg in rel.split('/') {
        if seg.is_empty() || seg == "." {
            continue;
        }
        if seg == ".." {
            return None; // no path traversal above the served root
        }
        resolved.push(seg);
    }
    if resolved.is_dir() {
        resolved.push("index.html");
    }
    Some(resolved)
}

fn handle_request(mut request: tiny_http::Request, ctx: &ServeCtx) {
    let url = request.url().to_string();
    let method = request.method().clone();

    if method == tiny_http::Method::Get && url == "/__codemap_version" {
        let mtime = |p: &Path| -> Option<String> {
            let t = fs::metadata(p).ok()?.modified().ok()?;
            let datetime: chrono::DateTime<chrono::Local> = t.into();
            Some(datetime.format("%H:%M:%S").to_string())
        };
        let payload = json!({
            "pid": std::process::id(),
            "indexHtmlMtime": mtime(&ctx.dir.join("index.html")),
            "traceLogMtime": mtime(Path::new(env!("CARGO_MANIFEST_DIR")).join("src").join("trace_log.rs").as_path()),
            "mainPyMtime": mtime(Path::new(env!("CARGO_MANIFEST_DIR")).join("src").join("main.rs").as_path()),
        });
        respond_json(request, 200, &payload);
        return;
    }

    if method == tiny_http::Method::Post && url == "/__codemap_log" {
        let mut body = String::new();
        let _ = request.as_reader().read_to_string(&mut body);
        let payload: Value = serde_json::from_str(&body).unwrap_or_else(|_| json!({"context": "?", "message": body, "level": "error"}));
        let tag = if payload.get("level").and_then(Value::as_str) == Some("error") { "[browser error]" } else { "[browser]" };
        println!(
            "{tag} {}: {}",
            payload.get("context").and_then(Value::as_str).unwrap_or("?"),
            payload.get("message").and_then(Value::as_str).unwrap_or("")
        );
        if let Some(stack) = payload.get("stack").and_then(Value::as_str) {
            println!("{stack}");
        }
        let _ = request.respond(tiny_http::Response::empty(204));
        return;
    }

    if method == tiny_http::Method::Post && url == "/__codemap_parse_trace" {
        let mut body = String::new();
        let _ = request.as_reader().read_to_string(&mut body);
        let source_index: Option<Value> = ctx.doc_path.as_ref().and_then(|p| fs::read_to_string(p).ok()).and_then(|t| serde_json::from_str(&t).ok());
        let graph: Option<Value> = ctx.graph_path.as_ref().and_then(|p| fs::read_to_string(p).ok()).and_then(|t| serde_json::from_str(&t).ok());
        let spans = trace_log::parse_trace(&body, source_index.as_ref(), graph.as_ref());
        println!("[parse_trace] trace_log module: cargo-codemap (Rust)");
        for s in &spans {
            println!(
                "  {}: depth={} stack={} openSeq={} closeSeq={} duration_ms={}",
                s["name"], s["depth"], s["stack"],
                s.get("openSeq").cloned().unwrap_or(Value::Null),
                s.get("closeSeq").cloned().unwrap_or(Value::Null),
                s.get("duration_ms").cloned().unwrap_or(Value::Null),
            );
        }
        respond_json(request, 200, &json!({"spans": spans}));
        return;
    }

    if method != tiny_http::Method::Get {
        let _ = request.respond(tiny_http::Response::empty(404));
        return;
    }

    // graph.json/source_index.json: fixed single files, wherever `graph`/
    // `doc` actually wrote them.
    if url == "/graph.json" {
        if let Some(p) = &ctx.graph_path {
            serve_file(request, p);
            return;
        }
    }
    if url == "/source_index.json" {
        if let Some(p) = &ctx.doc_path {
            serve_file(request, p);
            return;
        }
    }
    if let Some(docs_root) = &ctx.docs_root {
        if url == "/docs" || url.starts_with("/docs/") {
            let sub = url.strip_prefix("/docs").unwrap_or("/");
            if let Some(path) = resolve_static_path(docs_root, sub) {
                serve_file(request, &path);
                return;
            }
        }
    }
    match resolve_static_path(&ctx.dir, &url) {
        Some(path) => serve_file(request, &path),
        None => {
            let _ = request.respond(tiny_http::Response::empty(403));
        }
    }
}

fn serve_file(request: tiny_http::Request, path: &Path) {
    match fs::read(path) {
        Ok(bytes) => {
            let content_type = tiny_http::Header::from_bytes(&b"Content-Type"[..], mime_type(path).as_bytes()).unwrap();
            let response = tiny_http::Response::from_data(bytes).with_header(content_type);
            let _ = request.respond(response);
        }
        Err(_) => {
            let _ = request.respond(tiny_http::Response::empty(404));
        }
    }
}

fn respond_json(request: tiny_http::Request, status: u16, payload: &Value) {
    let body = serde_json::to_vec(payload).unwrap();
    let content_type = tiny_http::Header::from_bytes(&b"Content-Type"[..], &b"application/json"[..]).unwrap();
    let response = tiny_http::Response::from_data(body)
        .with_status_code(status)
        .with_header(content_type);
    let _ = request.respond(response);
}

fn cmd_serve(dir: Option<&str>, docs: Option<&str>, graph: Option<&str>, doc: Option<&str>, port: u16) {
    let dir = dir.map(PathBuf::from).unwrap_or_else(viewer_dir);
    let dir = dunce::canonicalize(&dir).unwrap_or(dir);
    let docs_root = docs.map(PathBuf::from).map(|p| dunce::canonicalize(&p).unwrap_or(p));
    let graph_path = graph.map(PathBuf::from).map(|p| dunce::canonicalize(&p).unwrap_or(p));
    let doc_path = doc.map(PathBuf::from).map(|p| dunce::canonicalize(&p).unwrap_or(p));

    println!("Serving {} at http://localhost:{port}/", dir.display());
    if let Some(d) = &docs_root {
        println!("Serving {} at http://localhost:{port}/docs/", d.display());
    }
    if let Some(g) = &graph_path {
        println!("Serving {} at http://localhost:{port}/graph.json (auto-loaded)", g.display());
    }
    if let Some(d) = &doc_path {
        println!("Serving {} at http://localhost:{port}/source_index.json (auto-loaded)", d.display());
    }
    println!("trace_log module: cargo-codemap (Rust port, src/trace_log.rs)");
    println!("Press Ctrl+C to stop.");

    let ctx = Arc::new(ServeCtx { dir, docs_root, graph_path, doc_path });
    let server = Arc::new(tiny_http::Server::http(("0.0.0.0", port)).expect("failed to bind HTTP server"));

    let mut handles = Vec::new();
    for _ in 0..8 {
        let server = server.clone();
        let ctx = ctx.clone();
        handles.push(std::thread::spawn(move || {
            while let Ok(request) = server.recv() {
                handle_request(request, &ctx);
            }
        }));
    }
    for h in handles {
        let _ = h.join();
    }
}

// ── run (graph + doc + serve) ──────────────────────────────────────────────

fn open_browser(url: &str) {
    #[cfg(target_os = "windows")]
    let _ = Command::new("cmd").args(["/C", "start", "", url]).status();
    #[cfg(target_os = "macos")]
    let _ = Command::new("open").arg(url).status();
    #[cfg(all(unix, not(target_os = "macos")))]
    let _ = Command::new("xdg-open").arg(url).status();
}

fn cmd_run(project: &str, port: u16, no_browser: bool) {
    let manifest = require_manifest(project);
    let default_dir = default_out_dir(&manifest);
    let graph_path = default_dir.join("graph.json");
    let doc_path = default_dir.join("source_index.json");

    if needs_build(&manifest, &graph_path, &doc_path) {
        cmd_build(project);
    } else {
        println!("Existing graph/doc index look up to date -- skipping rebuild (use `cargo codemap build` to force).");
        println!("  {}", graph_path.display());
        println!("  {}", doc_path.display());
    }
    let docs_root = PathBuf::from(cargo_metadata(&manifest)["target_directory"].as_str().unwrap()).join("doc");

    let url = format!("http://localhost:{port}/");
    println!("\nStarting the viewer at {url} -- graph and doc index load automatically.");
    println!("Use \"Load trace...\" in the toolbar once you have a run to replay.");

    if !no_browser {
        open_browser(&url);
    }

    cmd_serve(
        Some(viewer_dir().to_string_lossy().as_ref()),
        docs_root.exists().then(|| docs_root.to_string_lossy().to_string()).as_deref(),
        Some(graph_path.to_string_lossy().as_ref()),
        Some(doc_path.to_string_lossy().as_ref()),
        port,
    );
}

fn main() {
    // `cargo codemap build ...` invokes this binary as `cargo-codemap codemap
    // build ...` -- cargo forwards its own subcommand name as a literal
    // first argument, it doesn't strip it (a documented cargo-subcommand
    // quirk). Drop it so both that invocation and a direct `cargo-codemap
    // build ...` / `cargo run -- build ...` (no leading "codemap") parse
    // identically.
    let mut args: Vec<String> = std::env::args().collect();
    if args.get(1).map(String::as_str) == Some("codemap") {
        args.remove(1);
    }
    let cli = Cli::parse_from(args);
    let ok = match cli.command {
        Cmd::Run { project, port, no_browser } => {
            cmd_run(&project, port, no_browser);
            true
        }
        Cmd::Build { project } => {
            cmd_build(&project);
            true
        }
        Cmd::ValidateTrace { trace } => cmd_validate_trace(&trace),
    };
    if !ok {
        std::process::exit(1);
    }
}

// ── MIR-format canary ───────────────────────────────────────────────────────
//
// Was the `selfcheck` CLI subcommand; converted to a plain `cargo test` since
// it's a maintainer tool (run after a toolchain upgrade -- see
// doc/PROJECT.md §4, "MIR as the only extraction source"), not something an
// end user running `cargo codemap` on their own project ever needs. Builds
// the known `dummy-cli` fixture's graph and asserts a fixed set of
// structural facts about it that are already known true today; a future
// rustc that reformats MIR's pretty-printer output would otherwise silently
// produce a near-empty graph for a real user's real crate, no error at all --
// this exists to fail loudly and specifically instead.
#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn selfcheck_dummy_cli() {
        // CARGO_MANIFEST_DIR-relative, not cwd-relative -- correct
        // regardless of where `cargo test` happens to be invoked from.
        // examples/dummy-cli, not the old (now-deleted) sibling `dummy-cli`/
        // `dummy-lib` repos: it's the in-repo fixture (see PROJECT.md §2.16)
        // and covers most of the same MIR shapes. Two of the original 10
        // checks (an unqualified/no-hint cross-crate call; a generic
        // function's monomorphized turbofish) have no equivalent in this
        // simpler fixture -- dropped rather than faked.
        let project = Path::new(env!("CARGO_MANIFEST_DIR")).join("examples").join("dummy-cli");
        let project = project.to_string_lossy().to_string();

        let graph_path = cmd_graph(&project, None);
        let graph: Value = serde_json::from_str(&fs::read_to_string(&graph_path).unwrap()).unwrap();
        let node_ids: std::collections::HashSet<&str> =
            graph["nodes"].as_array().unwrap().iter().map(|n| n["data"]["id"].as_str().unwrap()).collect();
        let edges: std::collections::HashSet<(&str, &str)> = graph["edges"]
            .as_array()
            .unwrap()
            .iter()
            .map(|e| (e["data"]["source"].as_str().unwrap(), e["data"]["target"].as_str().unwrap()))
            .collect();
        let traced_count = graph["nodes"].as_array().unwrap().iter().filter(|n| n["data"]["traced"] == true).count();
        let mut call_orders_by_source: HashMap<&str, std::collections::HashSet<u64>> = HashMap::new();
        for e in graph["edges"].as_array().unwrap() {
            call_orders_by_source
                .entry(e["data"]["source"].as_str().unwrap())
                .or_default()
                .insert(e["data"]["callOrder"].as_u64().unwrap());
        }

        let checks: Vec<(&str, bool)> = vec![
            ("bare free fn (RE_FREE_FN, no module prefix)", node_ids.contains("dummy_api::describe")),
            ("impl method (RE_IMPL_SELF)", node_ids.contains("dummy_api::Square::area")),
            ("cross-crate call, MIR gave an explicit crate hint", edges.contains(&("dummy_api::run", "dummy_core::run"))),
            (
                "dyn-dispatch fan-out (one &dyn Trait call site resolves to every local impl)",
                edges.contains(&("dummy_api::dispatch_dynamic", "dummy_api::Square::area"))
                    && edges.contains(&("dummy_api::dispatch_dynamic", "dummy_api::Circle::area"))
                    && edges.contains(&("dummy_api::dispatch_dynamic", "dummy_api::Triangle::area")),
            ),
            ("self-recursion produces a real self-edge", edges.contains(&("dummy_api::recursive_factorial", "dummy_api::recursive_factorial"))),
            ("#[instrument]'s Callsite scaffolding sets traced=true", traced_count > 0),
            (
                "call_order restarts at 1 per caller (not inflated by tracing's own noise calls)",
                call_orders_by_source.values().any(|orders| orders.contains(&1)),
            ),
            (
                "cross-crate node-id collision: two distinct `describe`/`run` nodes",
                node_ids.contains("dummy_core::describe")
                    && node_ids.contains("dummy_api::describe")
                    && node_ids.contains("dummy_core::run")
                    && node_ids.contains("dummy_api::run"),
            ),
        ];

        let failed: Vec<&str> = checks.iter().filter(|(_, ok)| !ok).map(|(label, _)| *label).collect();
        assert!(
            failed.is_empty(),
            "MIR-format canary failed ({} node(s), {} edge(s)): {failed:?}\n\
             If nothing in src/ or examples/dummy-cli changed recently, this likely \
             means the installed rustc's MIR pretty-printer output changed shape -- see \
             doc/PROJECT.md §4, \"MIR as the only extraction source\".",
            node_ids.len(),
            edges.len(),
        );
    }
}
