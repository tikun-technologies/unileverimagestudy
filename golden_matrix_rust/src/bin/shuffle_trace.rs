#[path = "../numpy_compat.rs"]
mod numpy_compat;

use numpy_compat::Pcg64;
use serde_json::{json, Value};
use std::{env, fs};

fn u128_value(value: &Value) -> u128 {
    value.as_str().map(str::to_owned).unwrap_or_else(|| value.to_string()).parse().unwrap()
}

fn main() {
    let path = env::args().nth(1).expect("trace path");
    let input: Value = serde_json::from_str(&fs::read_to_string(path).unwrap()).unwrap();
    let state = &input["input_state"];
    let mut rng = Pcg64::from_raw_state(
        u128_value(&state["state"]), u128_value(&state["inc"]),
        state["has_uint32"].as_u64().unwrap() != 0, state["uinteger"].as_u64().unwrap() as u32,
    );
    let mut block: Vec<Vec<u8>> = serde_json::from_value(input["input_matrix"].clone()).unwrap();
    let before = block.clone();
    rng.shuffle_slice(&mut block);
    println!("{}", serde_json::to_string(&json!({"input_matrix":before,"input_state":state,
        "output_matrix":block,"output_state":{"state":rng.state.to_string(),"inc":rng.increment.to_string(),
        "has_uint32":usize::from(rng.has_uint32),"uinteger":rng.cached_uint32}})).unwrap());
}
