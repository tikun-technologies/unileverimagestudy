#[path = "../numpy_compat.rs"]
mod numpy_compat;

use numpy_compat::Pcg64;
use serde_json::{json, Value};
use std::{env, fs};

fn u128_value(value: &Value) -> u128 {
    value.as_str().map(str::to_owned).unwrap_or_else(|| value.to_string()).parse().unwrap()
}

fn main() {
    let path = env::args().nth(1).expect("stack trace path");
    let input: Value = serde_json::from_str(&fs::read_to_string(path).unwrap()).unwrap();
    let mut global = Vec::<i64>::new();
    let mut matrix = Vec::<Vec<u8>>::new();
    let mut block_checks = Vec::new();
    let mut retry_counts = Vec::new();
    for respondent in input["respondents"].as_array().unwrap() {
        let attempts = respondent["attempts"].as_array().unwrap();
        retry_counts.push(attempts.len());
        let mut accepted = false;
        for attempt in attempts {
            if attempt.get("block").is_none() { continue; }
            let state = &attempt["block"]["pre_shuffle_state"];
            let mut rng = Pcg64::from_raw_state(u128_value(&state["state"]), u128_value(&state["inc"]),
                state["has_uint32"].as_u64().unwrap() != 0, state["uinteger"].as_u64().unwrap() as u32);
            let mut block: Vec<Vec<u8>> = serde_json::from_value(attempt["block"]["pre_shuffle_matrix"].clone()).unwrap();
            let expected = attempt["block"]["post_shuffle_matrix"].clone();
            rng.shuffle_slice(&mut block);
            let shuffle_equal = serde_json::to_value(&block).unwrap() == expected;
            let expected_state = &attempt["block"]["post_shuffle_state"];
            let state_equal = rng.state == u128_value(&expected_state["state"])
                && rng.increment == u128_value(&expected_state["inc"])
                && usize::from(rng.has_uint32) == expected_state["has_uint32"].as_u64().unwrap() as usize
                && rng.cached_uint32 as u64 == expected_state["uinteger"].as_u64().unwrap();
            block_checks.push(json!({"respondent":respondent["respondent"],"attempt":attempt["attempt"],
                "shuffle_matrix_equal":shuffle_equal,"shuffle_state_equal":state_equal}));
            let rejected = attempt["rejected"].as_bool().unwrap();
            if !rejected {
                if global.is_empty() { global = vec![0; block[0].len()]; }
                for row in &block { for (j, value) in row.iter().enumerate() { global[j] += *value as i64; } }
                matrix.extend(block);
                accepted = true;
                break;
            }
        }
        if !accepted { panic!("respondent replay exhausted"); }
    }
    let expected_matrix = input["matrix"].clone();
    let expected_global = input["global_exposures"].clone();
    println!("{}", serde_json::to_string(&json!({"block_checks":block_checks,"retry_counts":retry_counts,
        "matrix":matrix,"global_exposures":global,"matrix_equal":serde_json::to_value(&matrix).unwrap()==expected_matrix,
        "global_exposures_equal":serde_json::to_value(&global).unwrap()==expected_global,
        "python_diagnostics":input["diagnostics"]})).unwrap());
}
