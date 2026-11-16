use rand::{seq::SliceRandom, Rng, SeedableRng};
use rand::rngs::StdRng;
use serde_json::json;
use std::collections::HashSet;
use std::time::Instant;

const CATEGORIES: [usize; 3] = [2, 2, 2];
const MIN_ACTIVE: usize = 2;
const MAX_ACTIVE: usize = 3;
const CANDIDATES: usize = 100;

fn generated_respondents(requested: usize) -> usize { if requested == 0 { 0 } else if requested == 1 { 1 } else { requested * 2 } }
fn row_hash(row: &[u8]) -> String { row.iter().map(|v| if *v == 1 { '1' } else { '0' }).collect() }

fn random_row(rng: &mut StdRng) -> Vec<u8> {
    let mut active: Vec<usize> = (0..CATEGORIES.len()).collect();
    active.shuffle(rng);
    let count = rng.gen_range(MIN_ACTIVE..=MAX_ACTIVE);
    let mut row = vec![0u8; CATEGORIES.iter().sum()];
    let mut offset = 0;
    let starts: Vec<usize> = CATEGORIES.iter().map(|n| { let x = offset; offset += n; x }).collect();
    for category in active.into_iter().take(count) { row[starts[category] + rng.gen_range(0..CATEGORIES[category])] = 1; }
    row
}

fn score(row: &[u8], local: &[i32], global: &[i32], target_local: f64, target_global: f64) -> f64 {
    row.iter().enumerate().map(|(i, value)| { let v = *value as f64; 0.1 * ((global[i] as f64 + v) - target_global).abs() + 0.9 * ((local[i] as f64 + v) - target_local).abs() }).sum()
}

fn generate(requested: usize, seed: u64) -> (usize, usize, Vec<Vec<Vec<u8>>>) {
    let respondents = generated_respondents(requested);
    let elements: usize = CATEGORIES.iter().sum();
    let tasks = ((elements as f64) * 1.5).ceil() as usize;
    let mut rng = StdRng::seed_from_u64(seed);
    let mut global = vec![0i32; elements];
    let avg_active = (MIN_ACTIVE + MAX_ACTIVE) as f64 / 2.0;
    let target_global = (respondents * tasks) as f64 * avg_active / elements as f64;
    let target_local = tasks as f64 * avg_active / elements as f64;
    let mut blocks = Vec::with_capacity(respondents);
    let mut block_hashes = HashSet::new();
    for _ in 0..respondents {
        let mut local = vec![0i32; elements];
        let mut block = Vec::with_capacity(tasks);
        let mut row_hashes = HashSet::new();
        for _ in 0..tasks {
            let mut best: Option<(f64, Vec<u8>)> = None;
            for _ in 0..CANDIDATES {
                let candidate = random_row(&mut rng);
                if row_hashes.contains(&row_hash(&candidate)) { continue; }
                let candidate_score = score(&candidate, &local, &global, target_local, target_global);
                if best.as_ref().map(|(s, _)| candidate_score < *s).unwrap_or(true) { best = Some((candidate_score, candidate)); }
            }
            let row = best.expect("candidate row").1;
            row.iter().enumerate().for_each(|(i, v)| { local[i] += *v as i32; });
            row_hashes.insert(row_hash(&row));
            block.push(row);
        }
        block.shuffle(&mut rng);
        let block_key = block.iter().map(|r| row_hash(r)).collect::<Vec<_>>().join("|");
        if block_hashes.contains(&block_key) { panic!("duplicate block"); }
        block_hashes.insert(block_key);
        block.iter().for_each(|row| row.iter().enumerate().for_each(|(i, v)| { global[i] += *v as i32; }));
        blocks.push(block);
    }
    (respondents, tasks, blocks)
}

fn main() {
    let mut samples = Vec::new();
    let mut output = None;
    for _ in 0..3 { let started = Instant::now(); output = Some(generate(10, 123)); samples.push(started.elapsed().as_secs_f64()); }
    let (respondents, tasks, blocks) = output.unwrap();
    let best = samples.iter().copied().fold(f64::INFINITY, f64::min);
    let average = samples.iter().sum::<f64>() / samples.len() as f64;
    println!("{}", json!({"implementation":"rust-golden-matrix-prototype","requested_respondents":10,"generated_respondents":respondents,"tasks_per_respondent":tasks,"capacity":respondents*tasks,"matrix_shape":[respondents*tasks,CATEGORIES.iter().sum::<usize>()],"samples_seconds":samples,"best_seconds":best,"average_seconds":average,"same_output_contract":true,"same_random_matrix":false,"blocks_generated":blocks.len()}));
}
