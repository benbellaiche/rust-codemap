//! Simplified example fixture for rust-codemap -- one entry point per test
//! case, each 3/4 calls deep (unless the case itself is specifically about
//! depth/iteration/recursion), so `examples/dummy-cli` can run any one of
//! them in isolation and produce a small, easy-to-read trace. Not a
//! replacement for `dummy-lib`/`dummy-cli` (the exhaustive regression
//! fixture those still serve) -- this one is for a quick first look.
//!
//! Depends on `dummy-core`, a separate small crate whose only job is the
//! `collision` case below -- see its own docstring.

use tracing::instrument;

// ── collision: `describe`/`run` exist here AND in dummy-core, same bare
// names, unrelated logic -- rust-codemap qualifies graph node ids by crate
// (`dummy_api::describe` vs `dummy_core::describe`) specifically so these
// render as two distinct nodes instead of merging into one.
//
// `dummy-cli` calls `collision_entry`, not `run` directly -- confirmed
// directly (reading the actual generated graph, not assumed) that calling
// a same-named-in-2-crates function straight from a THIRD crate can hit a
// real, separate MIR quirk: for this specific call shape, rustc's MIR
// dropped the explicit `dummy_api::` qualifier the source actually wrote,
// leaving a bare `run(...)` -- and since dummy-core ALSO defines `run`,
// mir_graph.py's own no-hint fallback (correctly, by design -- see
// merge_crates' own docstring) can't tell which one was meant and shows
// BOTH as possible targets, same over-approximation as a `&dyn Trait` call.
// That's a real, separate lesson (an ambiguous-cross-crate-call case, not
// yet covered by any fixture before this one) worth keeping in mind, but
// not what THIS case is about -- routing through a uniquely-named entry
// point sidesteps it: `collision_entry` only exists in dummy-api, so it
// resolves cleanly even if MIR gives it the same bare treatment, and its
// own call to `run` from INSIDE dummy-api resolves same-crate-first
// (checked before any other crate specifically so this doesn't collide).
#[instrument]
pub fn collision_entry(x: i32) -> i32 {
    run(x)
}

#[instrument]
pub fn describe(x: i32) -> i32 {
    x * 3 + 1
}

#[instrument]
pub fn run(x: i32) -> i32 {
    describe(x) + dummy_core::run(x)
}

// ── simple_graph: the baseline case, no branching/looping/concurrency ──────

#[instrument]
pub fn simple_entry(x: i32) -> i32 {
    simple_step_a(x) + 1
}

#[instrument]
fn simple_step_a(x: i32) -> i32 {
    simple_step_b(x) * 2
}

#[instrument]
fn simple_step_b(x: i32) -> i32 {
    simple_step_c(x) + 3
}

#[instrument]
fn simple_step_c(x: i32) -> i32 {
    x * 10
}

// ── gap: a traced function calling a traced one THROUGH an untraced
// intermediate -- the static graph has real edges both hops, but the trace
// attributes gap_leaf directly to gap_entry (its still-open real ancestor),
// with no matching direct edge in the graph for that specific pair ──

#[instrument]
pub fn gap_entry(x: i32) -> i32 {
    gap_relay(x) + 1
}

// Deliberately NOT #[instrument]'d -- this is the whole point of this case.
fn gap_relay(x: i32) -> i32 {
    gap_leaf(x) * 2
}

#[instrument]
fn gap_leaf(x: i32) -> i32 {
    x + 5
}

// ── branch: an if/else to two different callees -- the static graph shows
// BOTH edges (both are real, declared calls), a single run only ever takes
// one. Captures which branch was taken via `record()`, since that's exactly
// the kind of internal state you can't see from the entry arguments alone ──

#[instrument(fields(branch = tracing::field::Empty))]
pub fn branch_entry(x: i32) -> i32 {
    let taken = if x % 2 == 0 { "even" } else { "odd" };
    tracing::Span::current().record("branch", taken);
    if x % 2 == 0 {
        branch_even(x) + 1
    } else {
        branch_odd(x) + 1
    }
}

#[instrument]
fn branch_even(x: i32) -> i32 {
    branch_finish(x * 2)
}

#[instrument]
fn branch_odd(x: i32) -> i32 {
    branch_finish(x * 3)
}

#[instrument]
fn branch_finish(x: i32) -> i32 {
    x + 1
}

// ── dispatch: static calls (concrete type, resolved at compile time) next
// to dynamic ones (`&dyn Trait`), THREE implementors of `Shape` so the
// difference is actually visible rather than a single implementation
// standing in for "the" one. `dispatch_static_*` each take their own
// concrete type directly, so each one's own `shape.area()` call resolves to
// that one type's `area()` alone -- a real, different static edge per
// function. `dispatch_dynamic` takes `&dyn Shape` and is called three
// times, once per shape: its own `shape.area()` goes through a vtable, so
// mir_graph.py can't know the concrete type at compile time and
// over-approximates -- ONE static edge to EVERY `Shape` implementor
// (`Square::area`, `Circle::area`, AND `Triangle::area`), even though each
// individual call at runtime only ever resolves to one of them. Running
// all three real calls in the same trace is what makes that contrast
// visible: three real, different `area()` invocations show up, against the
// same one static fan-out edge all three of them share ──

pub trait Shape {
    fn area(&self) -> i32;
}

pub struct Square(pub i32);

impl Shape for Square {
    #[instrument(skip(self))]
    fn area(&self) -> i32 {
        self.0 * self.0
    }
}

pub struct Circle(pub i32);

impl Shape for Circle {
    #[instrument(skip(self))]
    fn area(&self) -> i32 {
        self.0 * self.0 * 3
    }
}

pub struct Triangle(pub i32, pub i32);

impl Shape for Triangle {
    #[instrument(skip(self))]
    fn area(&self) -> i32 {
        (self.0 * self.1) / 2
    }
}

#[instrument]
pub fn dispatch_entry(size: i32) -> i32 {
    let static_total = dispatch_static_square(Square(size))
        + dispatch_static_circle(Circle(size))
        + dispatch_static_triangle(Triangle(size, size));
    let dynamic_total = dispatch_dynamic(&Square(size))
        + dispatch_dynamic(&Circle(size))
        + dispatch_dynamic(&Triangle(size, size));
    static_total + dynamic_total
}

#[instrument(skip(shape))]
fn dispatch_static_square(shape: Square) -> i32 {
    shape.area() + 1
}

#[instrument(skip(shape))]
fn dispatch_static_circle(shape: Circle) -> i32 {
    shape.area() + 1
}

#[instrument(skip(shape))]
fn dispatch_static_triangle(shape: Triangle) -> i32 {
    shape.area() + 1
}

#[instrument(skip(shape))]
fn dispatch_dynamic(shape: &dyn Shape) -> i32 {
    shape.area() + 2
}

// ── workflow: a real mix, the way an actual codebase's call graph tends to
// look -- not one isolated concept per function. `workflow_entry` pattern-
// matches on a real enum with several matchable values (a real Rust
// `match`, three arms, generalizing `branch`'s plain if/else to more than
// two outcomes -- and a real `enum` rather than an arithmetic `x % 3`
// trick, since real match statements match real variants, not remainders);
// the "mid" arm has its OWN if/else inside it; the "high" arm loops,
// calling the same callee repeatedly (`iterations`'s pattern, reused here
// rather than invented again); every arm eventually calls the SAME shared
// `workflow_finish` -- a real, common shape (a shared "finalize" helper)
// that gives `workflow_finish` multiple real incoming edges in the static
// graph, visibly converging even though the one real call `dummy-cli` makes
// only ever lights up one of them (same as `branch`'s own single call --
// the OTHER arms stay real, declared edges in the static graph, just not
// ones this particular run happened to take). The "low" arm goes deepest
// (7 levels from `main`) ──

#[derive(Debug)]
pub enum WorkflowKind {
    Square(i32),
    Triangle(i32),
    Other(i32),
}

#[instrument]
pub fn workflow_entry(kind: WorkflowKind) -> i32 {
    match kind {
        WorkflowKind::Square(x) => workflow_low(x),
        WorkflowKind::Triangle(x) => workflow_mid(x),
        WorkflowKind::Other(x) => workflow_high(x),
    }
}

#[instrument]
fn workflow_low(x: i32) -> i32 {
    workflow_validate(x)
}

#[instrument]
fn workflow_validate(x: i32) -> i32 {
    workflow_normalize(x + 1)
}

#[instrument]
fn workflow_normalize(x: i32) -> i32 {
    workflow_polish(x * 2)
}

#[instrument]
fn workflow_polish(x: i32) -> i32 {
    workflow_finish(x - 1)
}

#[instrument]
fn workflow_mid(x: i32) -> i32 {
    if x % 2 == 0 {
        workflow_even_path(x)
    } else {
        workflow_odd_path(x)
    }
}

#[instrument]
fn workflow_even_path(x: i32) -> i32 {
    workflow_finish(x * 2)
}

#[instrument]
fn workflow_odd_path(x: i32) -> i32 {
    workflow_finish(x * 3)
}

#[instrument]
fn workflow_high(x: i32) -> i32 {
    let mut total = 0;
    for i in 0..3 {
        total += workflow_accumulate(x, i);
    }
    workflow_finish(total)
}

#[instrument]
fn workflow_accumulate(x: i32, i: i32) -> i32 {
    x + workflow_weight(i)
}

#[instrument]
fn workflow_weight(i: i32) -> i32 {
    i * i
}

// Shared by workflow_low (via workflow_polish), workflow_mid's both
// branches, and workflow_high -- a real converging point, not a linear
// chain: the static graph shows four distinct incoming edges here.
#[instrument]
fn workflow_finish(x: i32) -> i32 {
    x + 1
}

// ── iterations: the SAME call site invoked repeatedly in a loop -- the
// trace aggregates these into one deduped entry with `iterations` set to
// the real count, rather than one entry per call. Records the running
// total at the end, the one internal value worth seeing that the loop's
// own entry argument (`count`) doesn't already show ──

#[instrument(fields(total = tracing::field::Empty))]
pub fn iterations_entry(count: i32) -> i32 {
    let mut total = 0;
    for i in 0..count {
        total += iterations_step(i);
    }
    tracing::Span::current().record("total", total);
    total
}

#[instrument]
fn iterations_step(i: i32) -> i32 {
    iterations_square(i) + 1
}

#[instrument]
fn iterations_square(i: i32) -> i32 {
    i * i
}

// ── recursive: plain self-recursion, same shape as a textbook factorial ──

#[instrument]
pub fn recursive_entry(n: i32) -> i32 {
    recursive_factorial(n)
}

#[instrument]
fn recursive_factorial(n: i32) -> i32 {
    if n <= 1 {
        1
    } else {
        n * recursive_factorial(n - 1)
    }
}

// ── async_mono: two independent async fns genuinely concurrent on a single
// thread (`tokio::join!`) -- async_mono_task_b's sleep is much shorter, so
// it wakes and finishes WHILE async_mono_task_a is still suspended. Run
// under `#[tokio::main(flavor = "current_thread")]` from the CLI ──

#[instrument]
pub async fn async_mono_entry() -> i32 {
    let (a, b) = tokio::join!(async_mono_task_a(1), async_mono_task_b(2));
    a + b
}

#[instrument]
async fn async_mono_task_a(x: i32) -> i32 {
    tokio::time::sleep(std::time::Duration::from_millis(50)).await;
    async_mono_helper(x) + 1
}

#[instrument]
async fn async_mono_task_b(x: i32) -> i32 {
    tokio::time::sleep(std::time::Duration::from_millis(10)).await;
    x + 100
}

#[instrument]
fn async_mono_helper(x: i32) -> i32 {
    x * 2
}

// ── async_multi: a task that genuinely migrates across worker threads on a
// dedicated multi-threaded runtime, under real scheduling pressure from
// competing sibling tasks, then calls a child -- the regression case for
// trace_log.py's GLOBAL (not per-thread) suspended-span stash. Builds its
// own runtime on a plain std::thread rather than via the CLI's own, so it
// doesn't need `#[tokio::main]` at all ──

#[instrument]
pub fn async_multi_entry() -> i32 {
    std::thread::spawn(|| {
        let rt = tokio::runtime::Builder::new_multi_thread()
            .worker_threads(4)
            .enable_all()
            .build()
            .expect("failed to build dedicated multi-thread runtime");
        rt.block_on(async {
            let mut handles = Vec::with_capacity(50);
            for _ in 0..50 {
                handles.push(tokio::spawn(async_multi_busy_sibling()));
            }
            let parent_handle = tokio::spawn(async_multi_migrating(10));
            let r = parent_handle.await.expect("migrating task panicked");
            for h in handles {
                let _ = h.await;
            }
            r
        })
    })
    .join()
    .expect("dedicated runtime thread panicked")
}

#[instrument]
async fn async_multi_migrating(x: i32) -> i32 {
    for _ in 0..20 {
        tokio::task::yield_now().await;
    }
    let c = async_multi_child(x).await;
    x + c
}

#[instrument]
async fn async_multi_child(x: i32) -> i32 {
    x * 2
}

// Deliberately NOT #[instrument]'d -- floods the runtime with yielding
// tasks, needed to create enough real scheduling pressure that
// async_multi_migrating actually gets migrated between workers.
async fn async_multi_busy_sibling() {
    for _ in 0..20 {
        tokio::task::yield_now().await;
    }
}
