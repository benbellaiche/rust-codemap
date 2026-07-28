//! Small, separate crate whose only job is the cross-crate collision case:
//! `describe`/`run` exist here AND in `dummy-api`, same bare names, unrelated
//! logic -- rust-codemap qualifies graph node ids by crate
//! (`dummy_core::describe` vs `dummy_api::describe`) specifically so these
//! render as two distinct nodes instead of merging into one.

use tracing::instrument;

#[instrument]
pub fn describe(x: i32) -> i32 {
    x * 2
}

#[instrument]
pub fn run(x: i32) -> i32 {
    describe(x) + 1
}
