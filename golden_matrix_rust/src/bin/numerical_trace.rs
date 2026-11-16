#[path = "../openblas_compat.rs"]
mod openblas_compat;

use serde::Deserialize;
use serde_json::json;
use std::{env, fs};

#[derive(Deserialize)]
struct Fixture { rows: Vec<Vec<f64>> }

fn inverse_trace(mut a: Vec<Vec<f64>>) -> (Option<Vec<Vec<f64>>>, Vec<f64>) {
    let n = a.len();
    let mut inv = vec![vec![0.0; n]; n];
    let mut pivots = Vec::new();
    for i in 0..n { inv[i][i] = 1.0; }
    for k in 0..n {
        let pivot = (k..n).max_by(|&i, &j| a[i][k].abs().total_cmp(&a[j][k].abs())).unwrap();
        let magnitude = a[pivot][k].abs();
        pivots.push(magnitude);
        if magnitude < 1e-12 { return (None, pivots); }
        if pivot != k { a.swap(pivot, k); inv.swap(pivot, k); }
        let divisor = a[k][k];
        for j in 0..n { a[k][j] /= divisor; inv[k][j] /= divisor; }
        for i in 0..n {
            if i == k { continue; }
            let factor = a[i][k];
            for j in 0..n { a[i][j] -= factor * a[k][j]; inv[i][j] -= factor * inv[k][j]; }
        }
    }
    (Some(inv), pivots)
}

fn main() {
    let path = env::args().nth(1).expect("fixture path");
    let fixture: Fixture = serde_json::from_str(&fs::read_to_string(path).unwrap()).unwrap();
    let x = fixture.rows;
    let n = x.len();
    let p = x[0].len();
    let means: Vec<f64> = (0..p).map(|j| x.iter().map(|row| row[j]).sum::<f64>() / n as f64).collect();
    let centered: Vec<Vec<f64>> = x.iter().map(|row| row.iter().enumerate().map(|(j, value)| value - means[j]).collect()).collect();
    let covariance: Vec<Vec<f64>> = (0..p).map(|i| (0..p).map(|j| {
        centered.iter().map(|row| row[i] * row[j]).sum::<f64>() / (n - 1) as f64
    }).collect()).collect();
    let variances: Vec<f64> = (0..p).map(|i| covariance[i][i]).collect();
    let correlation: Vec<Vec<f64>> = (0..p).map(|i| (0..p).map(|j| {
        covariance[i][j] / (variances[i] * variances[j]).sqrt()
    }).collect()).collect();
    let (inverse, pivots) = inverse_trace(correlation.clone());
    let inverse_diagonal = inverse.as_ref().map(|matrix| (0..p).map(|i| matrix[i][i]).collect::<Vec<f64>>());
    let vif = inverse_diagonal.as_ref().map(|diag| diag.iter().copied().fold(f64::NEG_INFINITY, f64::max));
    let openblas = env::var("NUMPY_OPENBLAS_DLL").ok().and_then(|path| unsafe { openblas_compat::OpenBlas::load(&path).ok() });
    let (openblas_covariance, openblas_correlation, openblas_inverse, openblas_vif) = if let Some(ref blas) = openblas {
        let flat_centered: Vec<f64> = centered.iter().flatten().copied().collect();
        let product = blas.covariance_product_col_major(&flat_centered, n, p);
        // np.cov multiplies by 1/fact after the dot; it does not divide each
        // result by fact. Preserve that operation ordering.
        let factor = 1.0 / (n - 1) as f64;
        let cov: Vec<Vec<f64>> = (0..p).map(|i| (0..p).map(|j| product[i * p + j] * factor).collect()).collect();
        let var: Vec<f64> = (0..p).map(|i| cov[i][i]).collect();
        // np.corrcoef normalizes in two matrix-wide division passes, rather
        // than computing sqrt(var_i * var_j) per element.
        let mut corr = cov.clone();
        for i in 0..p { for j in 0..p { corr[i][j] /= var[i].sqrt(); } }
        for i in 0..p { for j in 0..p { corr[i][j] /= var[j].sqrt(); } }
        for i in 0..p { for j in 0..p { corr[i][j] = corr[i][j].clamp(-1.0, 1.0); } }
        let inv = blas.inverse_col_major(&corr).ok();
        let v = inv.as_ref().map(|matrix| (0..p).map(|i| matrix[i][i]).fold(f64::NEG_INFINITY, f64::max));
        (Some(cov), Some(corr), inv, v)
    } else { (None, None, None, None) };
    println!("{}", serde_json::to_string(&json!({
        "implementation":"rust-current",
        "shape":[n,p], "input":x, "means":means, "centered":centered,
        "column_variances":variances, "covariance":covariance,
        "correlation":correlation, "pivot_magnitudes":pivots,
        "inverse":inverse, "inverse_diagonal":inverse_diagonal, "vif":vif,
        "openblas_covariance":openblas_covariance, "openblas_correlation":openblas_correlation,
        "openblas_inverse":openblas_inverse, "openblas_vif":openblas_vif
    })).unwrap());
}
