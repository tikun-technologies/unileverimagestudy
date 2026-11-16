#[path = "../numpy_compat.rs"]
mod numpy_compat;
#[path = "../openblas_compat.rs"]
mod openblas_compat;

use numpy_compat::Pcg64;
use serde::Deserialize;
use serde_json::{json, Value};
use std::{env, fs};

const N_CANDIDATES: usize = 100;
const W_VIF_ROW: f64 = 0.25;

#[derive(Deserialize)]
struct Config {
    #[serde(default = "default_respondents")]
    n_respondents: usize,
    #[serde(default)]
    tasks_per_respondent: usize,
    elements_per_category: Vec<usize>,
    min_actives_per_row: usize,
    max_actives_per_row: usize,
    #[serde(default)]
    element_priors: Option<Vec<f64>>,
    #[serde(default)]
    incompatible_pairs: Vec<[usize; 2]>,
    #[serde(default = "default_wg")]
    w_global: f64,
    #[serde(default = "default_wl")]
    w_local: f64,
    #[serde(default = "default_true")]
    skip_vif: bool,
    #[serde(default)]
    seed: i64,
}

fn default_wg() -> f64 { 0.1 }
fn default_wl() -> f64 { 0.9 }
fn default_true() -> bool { true }
fn default_respondents() -> usize { 1 }

// NumPy's float64 reduction uses pairwise accumulation for sufficiently long
// contiguous reductions. This reproduces its eight-lane tree for the small
// design vectors used by candidate scoring.
fn numpy_pairwise_sum(values: &[f64]) -> f64 {
    if values.len() < 8 {
        return values.iter().fold(0.0, |sum, value| sum + value);
    }
    let full = values.len() - values.len() % 8;
    let mut lanes = [0.0f64; 8];
    for chunk in values[..full].chunks_exact(8) {
        for lane in 0..8 { lanes[lane] += chunk[lane]; }
    }
    lanes[0] += lanes[1];
    lanes[2] += lanes[3];
    lanes[4] += lanes[5];
    lanes[6] += lanes[7];
    lanes[0] += lanes[2];
    lanes[4] += lanes[6];
    lanes[0] += lanes[4];
    for value in &values[full..] { lanes[0] += value; }
    lanes[0]
}

fn state(rng: &Pcg64) -> Value {
    json!({"state": rng.state.to_string(), "inc": rng.increment.to_string(),
           "has_uint32": usize::from(rng.has_uint32), "uinteger": rng.cached_uint32})
}

fn trace_row(config: &Config, rng: &mut Pcg64) -> (Vec<u8>, Vec<Value>) {
    let mut calls = Vec::new();
    let before = state(rng);
    let active_count = config.min_actives_per_row
        + rng.bounded_u64_inclusive(0, (config.max_actives_per_row - config.min_actives_per_row) as u64) as usize;
    calls.push(json!({"operation":"integers", "low":config.min_actives_per_row,
        "high":config.max_actives_per_row, "endpoint":true, "result":active_count,
        "state_before":before, "state_after":state(rng)}));

    let before = state(rng);
    let categories = rng.choice_without_replacement(config.elements_per_category.len(), active_count);
    calls.push(json!({"operation":"choice_without_replacement",
        "population_size":config.elements_per_category.len(), "size":active_count,
        "replace":false, "result":categories, "state_before":before,
        "state_after":state(rng)}));

    let total: usize = config.elements_per_category.iter().sum();
    let mut row = vec![0u8; total];
    let mut offset = 0;
    let starts: Vec<usize> = config.elements_per_category.iter().map(|count| {
        let current = offset;
        offset += count;
        current
    }).collect();
    for category in categories {
        let population: Vec<usize> = (starts[category]..starts[category] + config.elements_per_category[category]).collect();
        let before = state(rng);
        let selected = rng.bounded_u64_inclusive(0, (population.len() - 1) as u64) as usize;
        let selected_absolute = starts[category] + selected;
        row[selected_absolute] = 1;
        calls.push(json!({"operation":"choice_scalar", "population":population,
            "result":selected_absolute, "state_before":before,
            "state_after":state(rng)}));
    }
    (row, calls)
}

fn inverse(mut a: Vec<Vec<f64>>) -> Option<Vec<Vec<f64>>> {
    let n = a.len();
    let mut inv = vec![vec![0.0; n]; n];
    for i in 0..n { inv[i][i] = 1.0; }
    for k in 0..n {
        let pivot = (k..n).max_by(|&i, &j| a[i][k].abs().total_cmp(&a[j][k].abs()))?;
        if a[pivot][k].abs() < 1e-12 { return None; }
        if pivot != k { a.swap(pivot, k); inv.swap(pivot, k); }
        let divisor = a[k][k];
        for j in 0..n { a[k][j] /= divisor; inv[k][j] /= divisor; }
        for i in 0..n {
            if i == k { continue; }
            let factor = a[i][k];
            for j in 0..n { a[i][j] -= factor * a[k][j]; inv[i][j] -= factor * inv[k][j]; }
        }
    }
    Some(inv)
}

fn vif(rows: &[Vec<u8>], p: usize) -> f64 {
    if rows.len() < p { return f64::INFINITY; }
    let n = rows.len() as f64;
    let mut gram = vec![vec![0.0; p]; p];
    let sums: Vec<f64> = (0..p).map(|j| rows.iter().map(|r| r[j] as f64).sum()).collect();
    for row in rows {
        for i in 0..p { if row[i] > 0 { for j in 0..p { gram[i][j] += row[j] as f64; } } }
    }
    let mut corr = vec![vec![0.0; p]; p];
    for i in 0..p { for j in 0..p {
        let vi = gram[i][i] - sums[i] * sums[i] / n;
        let vj = gram[j][j] - sums[j] * sums[j] / n;
        if vi <= 0.0 || vj <= 0.0 { return f64::INFINITY; }
        corr[i][j] = (gram[i][j] - sums[i] * sums[j] / n) / (vi * vj).sqrt();
    }}
    inverse(corr).map(|m| (0..p).map(|i| m[i][i]).fold(f64::NEG_INFINITY, f64::max))
        .unwrap_or(f64::INFINITY)
}

fn vif_openblas(rows: &[Vec<u8>], p: usize, blas: &openblas_compat::OpenBlas) -> f64 {
    let n = rows.len();
    if n < p { return f64::INFINITY; }
    let means: Vec<f64> = (0..p).map(|j| {
        let values: Vec<f64> = rows.iter().map(|row| row[j] as f64).collect();
        numpy_pairwise_sum(&values) / n as f64
    }).collect();
    let centered: Vec<f64> = rows.iter().flat_map(|row| {
        row.iter().enumerate().map(|(j, value)| *value as f64 - means[j]).collect::<Vec<_>>()
    }).collect();
    let product = blas.covariance_product_col_major(&centered, n, p);
    let factor = 1.0 / (n - 1) as f64;
    let covariance: Vec<Vec<f64>> = (0..p).map(|i| (0..p).map(|j| product[i * p + j] * factor).collect()).collect();
    let variance: Vec<f64> = (0..p).map(|i| covariance[i][i]).collect();
    let mut correlation = covariance;
    for i in 0..p { for j in 0..p { correlation[i][j] /= variance[i].sqrt(); } }
    for i in 0..p { for j in 0..p { correlation[i][j] /= variance[j].sqrt(); } }
    for i in 0..p { for j in 0..p { correlation[i][j] = correlation[i][j].clamp(-1.0, 1.0); } }
    blas.inverse_col_major(&correlation)
        .map(|matrix| (0..p).map(|i| matrix[i][i]).fold(f64::NEG_INFINITY, f64::max))
        .unwrap_or(f64::INFINITY)
}

fn trace_block(config: &Config, seed: u64) -> Value {
    let mut rng = Pcg64::from_seed_sequence(seed as u128);
    let p: usize = config.elements_per_category.iter().sum();
    let tasks = if config.tasks_per_respondent == 0 { (3 * p + 1) / 2 } else { config.tasks_per_respondent };
    let avg = (config.min_actives_per_row + config.max_actives_per_row) as f64 / 2.0;
    let mut weights = config.element_priors.clone().unwrap_or_else(|| vec![1.0; p]);
    let mean = weights.iter().sum::<f64>() / p as f64;
    for weight in &mut weights { *weight /= mean; }
    let global_target = (config.n_respondents * tasks) as f64 * avg / p as f64;
    let local_target = tasks as f64 * avg / p as f64;
    let target_global: Vec<f64> = weights.iter().map(|w| global_target * w).collect();
    let target_local: Vec<f64> = weights.iter().map(|w| local_target * w).collect();
    let mut local = vec![0i32; p];
    let mut rows: Vec<Vec<u8>> = Vec::new();
    let mut slots = Vec::new();
    let blas = env::var("NUMPY_OPENBLAS_DLL").ok()
        .and_then(|path| unsafe { openblas_compat::OpenBlas::load(&path).ok() });

    for slot in 0..tasks {
        let mut candidates = Vec::new();
        let mut accepted: Vec<(usize, f64, Vec<u8>)> = Vec::new();
        for generated_index in 0..N_CANDIDATES {
            let before = state(&rng);
            let (row, calls) = trace_row(config, &mut rng);
            let after = state(&rng);
            let mut item = json!({"generated_index":generated_index, "rng_before":before,
                "rng_after":after, "rng_calls":calls, "row":row, "accepted":false});
            let duplicate = rows.iter().any(|old| old == &row);
            let incompatible = config.incompatible_pairs.iter().any(|[a,b]| row[*a] == 1 && row[*b] == 1);
            if duplicate || incompatible {
                item["rejection"] = json!(if duplicate { "duplicate" } else { "incompatible" });
                candidates.push(item);
                continue;
            }
            let global_values: Vec<f64> = (0..p).map(|j| (row[j] as f64 - target_global[j]).abs()).collect();
            let local_values: Vec<f64> = (0..p).map(|j| ((local[j] + row[j] as i32) as f64 - target_local[j]).abs()).collect();
            let global_raw = numpy_pairwise_sum(&global_values);
            let local_raw = numpy_pairwise_sum(&local_values);
            let global_component = config.w_global * global_raw;
            let local_component = config.w_local * local_raw;
            let need_vif = !config.skip_vif && rows.len() + 1 >= p;
            let trial = [rows.clone(), vec![row.clone()]].concat();
            let vif_value = if need_vif {
                blas.as_ref().map_or_else(|| vif(&trial, p), |backend| vif_openblas(&trial, p, backend))
            } else { f64::NAN };
            let vif_penalty = if need_vif { W_VIF_ROW * if vif_value.is_finite() { vif_value } else { 1e6 } } else { 0.0 };
            let final_score = global_component + local_component + vif_penalty;
            let accepted_index = accepted.len();
            accepted.push((generated_index, final_score, row.clone()));
            item["accepted"] = json!(true);
            item["global_raw"] = json!(global_raw);
            item["local_raw"] = json!(local_raw);
            item["global_component"] = json!(global_component);
            item["local_component"] = json!(local_component);
            item["vif"] = if need_vif && vif_value.is_finite() { json!(vif_value) } else { Value::Null };
            item["vif_penalty"] = json!(vif_penalty);
            item["final_score"] = json!(final_score);
            item["accepted_index"] = json!(accepted_index);
            candidates.push(item);
        }
        let selected_index = accepted.iter().enumerate().min_by(|a,b| a.1.1.total_cmp(&b.1.1)).unwrap().0;
        let (selected_generated, _, selected_row) = accepted[selected_index].clone();
        for j in 0..p { local[j] += selected_row[j] as i32; }
        rows.push(selected_row.clone());
        slots.push(json!({"slot":slot, "candidates":candidates,
            "accepted_candidate_count":accepted.len(), "selected_accepted_index":selected_index,
            "selected_generated_index":selected_generated, "selected_row":selected_row}));
    }
    json!({"seed":seed, "skip_vif":config.skip_vif, "slots":slots, "block":rows})
}

fn main() {
    let path = env::args().nth(1).unwrap_or_else(|| "example.json".to_string());
    let config: Config = serde_json::from_str(&fs::read_to_string(path).expect("read config")).expect("parse config");
    let attempt_seed = config.seed.wrapping_mul(1_000_003).wrapping_add(42) as u64;
    println!("{}", serde_json::to_string(&json!({
        "implementation":"numpy-compatible-rust", "preflight":trace_block(&config, 0),
        "attempt_0":trace_block(&config, attempt_seed)
    })).unwrap());
}
