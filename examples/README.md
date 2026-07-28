# examples

A small, self-contained target for trying rust-codemap without needing any
other project on disk: `dummy-core` + `dummy-api` (two crates, so the
cross-crate collision case has something real to cross) + `dummy-cli`.
Simplified on purpose -- for the exhaustive regression fixture
(`[lib] name` overrides, private/public toggling, etc.), see the separate
`dummy-lib`/`dummy-cli` this one is modeled on; that pair is left untouched.

`dummy-api` has one small function chain per test case (3-4 calls deep,
except where the case is specifically about depth/looping/recursion);
`dummy-cli` runs exactly one of them per invocation, picked by name, and
writes its own trace to `target/traces/trace_<name>.jsonl` (under `target/`
deliberately -- generated output, not committed, regenerate any time):

```sh
cd examples/dummy-cli
cargo run -- <test-name>          # writes target/traces/trace_<test-name>.jsonl
cargo run                         # no argument -- prints the list below
```

| Test name      | Demonstrates                                                                          |
|----------------|-----------------------------------------------------------------------------------------|
| `simple_graph` | A plain call chain, no branching/looping/concurrency -- the baseline case               |
| `gap`          | A traced function calling a traced one *through* an untraced intermediate               |
| `branch`       | An `if`/`else` to two different callees -- both are real edges in the static graph, one run only takes one (captures which branch via `record()`) |
| `dispatch`     | Static calls (3 concrete types) next to a dynamic one (`&dyn Trait`, called with all 3) -- the dynamic one fans out to every implementor in the static graph, the static ones each resolve to exactly one |
| `workflow`     | A real mix, not one isolated concept: a `match` on a real enum (`WorkflowKind::Square`/`Triangle`/`Other`), an `if`/`else` inside one arm, a loop inside another, all converging on one shared "finish" helper -- meant to look like an actual codebase's call graph, not a toy chain. One real call (like `branch`), so only one arm actually runs -- the other two stay real, declared edges in the static graph |
| `iterations`   | The same call site invoked repeatedly in a loop -- aggregates into one entry with a real `iterations` count |
| `recursive`    | Plain self-recursion (factorial-style)                                                   |
| `async_mono`   | Two async fns genuinely concurrent on a single thread (`tokio::join!`)                   |
| `async_multi`  | An async task that genuinely migrates across worker threads on a multi-threaded runtime, then calls a child |
| `collision`    | The same function names (`describe`/`run`) in two different crates -- rust-codemap qualifies node ids by crate so they render as distinct nodes instead of merging |

Try one, e.g.:

```sh
cd examples/dummy-cli && cargo run -- branch
cd ../..   # rust-codemap root, PYTHONPATH=src already set (see main README)
python -m codemap run --project examples/dummy-cli --include-private
# then "Load trace..." in the toolbar -> examples/dummy-cli/target/traces/trace_branch.jsonl
```

`--include-private` matters here: every function in this example except
the entry points is a private (non-`pub`) helper, and the trace can only
resolve/qualify a name via `cargo doc`'s own output for it.
