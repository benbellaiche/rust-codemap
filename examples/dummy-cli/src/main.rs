//! Runs exactly one `dummy-api` test case per invocation, picked by name,
//! and writes its trace to `target/traces/trace_<name>.jsonl` -- run
//! `cargo run -- <name>` with no argument to see the list of valid names.
//! Deliberately one-at-a-time, not "run everything": each case is meant to
//! be looked at on its own, with its own small, easy-to-read trace file,
//! not mixed into one combined log the way `dummy-cli` (the exhaustive
//! fixture this one simplifies) does.
//!
//! Written under `target/` on purpose, not committed alongside the source:
//! these are generated output, regenerated any time with `cargo run`, same
//! reasoning as everything else `target/` already holds.

use std::env;
use std::fs;
use std::fs::File;

const TEST_NAMES: &[&str] = &[
    "simple_graph",
    "gap",
    "branch",
    "dispatch",
    "workflow",
    "iterations",
    "recursive",
    "async_mono",
    "async_multi",
    "collision",
];

fn print_usage() {
    eprintln!("usage: dummy-cli <test-name>");
    eprintln!("available test names:");
    for name in TEST_NAMES {
        eprintln!("  {name}");
    }
}

fn main() {
    let test_name = match env::args().nth(1) {
        Some(name) => name,
        None => {
            print_usage();
            std::process::exit(1);
        }
    };
    if !TEST_NAMES.contains(&test_name.as_str()) {
        eprintln!("unknown test name '{test_name}'\n");
        print_usage();
        std::process::exit(1);
    }

    let log_dir = "target/traces";
    fs::create_dir_all(log_dir).expect("failed to create target/traces");
    let log_path = format!("{log_dir}/trace_{test_name}.jsonl");
    let file = File::create(&log_path).expect("failed to create log file");
    tracing_subscriber::fmt()
        .json()
        .with_span_events(
            tracing_subscriber::fmt::format::FmtSpan::NEW
                | tracing_subscriber::fmt::format::FmtSpan::ENTER
                | tracing_subscriber::fmt::format::FmtSpan::EXIT
                | tracing_subscriber::fmt::format::FmtSpan::CLOSE,
        )
        .with_file(true)
        .with_line_number(true)
        .with_thread_ids(true)
        .with_writer(move || file.try_clone().expect("failed to clone log file handle"))
        .init();

    let result = match test_name.as_str() {
        "simple_graph" => dummy_api::simple_entry(5).to_string(),
        "gap" => dummy_api::gap_entry(5).to_string(),
        "branch" => dummy_api::branch_entry(5).to_string(),
        "dispatch" => dummy_api::dispatch_entry(3).to_string(),
        // One real call, same as `branch` -- only this one arm (the
        // deepest, 7 levels from main) actually runs; Triangle/Other stay
        // real, declared edges in the static graph regardless.
        "workflow" => dummy_api::workflow_entry(dummy_api::WorkflowKind::Square(0)).to_string(),
        "iterations" => dummy_api::iterations_entry(5).to_string(),
        "recursive" => dummy_api::recursive_entry(5).to_string(),
        "async_mono" => tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .expect("failed to build current-thread runtime")
            .block_on(dummy_api::async_mono_entry())
            .to_string(),
        "async_multi" => dummy_api::async_multi_entry().to_string(),
        "collision" => dummy_api::collision_entry(5).to_string(),
        _ => unreachable!("already validated against TEST_NAMES above"),
    };

    println!("test={test_name} result={result}");
    println!("log written to {log_path}");
}
