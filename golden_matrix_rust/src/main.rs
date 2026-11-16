//! Standalone Golden Matrix executable.
//!
//! The implementation lives in `bin/stack_generate.rs` so the default package
//! binary and the explicitly named parity binary execute the same code path.
//! Keeping one implementation prevents the former `StdRng` prototype from
//! silently diverging from the NumPy-compatible generator.

include!("bin/stack_generate.rs");
