#[path = "../numpy_compat.rs"]
mod numpy_compat;

use numpy_compat::Pcg64;
use serde::Deserialize;
use serde_json::{json, Value};
use std::{env, fs};

#[derive(Deserialize)]
struct Config {
    elements_per_category: Vec<usize>,
    min_actives_per_row: usize,
    max_actives_per_row: usize,
    #[serde(default)]
    seed: i64,
}

fn state(generator: &Pcg64) -> Value {
    json!({
        "state": generator.state.to_string(),
        "inc": generator.increment.to_string(),
        "has_uint32": usize::from(generator.has_uint32),
        "uinteger": generator.cached_uint32,
    })
}

fn trace_rows(seed: u64, stream: &str, config: &Config, row_count: usize) -> Vec<Value> {
    let mut generator = Pcg64::from_seed_sequence(seed as u128);
    let mut events = vec![json!({
        "stream": stream,
        "operation": "generator_init",
        "algorithm": "numpy-2.3.2-PCG64",
        "seed": seed,
        "pcg64_state": state(&generator),
    })];

    for row_index in 0..row_count {
        let before = state(&generator);
        let range = config.max_actives_per_row - config.min_actives_per_row;
        let active_count = config.min_actives_per_row
            + generator.bounded_u64_inclusive(0, range as u64) as usize;
        events.push(json!({
            "stream": stream,
            "row_index": row_index,
            "operation": "bounded_integer",
            "low": config.min_actives_per_row,
            "high_inclusive": config.max_actives_per_row,
            "state_before": before,
            "state_after": state(&generator),
            "result": active_count,
        }));

        let before = state(&generator);
        let categories = generator.choice_without_replacement(
            config.elements_per_category.len(), active_count
        );
        events.push(json!({
            "stream": stream,
            "row_index": row_index,
            "operation": "choice_without_replacement",
            "population_size": config.elements_per_category.len(),
            "sample_size": active_count,
            "shuffle": true,
            "state_before": before,
            "state_after": state(&generator),
            "result": categories,
        }));

        for &category in &categories {
            let population: Vec<usize> = (0..config.elements_per_category[category]).collect();
            let before = state(&generator);
            let selected = generator.bounded_u64_inclusive(0, (population.len() - 1) as u64) as usize;
            events.push(json!({
                "stream": stream,
                "row_index": row_index,
                "operation": "choice_with_replacement_scalar",
                "category": category,
                "population": population,
                "state_before": before,
                "state_after": state(&generator),
                "result": selected,
            }));
        }
    }
    events
}

fn main() {
    let path = env::args().nth(1).unwrap_or_else(|| "example.json".to_string());
    let config: Config = serde_json::from_str(&fs::read_to_string(path).expect("read config"))
        .expect("parse config");
    let attempt_seed = config.seed.wrapping_mul(1_000_003).wrapping_add(42) as u64;
    let row_count = env::var("GOLDEN_TRACE_ROWS")
        .ok()
        .and_then(|value| value.parse().ok())
        .unwrap_or(1);
    let output = json!({
        "implementation": "numpy-compatible-rust",
        "preflight": trace_rows(0, "preflight", &config, row_count),
        "attempt_0": trace_rows(attempt_seed, "attempt_0", &config, row_count),
    });
    println!("{}", serde_json::to_string_pretty(&output).unwrap());
}
