#[path = "../numpy_compat.rs"]
mod numpy_compat;

use numpy_compat::{seed_sequence_u64, Pcg64};
use serde_json::json;
use std::env;

fn main() {
    let seed: u128 = env::args()
        .nth(1)
        .unwrap_or_else(|| "0".to_string())
        .parse()
        .expect("non-negative integer seed");
    let seed_words = seed_sequence_u64(seed, 4);
    let mut generator = Pcg64::from_seed_sequence(seed);
    let initial_state = generator.state;
    let increment = generator.increment;
    let raw: Vec<u64> = (0..10).map(|_| generator.next_u64()).collect();
    println!(
        "{}",
        serde_json::to_string_pretty(&json!({
            "seed": seed.to_string(),
            "seed_state_u64": seed_words,
            "initial_state": {
                "state": initial_state.to_string(),
                "inc": increment.to_string(),
                "has_uint32": 0,
                "uinteger": 0
            },
            "raw_u64": raw
        }))
        .unwrap()
    );
}
