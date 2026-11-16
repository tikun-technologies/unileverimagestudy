#[path = "../numpy_compat.rs"]
mod numpy_compat;
#[path = "../openblas_compat.rs"]
mod openblas_compat;

use numpy_compat::Pcg64;
use serde::Deserialize;
use serde_json::{json, Value};
use std::{env, fs};

const N_CANDIDATES: usize = 100;
const POLISH_PASSES: usize = 3;
const BALANCE_TOL: f64 = 0.05;

#[derive(Deserialize)]
struct Config {
    #[serde(default)]
    tasks_per_respondent: usize,
    elements_per_category: Vec<usize>,
    min_actives_per_row: usize,
    max_actives_per_row: usize,
    #[serde(default)]
    element_priors: Option<Vec<f64>>,
    #[serde(default)]
    incompatible_pairs: Vec<[usize; 2]>,
    #[serde(default)]
    seed: i64,
}

fn state(rng: &Pcg64) -> Value {
    json!({"state":rng.state.to_string(),"inc":rng.increment.to_string(),
        "has_uint32":usize::from(rng.has_uint32),"uinteger":rng.cached_uint32})
}

fn u128_value(value: &Value) -> u128 {
    let text = value.as_str().map(str::to_owned).unwrap_or_else(|| value.to_string());
    text.parse().unwrap_or_else(|_| panic!("invalid u128 state value: {text}"))
}

fn pairwise_sum(values: &[f64]) -> f64 {
    if values.len() < 8 { return values.iter().fold(0.0, |a, b| a + b); }
    let full = values.len() - values.len() % 8;
    let mut lanes = [0.0; 8];
    for chunk in values[..full].chunks_exact(8) { for i in 0..8 { lanes[i] += chunk[i]; } }
    lanes[0] += lanes[1]; lanes[2] += lanes[3]; lanes[4] += lanes[5]; lanes[6] += lanes[7];
    lanes[0] += lanes[2]; lanes[4] += lanes[6]; lanes[0] += lanes[4];
    for value in &values[full..] { lanes[0] += value; }
    lanes[0]
}

fn row(config: &Config, rng: &mut Pcg64) -> Vec<u8> {
    let p: usize = config.elements_per_category.iter().sum();
    let active = config.min_actives_per_row
        + rng.bounded_u64_inclusive(0, (config.max_actives_per_row - config.min_actives_per_row) as u64) as usize;
    let categories = rng.choice_without_replacement(config.elements_per_category.len(), active);
    let mut starts = Vec::with_capacity(config.elements_per_category.len());
    let mut offset = 0;
    for count in &config.elements_per_category { starts.push(offset); offset += count; }
    let mut result = vec![0u8; p];
    for category in categories {
        let selected = rng.bounded_u64_inclusive(0, (config.elements_per_category[category] - 1) as u64) as usize;
        result[starts[category] + selected] = 1;
    }
    result
}

fn d_efficiency(rows: &[Vec<u8>], p: usize, blas: &openblas_compat::OpenBlas) -> f64 {
    let n = rows.len();
    let flat: Vec<f64> = rows.iter().flatten().map(|value| *value as f64).collect();
    match blas.slogdet_cross_product(&flat, n, p) {
        Ok((sign, logdet)) if sign > 0.0 => (logdet / p as f64).exp() / n as f64,
        _ => 0.0,
    }
}

fn balance(rows: &[Vec<u8>], target: &[f64]) -> f64 {
    let p = target.len();
    let values: Vec<f64> = (0..p).map(|j| {
        let exposure: f64 = rows.iter().map(|row| row[j] as f64).sum();
        (exposure - target[j]).abs()
    }).collect();
    pairwise_sum(&values)
}

fn main() {
    let config_path = env::args().nth(1).expect("config path");
    let trace_path = env::args().nth(2).expect("python polish trace path");
    let config: Config = serde_json::from_str(&fs::read_to_string(config_path).unwrap()).unwrap();
    let input: Value = serde_json::from_str(&fs::read_to_string(trace_path).unwrap()).unwrap();
    let backend_path = env::var("NUMPY_OPENBLAS_DLL").expect("NUMPY_OPENBLAS_DLL");
    let backend = unsafe { openblas_compat::OpenBlas::load(&backend_path).expect("load OpenBLAS") };
    let mut rows: Vec<Vec<u8>> = serde_json::from_value(input["pre_polish_matrix"].clone()).unwrap();
    let raw_state = &input["pre_polish_rng_state"];
    let mut rng = Pcg64::from_raw_state(
        u128_value(&raw_state["state"]),
        u128_value(&raw_state["inc"]),
        raw_state["has_uint32"].as_u64().unwrap() != 0,
        raw_state["uinteger"].as_u64().unwrap() as u32,
    );
    let p: usize = config.elements_per_category.iter().sum();
    let tasks = if config.tasks_per_respondent == 0 { (3 * p + 1) / 2 } else { config.tasks_per_respondent };
    let avg = (config.min_actives_per_row + config.max_actives_per_row) as f64 / 2.0;
    let mut weights = config.element_priors.clone().unwrap_or_else(|| vec![1.0; p]);
    let mean = weights.iter().sum::<f64>() / p as f64;
    for weight in &mut weights { *weight /= mean; }
    let target_base = tasks as f64 * avg / p as f64;
    let target: Vec<f64> = weights.iter().map(|weight| weight * target_base).collect();
    let mut current_d = d_efficiency(&rows, p, &backend);
    let mut current_balance = balance(&rows, &target);
    let mut passes = Vec::new();

    for pass_index in 0..POLISH_PASSES {
        let before_pass = rows.clone();
        let mut improved = false;
        let mut row_traces = Vec::new();
        for row_index in 0..rows.len() {
            let old = rows[row_index].clone();
            let mut best = old.clone();
            let mut best_d = current_d;
            let others: Vec<Vec<u8>> = rows.iter().enumerate().filter(|(i, _)| *i != row_index).map(|(_, row)| row.clone()).collect();
            let mut candidates = Vec::new();
            for candidate_index in 0..N_CANDIDATES {
                let before = state(&rng);
                let candidate = row(&config, &mut rng);
                let after = state(&rng);
                let mut item = json!({"candidate_index":candidate_index,"rng_before":before,
                    "rng_after":after,"row":candidate,"accepted":false});
                let duplicate = others.iter().any(|other| *other == candidate);
                let incompatible = config.incompatible_pairs.iter().any(|[a,b]| candidate[*a] == 1 && candidate[*b] == 1);
                if duplicate || incompatible {
                    item["rejection"] = json!(if duplicate {"duplicate"} else {"incompatible"});
                    candidates.push(item);
                    continue;
                }
                rows[row_index] = candidate.clone();
                let new_d = d_efficiency(&rows, p, &backend);
                item["new_d"] = json!(new_d);
                item["improves_d"] = json!(new_d > best_d);
                if new_d > best_d {
                    let new_balance = balance(&rows, &target);
                    let degradation = (new_balance - current_balance) / current_balance.max(1.0);
                    item["new_balance"] = json!(new_balance);
                    item["degradation"] = json!(degradation);
                    item["balance_allowed"] = json!(degradation <= BALANCE_TOL);
                    if degradation <= BALANCE_TOL { best = candidate.clone(); best_d = new_d; item["accepted"] = json!(true); }
                }
                rows[row_index] = old.clone();
                candidates.push(item);
            }
            let selected = best != old;
            if selected { rows[row_index] = best.clone(); current_d = best_d; current_balance = balance(&rows, &target); improved = true; }
            row_traces.push(json!({"row_index":row_index,"old_row":old,"candidates":candidates,
                "selected":selected,"selected_row":best,"selected_d":best_d,"current_balance":current_balance}));
        }
        passes.push(json!({"pass_index":pass_index,"input_matrix":before_pass,"rows":row_traces,
            "improved":improved,"output_matrix":rows,"output_d":current_d,"output_balance":current_balance}));
        if !improved { break; }
    }
    if env::var("POLISH_BENCH").ok().as_deref() != Some("1") {
        println!("{}", serde_json::to_string(&json!({"pre_polish_matrix":input["pre_polish_matrix"],
            "pre_polish_rng_state":input["pre_polish_rng_state"],"post_polish_rng_state":state(&rng),
            "passes":passes,"final_matrix":rows})).unwrap());
    }
}
