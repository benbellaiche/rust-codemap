"""codemap.__main__ -- CLI entry point. Run as `python -m codemap <subcommand>`.

Subcommands:
  run     Generate the call-graph + doc index for a target crate and serve
          the viewer, in one step -- the graph and doc index load
          automatically on page load, no further action needed.
  graph   Build the call-graph for a target crate AND every crate it
          actually (transitively) depends on locally -- not merely every
          crate that happens to share its workspace, which could include
          unrelated siblings or client binaries that depend on the target
          rather than the other way around. Runs `cargo build -p <each>`
          with `RUSTFLAGS=--emit=mir`, then extracts and merges the graph
          from each one's MIR dump. Useful on its own (paired with `serve
          --graph ...`, already running) to regenerate just the graph after
          a code change without rebuilding the doc index or restarting the
          server -- `serve` re-reads its `--graph`/`--doc` files on every
          request, so reloading the page alone picks up new output.
  doc     Cross-reference `cargo doc` output with a graph.json, producing
          source_index.json (signatures, doc comments, source links). Same
          iterate-without-restarting use case as `graph`.
  serve   Serve the viewer directory over plain HTTP, optionally also
          serving a graph.json/source_index.json/doc root at fixed URLs
          the viewer auto-loads (`--graph`/`--doc`/`--docs`) -- `run` always
          passes its own just-generated ones. Without them there's nothing
          to auto-load and no other way to load anything into the viewer:
          the old "Load graph.../Load doc index..." file-picker fallback
          was removed once auto-load made it redundant for the case it
          existed for.

`graph`/`doc` write into `<target crate>/target/rust-codemap/<crate_name>/`
by default -- next to cargo's own build output, never into this repo, and
nested under the crate's own name so multiple crates in one workspace
don't overwrite each other's output.

Run `python -m codemap <subcommand> --help` for each command's options.
See README.md for the full workflow and the tracing log format contract.
"""
from __future__ import annotations

import argparse
import functools
import http.server
import json
import os
import re
import subprocess
import sys
import webbrowser
from pathlib import Path
from types import SimpleNamespace

from . import mir_graph, doc_index, trace_log


def cargo_metadata(manifest: Path) -> dict:
    out = subprocess.run(
        ["cargo", "metadata", "--no-deps", "--format-version", "1", "--manifest-path", str(manifest)],
        capture_output=True, text=True, check=True,
    )
    return json.loads(out.stdout)


def read_toml_section(manifest: Path, section: str) -> str | None:
    """Crude single-section extractor: the text between `[section]` and the
    next `[...]` header (or end of file). Good enough for the handful of
    single-line string fields this tool reads (package/lib name) without
    pulling in a TOML parser dependency -- but it does mean it's scoped to
    one specific table, not just "the first match anywhere in the file"."""
    text = manifest.read_text(encoding="utf-8")
    m = re.search(rf"^\[{re.escape(section)}\]\s*$(.*?)(?=^\[|\Z)", text, re.MULTILINE | re.DOTALL)
    return m.group(1) if m else None


def read_package_name(manifest: Path) -> str:
    """The [package] name, read from within the [package] table
    specifically -- not just "the first name = ... in the file", which
    would be wrong if a [lib] name override happens to appear earlier."""
    section = read_toml_section(manifest, "package")
    m = re.search(r'^\s*name\s*=\s*"([^"]+)"', section, re.MULTILINE) if section else None
    if not m:
        sys.exit(f'ERROR: could not find [package] name = "..." in {manifest}')
    return m.group(1)


def require_manifest(project: str) -> Path:
    manifest = Path(project).resolve() / "Cargo.toml"
    if not manifest.exists():
        sys.exit(f"ERROR: no Cargo.toml at {manifest}")
    return manifest


def crate_name(manifest: Path) -> str:
    """The actual compiled crate name: [lib] name if the manifest overrides
    it (some real projects do this on every crate -- confirmed to silently
    break MIR/doc lookup otherwise, since cargo then names the artifact
    after the override, not the package name), else [package] name with
    hyphens turned into underscores (cargo's own default when there's no
    override)."""
    section = read_toml_section(manifest, "lib")
    if section:
        m = re.search(r'^\s*name\s*=\s*"([^"]+)"', section, re.MULTILINE)
        if m:
            return m.group(1)
    return read_package_name(manifest).replace("-", "_")


def find_vendor_dirs(manifest: Path) -> list:
    """Bare directory-name marker(s) (e.g. "vendor") for wherever any
    `.cargo/config.toml` in scope (walking upward from the manifest the way
    cargo itself discovers config, plus $CARGO_HOME) redirects a
    `[source.*]` to via `directory = "..."` -- i.e. wherever `cargo vendor`
    put its copy of external dependency source, whatever that source-
    replacement table happens to be named (`cargo vendor` itself defaults
    to "vendored-sources", but nothing enforces that name).

    Passed to mir_graph as extra "this path is external, not the target
    crate" markers, the same way EXTERNAL_PATH_MARKERS's existing entries
    (".cargo", "registry", ...) are: a bare recognizable fragment, not a
    resolved path -- a vendor directory normally lives *inside* the
    workspace, so rustc prints it as a workspace-relative path (matching
    every genuinely local crate's own path shape), not an absolute one
    `.cargo`/`registry`/`.rustup` paths get simply for living outside the
    workspace. Only the last path segment of the configured `directory` is
    used (e.g. "vendor" out of "third_party/vendor"), so nesting choices
    don't matter."""
    candidates = []
    cur = manifest.parent.resolve()
    while True:
        candidates.append(cur / ".cargo" / "config.toml")
        candidates.append(cur / ".cargo" / "config")
        if cur.parent == cur:
            break
        cur = cur.parent
    cargo_home = Path(os.environ.get("CARGO_HOME", Path.home() / ".cargo"))
    candidates += [cargo_home / "config.toml", cargo_home / "config"]

    markers = []
    for config_path in candidates:
        if not config_path.exists():
            continue
        text = config_path.read_text(encoding="utf-8", errors="ignore")
        for m in re.finditer(r'^\[source\.[^\]]+\]\s*$(.*?)(?=^\[|\Z)', text, re.MULTILINE | re.DOTALL):
            dm = re.search(r'^\s*directory\s*=\s*"([^"]+)"', m.group(1), re.MULTILINE)
            if dm:
                name = Path(dm.group(1).replace("\\", "/")).name
                if name and name not in markers:
                    markers.append(name)
    return markers


def local_dependency_closure(manifest: Path) -> list:
    """Every package in the target crate's own transitive dependency graph
    that's local (a path dependency, not crates.io/a git registry) --
    including the target crate itself.

    This is deliberately *not* "every member of the workspace this crate
    happens to live in" (that was the previous approach, `cargo metadata
    --no-deps`): a workspace can contain sibling crates unrelated to this
    one, or -- the case that actually broke -- client binaries that DEPEND
    ON this crate rather than being depended on BY it. Those must not be
    swept in just because `cargo build --workspace` would build them too.

    Uses `cargo metadata`'s dependency-resolution graph (no --no-deps this
    time) and walks only the forward `deps` edges from the target's own
    node, so it naturally handles both directions correctly."""
    out = subprocess.run(
        ["cargo", "metadata", "--format-version", "1", "--manifest-path", str(manifest)],
        capture_output=True, text=True, check=True,
    )
    meta = json.loads(out.stdout)
    resolve = meta["resolve"]
    root = resolve["root"]
    if root is None:
        sys.exit(f"ERROR: {manifest} has no resolvable root package "
                  f"(pointing --project at a virtual workspace manifest?)")

    nodes_by_id = {n["id"]: n for n in resolve["nodes"]}
    closure = set()
    stack = [root]
    while stack:
        pid = stack.pop()
        if pid in closure:
            continue
        closure.add(pid)
        stack.extend(dep["pkg"] for dep in nodes_by_id[pid]["deps"])

    packages_by_id = {p["id"]: p for p in meta["packages"]}
    return [packages_by_id[pid] for pid in closure if pid.startswith("path+file://")]


def default_out_dir(manifest: Path) -> Path:
    """Where generated artifacts land by default:
    <target_directory>/rust-codemap/<crate_name>/ -- resolved via `cargo
    metadata` so it's correct for workspace members too (same directory
    cargo itself already uses for build output and docs). Nested under the
    crate's own name, not just "rust-codemap/": a workspace's members all
    share one target_directory, so without this every crate's output would
    land at the exact same path and each one would overwrite the last."""
    target_dir = Path(cargo_metadata(manifest)["target_directory"])
    return target_dir / "rust-codemap" / crate_name(manifest)


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


# ── graph ────────────────────────────────────────────────────────────────

def find_mir_file(deps_dir: Path, name: str) -> Path | None:
    """A lib's .mir carries a hash suffix (name-<hash>.mir); a bin's doesn't
    (name.mir). Match on the crate's own name first -- picking "whatever
    .mir is newest" breaks the moment cargo decides nothing needs
    rebuilding (a no-op "Finished" that never touches this crate's .mir)
    while some OTHER crate in the same shared target/deps dir got rebuilt
    more recently, which is the common case in a workspace. mtime only
    breaks ties among this crate's own (e.g. stale profile) artifacts."""
    candidates = sorted(
        [*deps_dir.glob(f"{name}.mir"), *deps_dir.glob(f"{name}-*.mir")],
        key=lambda p: p.stat().st_mtime, reverse=True,
    )
    return candidates[0] if candidates else None


def cmd_graph(args) -> Path:
    manifest = require_manifest(args.project)
    target_dir = Path(cargo_metadata(manifest)["target_directory"])
    out_path = Path(args.out) if args.out else default_out_dir(manifest) / "graph.json"

    packages = local_dependency_closure(manifest)
    members = [(pkg["name"], crate_name(Path(pkg["manifest_path"]))) for pkg in packages]
    print(f"Local dependency closure: {', '.join(n for n, _ in members)}")

    # cargo rustc --emit=mir only applies to the one crate being directly
    # built -- RUSTFLAGS applies to every rustc invocation cargo makes, so
    # it reaches every -p we ask for (and their external deps too, which is
    # fine: we only ever read the files matching one of OUR package names
    # below). Building just these -p's (not --workspace) also means we
    # never compile -- and never even glance at -- unrelated sibling crates
    # or client binaries that merely happen to share the workspace.
    print("Building with RUSTFLAGS=--emit=mir ...")
    subprocess.run(
        ["cargo", "build", "--manifest-path", str(manifest),
         *[a for pkg_name, _ in members for a in ("-p", pkg_name)]],
        check=True, env={**os.environ, "RUSTFLAGS": "--emit=mir"},
    )

    deps_dir = target_dir / "debug" / "deps"
    mir_texts = []
    for pkg_name, cname in members:
        mir_path = find_mir_file(deps_dir, cname)
        if mir_path is None:
            print(f"  WARNING: no .mir found for {pkg_name} ({cname}) -- skipping")
            continue
        print(f"  {pkg_name}: {mir_path.name} ({mir_path.stat().st_size // 1024} KB)")
        mir_texts.append(mir_path.read_text(encoding="utf-8", errors="ignore"))
    if not mir_texts:
        sys.exit(f"ERROR: no .mir file found for any dependency-closure member under {deps_dir}")

    # A single combined text: parse_mir just scans lines, so concatenating
    # every member's MIR and parsing once naturally merges nodes/edges
    # across crates (including cross-crate calls, once resolvable) with no
    # separate "merge N graphs" step needed.
    vendor_dirs = find_vendor_dirs(manifest)
    graph = mir_graph.build_graph("\n".join(mir_texts), vendor_dirs)
    write_json(out_path, graph)
    traced = sum(1 for n in graph["nodes"] if n["data"]["traced"])
    print(f"OK {out_path}  ({len(graph['nodes'])} nodes, {len(graph['edges'])} edges, "
          f"{traced} traced, {len(mir_texts)} crate(s) merged)")
    return out_path


# ── doc ──────────────────────────────────────────────────────────────────

def cmd_doc(args) -> Path:
    manifest = require_manifest(args.project)
    target_dir = Path(cargo_metadata(manifest)["target_directory"])
    default_dir = default_out_dir(manifest)
    graph_path = Path(args.graph) if args.graph else default_dir / "graph.json"
    out_path = Path(args.out) if args.out else default_dir / "source_index.json"

    # graph.json can contain nodes from any crate in the dependency closure
    # (see `graph`) -- doc exactly that same set, not the whole workspace
    # (which could include unrelated siblings or client binaries), or
    # cross-references for those crates' items would silently come up empty.
    packages = local_dependency_closure(manifest)
    print("Running cargo doc --no-deps ...")
    subprocess.run(
        ["cargo", "doc", "--no-deps", "--manifest-path", str(manifest),
         *[a for pkg in packages for a in ("-p", pkg["name"])]],
        check=True,
    )

    crates = []
    for pkg in packages:
        cname = crate_name(Path(pkg["manifest_path"]))
        doc_root = target_dir / "doc" / cname
        if doc_root.exists():
            crates.append((doc_root, Path(pkg["manifest_path"]).parent / "src", cname))
        else:
            print(f"  WARNING: no doc output for {pkg['name']} ({cname}) -- skipping")
    if not crates:
        sys.exit(f"ERROR: no cargo doc output found under {target_dir / 'doc'}")

    graph = json.loads(graph_path.read_text(encoding="utf-8")) if graph_path.exists() else {"nodes": []}

    index = doc_index.build_index(crates, graph)
    write_json(out_path, index)
    print(f"OK {out_path}  ({len(index)} entries, {len(crates)} crate(s) cross-referenced)")
    return out_path


# ── serve ────────────────────────────────────────────────────────────────

class LoggingHandler(http.server.SimpleHTTPRequestHandler):
    """Static file serving (inherited, unchanged) plus one endpoint the
    viewer posts to when a client-side error happens during load/render
    (see index.html) -- printed straight to this process's terminal, since
    that's what's actually being watched while iterating, not the browser
    console.

    Also optionally serves a second, unrelated directory (the target
    crate's own `target/doc/`) under the `/docs/` prefix, so the viewer can
    embed the native `cargo doc` HTML pages in an iframe -- same-origin,
    which a `file://` src would not be. `doc_index.py` already emits each
    entry's `docPage` relative to that shared target/doc/ root (not any
    single crate's own subdirectory within it), so that root is exactly
    what --docs should point at: page-relative asset links (css/fonts under
    a sibling static.files/ dir) resolve correctly through this same prefix
    without any rewriting."""

    def __init__(self, *args, docs_root=None, graph_path=None, doc_path=None, **kwargs):
        self.docs_root = docs_root
        self.graph_path = graph_path
        self.doc_path = doc_path
        super().__init__(*args, **kwargs)

    def translate_path(self, path):
        # graph.json/source_index.json are single files living wherever
        # `codemap graph`/`doc` actually wrote them (next to the target
        # crate, never next to this viewer) -- `run` knows those exact
        # paths already, since it just generated them, so it can serve
        # them at a fixed URL the viewer auto-fetches on load instead of
        # requiring "Load graph…"/"Load doc index…" every single time.
        # Returning the absolute path directly here needs no directory-
        # swap trick (unlike --docs below): the base class's send_head()
        # doesn't care whether translate_path's result sits under
        # self.directory or not.
        if path == "/graph.json" and self.graph_path:
            return self.graph_path
        if path == "/source_index.json" and self.doc_path:
            return self.doc_path
        if self.docs_root and (path == "/docs" or path.startswith("/docs/")):
            original_directory = self.directory
            self.directory = self.docs_root
            try:
                return super().translate_path(path[len("/docs"):] or "/")
            finally:
                self.directory = original_directory
        return super().translate_path(path)

    def do_POST(self):
        if self.path == "/__codemap_parse_trace":
            self._handle_parse_trace()
            return
        if self.path != "/__codemap_log":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else b""
        try:
            payload = json.loads(body.decode("utf-8", errors="replace"))
        except ValueError:
            payload = {"context": "?", "message": body.decode("utf-8", errors="replace"), "level": "error"}
        tag = "[browser error]" if payload.get("level") == "error" else "[browser]"
        print(f"{tag} {payload.get('context', '?')}: {payload.get('message', '')}", flush=True)
        if payload.get("stack"):
            print(payload["stack"], flush=True)
        self.send_response(204)
        self.end_headers()

    def _handle_parse_trace(self):
        # Routes a raw trace log through the one real parser (trace_log.py)
        # instead of the viewer's own JS reimplementation of the same
        # dedup/iteration/duration logic -- the two had already drifted
        # (the JS version silently dropped span `fields`, see PROJECT.md
        # §3). The viewer still carries a client-side fallback for when it's
        # opened without this server running (e.g. straight off disk) --
        # this endpoint is what makes that fallback path the exception
        # rather than the rule.
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else b""
        try:
            spans = trace_log.parse_trace(body.decode("utf-8", errors="replace"))
            response = json.dumps({"spans": spans}).encode("utf-8")
        except Exception as exc:
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": str(exc)}).encode("utf-8"))
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)


def cmd_serve(args):
    # Browser-supplied log/error text (see LoggingHandler.do_POST) can
    # contain arbitrary Unicode. The default console encoding on Windows
    # (cp1252, not UTF-8) throws UnicodeEncodeError on print() for
    # anything outside it -- which kills that request's handler thread
    # silently (the server keeps running, but that one message never
    # appears). Reconfigure so it's replaced with '?' instead of crashing.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    directory = str(Path(args.dir).resolve())
    docs_root = str(Path(args.docs).resolve()) if getattr(args, "docs", None) else None
    graph_path = str(Path(args.graph).resolve()) if getattr(args, "graph", None) else None
    doc_path = str(Path(args.doc).resolve()) if getattr(args, "doc", None) else None
    handler = functools.partial(LoggingHandler, directory=directory, docs_root=docs_root,
                                 graph_path=graph_path, doc_path=doc_path)
    with http.server.ThreadingHTTPServer(("", args.port), handler) as httpd:
        print(f"Serving {directory} at http://localhost:{args.port}/", flush=True)
        if docs_root:
            print(f"Serving {docs_root} at http://localhost:{args.port}/docs/", flush=True)
        if graph_path:
            print(f"Serving {graph_path} at http://localhost:{args.port}/graph.json (auto-loaded)", flush=True)
        if doc_path:
            print(f"Serving {doc_path} at http://localhost:{args.port}/source_index.json (auto-loaded)", flush=True)
        print("Press Ctrl+C to stop.", flush=True)
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nStopped.")


# ── run (graph + doc + serve) ──────────────────────────────────────────────

def cmd_run(args):
    manifest = require_manifest(args.project)
    graph_path = cmd_graph(SimpleNamespace(project=args.project, out=None))
    doc_path = cmd_doc(SimpleNamespace(project=args.project, graph=None, out=None))
    docs_root = Path(cargo_metadata(manifest)["target_directory"]) / "doc"

    print()
    print("Generated:")
    print(f"  {graph_path}")
    print(f"  {doc_path}")
    url = f"http://localhost:{args.port}/"
    print(f"\nStarting the viewer at {url} -- graph and doc index load automatically.")
    print('Use "Load trace..." in the toolbar once you have a run to replay.')

    if not args.no_browser:
        webbrowser.open(url)

    cmd_serve(SimpleNamespace(
        dir="viewer", port=args.port,
        docs=str(docs_root) if docs_root.exists() else None,
        graph=str(graph_path), doc=str(doc_path),
    ))


# ── CLI wiring ───────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(
        prog="python -m codemap", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = ap.add_subparsers(dest="command", required=True)

    r = sub.add_parser("run", help="Generate the graph + doc index for a project and serve the viewer")
    r.add_argument("--project", required=True, help="Path to the target crate (dir containing Cargo.toml)")
    r.add_argument("--port", type=int, default=8787)
    r.add_argument("--no-browser", action="store_true", help="Don't automatically open a browser tab")
    r.set_defaults(func=cmd_run)

    g = sub.add_parser("graph", help="Generate the call-graph for a target crate and its whole workspace")
    g.add_argument("--project", required=True, help="Path to the target crate (dir containing Cargo.toml)")
    g.add_argument("--out", default=None, help="Where to write graph.json (default: <target>/rust-codemap/<crate>/graph.json)")
    g.set_defaults(func=cmd_graph)

    d = sub.add_parser("doc", help="Cross-reference cargo doc output with a graph.json")
    d.add_argument("--project", required=True, help="Path to the target crate (dir containing Cargo.toml)")
    d.add_argument("--graph", default=None, help="graph.json to cross-reference against (default: <target>/rust-codemap/<crate>/graph.json)")
    d.add_argument("--out", default=None, help="Where to write source_index.json (default: <target>/rust-codemap/<crate>/source_index.json)")
    d.set_defaults(func=cmd_doc)

    s = sub.add_parser("serve", help="Serve the viewer directory over HTTP")
    s.add_argument("--dir", default="viewer", help="Directory to serve (should contain index.html)")
    s.add_argument("--docs", default=None, help="Also serve this directory (a target/doc/ root) under /docs/, "
                                                  "so the viewer can embed native cargo-doc pages")
    s.add_argument("--graph", default=None, help="graph.json to serve at /graph.json -- the viewer auto-loads "
                                                   "it on page load instead of needing \"Load graph...\"")
    s.add_argument("--doc", default=None, help="source_index.json to serve at /source_index.json -- "
                                                 "auto-loaded the same way as --graph")
    s.add_argument("--port", type=int, default=8787)
    s.set_defaults(func=cmd_serve)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
