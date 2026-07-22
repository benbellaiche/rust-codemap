"""codemap.__main__ -- CLI entry point. Run as `python -m codemap <subcommand>`.

Subcommands:
  run     Generate the call-graph + doc index for a target crate and serve
          the viewer, in one step.
  graph   Build the call-graph for a target crate's binary or library
          target (runs `cargo rustc --emit=mir`, then extracts the graph
          from the resulting MIR dump).
  doc     Cross-reference `cargo doc` output with a graph.json, producing
          source_index.json (signatures, doc comments, source links).
  trace   Convert a raw tracing_subscriber JSON-lines log into trace.json.
  serve   Serve the viewer directory over plain HTTP.

`graph`/`doc`/`trace` write into
`<target crate>/target/rust-codemap/<crate_name>/` by default -- next to
cargo's own build output, never into this repo, and nested under the
crate's own name so multiple crates in one workspace don't overwrite each
other's output. The viewer never needs project-specific files sitting next
to it: use the "Load graph..." / "Load doc index..." / "Load trace..."
buttons in its toolbar to pick up whatever was generated.

Run `python -m codemap <subcommand> --help` for each command's options.
See README.md for the full workflow and the tracing log format contract.
"""
import argparse
import functools
import http.server
import json
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


def read_package_name(manifest: Path) -> str:
    """First `name = "..."` in Cargo.toml -- the [package] name, in practice
    always the first such line in an idiomatic manifest."""
    m = re.search(r'^\s*name\s*=\s*"([^"]+)"', manifest.read_text(encoding="utf-8"), re.MULTILINE)
    if not m:
        sys.exit(f'ERROR: could not find name = "..." in {manifest}')
    return m.group(1)


def require_manifest(project: str) -> Path:
    manifest = Path(project).resolve() / "Cargo.toml"
    if not manifest.exists():
        sys.exit(f"ERROR: no Cargo.toml at {manifest}")
    return manifest


def crate_name(manifest: Path) -> str:
    return read_package_name(manifest).replace("-", "_")


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


def target_flag(args) -> tuple:
    """Which cargo target to compile: (cargo-rustc args, human label).
    `--bin <name>` and `--lib` are mutually exclusive at the argparse level."""
    if args.lib:
        return ["--lib"], "--lib"
    return ["--bin", args.bin], f"--bin {args.bin}"


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


# ── graph ────────────────────────────────────────────────────────────────

def cmd_graph(args) -> Path:
    manifest = require_manifest(args.project)
    target_dir = Path(cargo_metadata(manifest)["target_directory"])
    out_path = Path(args.out) if args.out else default_out_dir(manifest) / "graph.json"
    cargo_target_args, label = target_flag(args)

    print(f"Compiling {label} with --emit=mir ...")
    subprocess.run(
        ["cargo", "rustc", "--manifest-path", str(manifest), *cargo_target_args, "--", "--emit=mir"],
        check=True,
    )

    # Picking "whatever .mir is newest" breaks the moment cargo decides
    # nothing needs rebuilding (a no-op "Finished" that never touches this
    # crate's .mir) while some OTHER crate in the same shared target/deps
    # dir got rebuilt more recently -- easy to hit in a workspace. Narrow
    # the glob to this crate's own name first; only fall back to mtime to
    # break ties among that crate's own (e.g. stale profile) artifacts.
    cname = crate_name(manifest)
    deps_dir = target_dir / "debug" / "deps"
    mir_files = sorted(
        [*deps_dir.glob(f"{cname}.mir"), *deps_dir.glob(f"{cname}-*.mir")],
        key=lambda p: p.stat().st_mtime, reverse=True,
    )
    if not mir_files:
        sys.exit(f"ERROR: no {cname}(-*).mir file found under {deps_dir}")
    mir_path = mir_files[0]
    print(f"Parsing {mir_path.name} ({mir_path.stat().st_size // 1024} KB)...")

    graph = mir_graph.build_graph(mir_path.read_text(encoding="utf-8", errors="ignore"))
    write_json(out_path, graph)
    traced = sum(1 for n in graph["nodes"] if n["data"]["traced"])
    print(f"OK {out_path}  ({len(graph['nodes'])} nodes, {len(graph['edges'])} edges, {traced} traced)")
    return out_path


# ── doc ──────────────────────────────────────────────────────────────────

def cmd_doc(args) -> Path:
    manifest = require_manifest(args.project)
    target_dir = Path(cargo_metadata(manifest)["target_directory"])
    default_dir = default_out_dir(manifest)
    graph_path = Path(args.graph) if args.graph else default_dir / "graph.json"
    out_path = Path(args.out) if args.out else default_dir / "source_index.json"

    print("Running cargo doc --no-deps ...")
    subprocess.run(["cargo", "doc", "--no-deps", "--manifest-path", str(manifest)], check=True)

    doc_root = target_dir / "doc" / crate_name(manifest)
    if not doc_root.exists():
        sys.exit(f"ERROR: {doc_root} not found (unexpected crate/package name mismatch?)")

    graph = json.loads(graph_path.read_text(encoding="utf-8")) if graph_path.exists() else {"nodes": []}

    index = doc_index.build_index(doc_root, manifest.parent / "src", graph)
    write_json(out_path, index)
    print(f"OK {out_path}  ({len(index)} entries)")
    return out_path


# ── trace ────────────────────────────────────────────────────────────────

def cmd_trace(args) -> Path:
    in_path = Path(args.input)
    if not in_path.exists():
        sys.exit(f"ERROR: {in_path} not found")

    if args.out:
        out_path = Path(args.out)
    elif args.project:
        out_path = default_out_dir(require_manifest(args.project)) / "trace.json"
    else:
        out_path = Path("trace.json")

    spans = trace_log.parse_trace(in_path.read_text(encoding="utf-8", errors="ignore"))
    write_json(out_path, {"spans": spans})
    print(f"OK {out_path}  ({len(spans)} spans)")
    for s in spans:
        it = f" x{s['iterations']}" if s["iterations"] > 1 else ""
        print(f"  {'  ' * s['depth']}{s['name']}  {s['duration_ms']}ms{it}")
    return out_path


# ── serve ────────────────────────────────────────────────────────────────

def cmd_serve(args):
    directory = str(Path(args.dir).resolve())
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=directory)
    with http.server.ThreadingHTTPServer(("", args.port), handler) as httpd:
        print(f"Serving {directory} at http://localhost:{args.port}/")
        print("Press Ctrl+C to stop.")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nStopped.")


# ── run (graph + doc + serve) ──────────────────────────────────────────────

def cmd_run(args):
    graph_path = cmd_graph(SimpleNamespace(project=args.project, bin=args.bin, lib=args.lib, out=None))
    doc_path = cmd_doc(SimpleNamespace(project=args.project, graph=None, out=None))

    print()
    print("Generated:")
    print(f"  {graph_path}")
    print(f"  {doc_path}")
    url = f"http://localhost:{args.port}/"
    print(f"\nStarting the viewer at {url}")
    print('Use "Load graph..." and "Load doc index..." in the toolbar to pick the files above.')

    if not args.no_browser:
        webbrowser.open(url)

    cmd_serve(SimpleNamespace(dir="viewer", port=args.port))


# ── CLI wiring ───────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(
        prog="python -m codemap", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = ap.add_subparsers(dest="command", required=True)

    r = sub.add_parser("run", help="Generate the graph + doc index for a project and serve the viewer")
    r.add_argument("--project", required=True, help="Path to the target crate (dir containing Cargo.toml)")
    rt = r.add_mutually_exclusive_group(required=True)
    rt.add_argument("--bin", help="Binary target to compile (cargo rustc --bin <name>)")
    rt.add_argument("--lib", action="store_true", help="Analyze the crate's library target instead of a binary")
    r.add_argument("--port", type=int, default=8787)
    r.add_argument("--no-browser", action="store_true", help="Don't automatically open a browser tab")
    r.set_defaults(func=cmd_run)

    g = sub.add_parser("graph", help="Generate the call-graph from a target crate's MIR")
    g.add_argument("--project", required=True, help="Path to the target crate (dir containing Cargo.toml)")
    gt = g.add_mutually_exclusive_group(required=True)
    gt.add_argument("--bin", help="Binary target to compile (cargo rustc --bin <name>)")
    gt.add_argument("--lib", action="store_true", help="Analyze the crate's library target instead of a binary")
    g.add_argument("--out", default=None, help="Where to write graph.json (default: <target>/rust-codemap/<crate>/graph.json)")
    g.set_defaults(func=cmd_graph)

    d = sub.add_parser("doc", help="Cross-reference cargo doc output with a graph.json")
    d.add_argument("--project", required=True, help="Path to the target crate (dir containing Cargo.toml)")
    d.add_argument("--graph", default=None, help="graph.json to cross-reference against (default: <target>/rust-codemap/<crate>/graph.json)")
    d.add_argument("--out", default=None, help="Where to write source_index.json (default: <target>/rust-codemap/<crate>/source_index.json)")
    d.set_defaults(func=cmd_doc)

    t = sub.add_parser("trace", help="Parse a tracing_subscriber JSON-lines log into trace.json")
    t.add_argument("--input", required=True, help="Path to the raw trace log (JSON lines)")
    t.add_argument("--project", default=None, help="Optional -- if given, default --out is <target>/rust-codemap/<crate>/trace.json")
    t.add_argument("--out", default=None, help="Where to write trace.json (default: trace.json, or under --project's target dir)")
    t.set_defaults(func=cmd_trace)

    s = sub.add_parser("serve", help="Serve the viewer directory over HTTP")
    s.add_argument("--dir", default="viewer", help="Directory to serve (should contain index.html)")
    s.add_argument("--port", type=int, default=8787)
    s.set_defaults(func=cmd_serve)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
