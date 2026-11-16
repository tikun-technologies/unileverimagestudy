#[path = "../numpy_compat.rs"] mod numpy_compat;
#[path = "../openblas_compat.rs"] mod openblas_compat;

use numpy_compat::Pcg64;
use serde::Deserialize;
use serde_json::{json, Value};
use std::{collections::HashSet, env, fs, io::{self, Read}};

const CANDIDATES: usize = 100;
const POLISH_PASSES: usize = 3;
const BALANCE_TOL: f64 = 0.05;
const VIF_ROW_WEIGHT: f64 = 0.25;

#[derive(Clone, Deserialize)] struct Config {
    n_respondents: usize, #[serde(default)] tasks_per_respondent: usize,
    elements_per_category: Vec<usize>, min_actives_per_row: usize, max_actives_per_row: usize,
    #[serde(default)] element_priors: Option<Vec<f64>>, #[serde(default)] incompatible_pairs: Vec<[usize;2]>,
    #[serde(default="wg")] w_global: f64, #[serde(default="wl")] w_local: f64,
    #[serde(default="yes")] polish: bool, #[serde(default="attempts")] max_attempts: usize,
    #[serde(default="retries")] max_block_retries: usize, #[serde(default="maxvif")] max_vif: f64,
    #[serde(default)] seed: i64,
    /// When true, n_respondents is the requested panel size.
    /// One respondent stays one. Every larger request becomes min(n * 2, respondent_cap).
    #[serde(default)] expand_respondents: bool,
    #[serde(default = "respondent_cap")] respondent_cap: usize,
}
fn respondent_cap()->usize{1500}
fn generated_respondent_count(requested: usize, cap: usize)->usize{
    if requested<1 {0} else if requested==1 {1} else {requested.saturating_mul(2).min(cap)}
}
fn wg()->f64{0.1} fn wl()->f64{0.9} fn yes()->bool{true} fn attempts()->usize{3} fn retries()->usize{1000} fn maxvif()->f64{10.0}
impl Config {
    fn p(&self)->usize{self.elements_per_category.iter().sum()}
    fn t(&self)->usize{if self.tasks_per_respondent==0{(3*self.p()+1)/2}else{self.tasks_per_respondent}}
    fn avg(&self)->f64{(self.min_actives_per_row+self.max_actives_per_row)as f64/2.0}
    fn validate(&self)->Result<(),String>{let p=self.p();let c=self.elements_per_category.len();if self.n_respondents==0||c==0||self.elements_per_category.iter().any(|x|*x==0){return Err("positive respondents/categories/elements required".into())}if self.min_actives_per_row==0||self.min_actives_per_row>self.max_actives_per_row||self.max_actives_per_row>c{return Err("invalid active range".into())}if self.t()<2||self.t()<p{return Err(format!("T={} must be >= P={} and >=2",self.t(),p))}if let Some(w)=&self.element_priors{if w.len()!=p||w.iter().any(|x|*x<=0.0||!x.is_finite()){return Err("invalid priors".into())}}if self.incompatible_pairs.iter().any(|[a,b]|*a>=p||*b>=p){return Err("invalid incompatible pair".into())}if self.max_attempts==0||self.max_block_retries==0||self.max_vif<=0.0{return Err("invalid retry/VIF settings".into())}Ok(())}
    fn weights(&self)->Vec<f64>{let mut w=self.element_priors.clone().unwrap_or_else(||vec![1.0;self.p()]);let m=w.iter().sum::<f64>()/w.len() as f64;for x in &mut w{*x/=m;}w}
}

fn pairwise_sum(v:&[f64])->f64{if v.len()<8{return v.iter().fold(0.0,|a,b|a+b)}let full=v.len()-v.len()%8;let mut r=[0.0;8];for c in v[..full].chunks_exact(8){for i in 0..8{r[i]+=c[i]}}r[0]+=r[1];r[2]+=r[3];r[4]+=r[5];r[6]+=r[7];r[0]+=r[2];r[4]+=r[6];r[0]+=r[4];for x in &v[full..]{r[0]+=x}r[0]}
fn starts(c:&Config)->Vec<usize>{let mut s=Vec::new();let mut x=0;for n in &c.elements_per_category{s.push(x);x+=n}s}
fn row(c:&Config,rng:&mut Pcg64)->Vec<u8>{let p=c.p();let k=c.min_actives_per_row+rng.bounded_u64_inclusive(0,(c.max_actives_per_row-c.min_actives_per_row)as u64)as usize;let cats=rng.choice_without_replacement(c.elements_per_category.len(),k);let s=starts(c);let mut r=vec![0;p];for cat in cats{let j=rng.bounded_u64_inclusive(0,(c.elements_per_category[cat]-1)as u64)as usize;r[s[cat]+j]=1}r}
fn allowed(c:&Config,r:&[u8])->bool{c.incompatible_pairs.iter().all(|[a,b]|r[*a]==0||r[*b]==0)}
fn vif(rows:&[Vec<u8>],p:usize,b:&openblas_compat::OpenBlas)->f64{if rows.len()<p{return f64::INFINITY}let n=rows.len();let means:Vec<f64>=(0..p).map(|j|{let v:Vec<f64>=rows.iter().map(|r|r[j]as f64).collect();pairwise_sum(&v)/n as f64}).collect();let centered:Vec<f64>=rows.iter().flat_map(|r|r.iter().enumerate().map(|(j,x)|*x as f64-means[j]).collect::<Vec<_>>()).collect();let product=b.covariance_product_col_major(&centered,n,p);let f=1.0/(n-1)as f64;let cov:Vec<Vec<f64>>=(0..p).map(|i|(0..p).map(|j|product[i*p+j]*f).collect()).collect();let var:Vec<f64>=(0..p).map(|i|cov[i][i]).collect();if var.iter().any(|x|*x<=0.0){return f64::INFINITY}let mut corr=cov;for i in 0..p{for j in 0..p{corr[i][j]/=var[i].sqrt()}}for i in 0..p{for j in 0..p{corr[i][j]/=var[j].sqrt();corr[i][j]=corr[i][j].clamp(-1.0,1.0)}}b.inverse_col_major(&corr).map(|m|(0..p).map(|i|m[i][i]).fold(f64::NEG_INFINITY,f64::max)).unwrap_or(f64::INFINITY)}
fn deff(rows:&[Vec<u8>],p:usize,b:&openblas_compat::OpenBlas)->f64{if rows.is_empty(){return 0.0}let flat:Vec<f64>=rows.iter().flatten().map(|x|*x as f64).collect();match b.slogdet_cross_product(&flat,rows.len(),p){Ok((s,l))if s>0.0=>(l/p as f64).exp()/rows.len()as f64,_=>0.0}}
fn balance(rows:&[Vec<u8>],target:&[f64])->f64{let v:Vec<f64>=(0..target.len()).map(|j|(rows.iter().map(|r|r[j]as f64).sum::<f64>()-target[j]).abs()).collect();pairwise_sum(&v)}
fn block(c:&Config,rng:&mut Pcg64,global:&[i32],local_target:&[f64],global_target:&[f64],skip:bool,b:&openblas_compat::OpenBlas)->Result<Vec<Vec<u8>>,String>{let p=c.p();let mut rows=Vec::new();let mut local=vec![0i32;p];for _ in 0..c.t(){let mut best:Option<(f64,Vec<u8>)>=None;for _ in 0..CANDIDATES{let candidate=row(c,rng);if rows.iter().any(|r|*r==candidate)||!allowed(c,&candidate){continue}let gv:Vec<f64>=(0..p).map(|j|((global[j]+candidate[j]as i32)as f64-global_target[j]).abs()).collect();let lv:Vec<f64>=(0..p).map(|j|((local[j]+candidate[j]as i32)as f64-local_target[j]).abs()).collect();let mut score=c.w_global*pairwise_sum(&gv)+c.w_local*pairwise_sum(&lv);if !skip&&rows.len()+1>=p{let mut trial=rows.clone();trial.push(candidate.clone());let v=vif(&trial,p,b);score+=VIF_ROW_WEIGHT*if v.is_finite(){v}else{1e6}}if best.as_ref().map_or(true,|x|score<x.0){best=Some((score,candidate))}}let r=best.ok_or("No valid candidate rows".to_string())?.1;for j in 0..p{local[j]+=r[j]as i32}rows.push(r)}if c.polish{let target=local_target;let mut d=deff(&rows,p,b);let mut bal=balance(&rows,target);for _ in 0..POLISH_PASSES{let mut changed=false;for i in 0..rows.len(){let old=rows[i].clone();let others:HashSet<Vec<u8>>=rows.iter().enumerate().filter(|(j,_)|*j!=i).map(|(_,r)|r.clone()).collect();let(mut best,mut bd)=(old.clone(),d);for _ in 0..CANDIDATES{let cand=row(c,rng);if others.contains(&cand)||!allowed(c,&cand){continue}rows[i]=cand.clone();let nd=deff(&rows,p,b);if nd>bd{let nb=balance(&rows,target);if(nb-bal)/bal.max(1.0)<=BALANCE_TOL{best=cand;bd=nd}}rows[i]=old.clone()}if best!=old{rows[i]=best;d=bd;bal=balance(&rows,target);changed=true}}if !changed{break}}}rng.shuffle_slice(&mut rows);Ok(rows)}
fn preflight(c:&Config,b:&openblas_compat::OpenBlas)->Result<(bool,f64),String>{
    // Exact translation of Python _count_row_variety: for each active-category
    // count, sum the product of the selected category sizes.
    fn variety(c:&Config, at:usize, need:usize, product:u64)->u64{
        if need==0{return product}
        if at>=c.elements_per_category.len(){return 0}
        let mut total=0u64;
        for cat in at..=c.elements_per_category.len()-need{
            total=total.saturating_add(variety(c,cat+1,need-1,product.saturating_mul(c.elements_per_category[cat] as u64)));
        }
        total
    }
    let row_variety=(c.min_actives_per_row..=c.max_actives_per_row).map(|k|variety(c,0,k,1)).sum::<u64>();
    let p=c.p();let mut rng=Pcg64::from_seed_sequence(0);let mut rows=Vec::new();
    for _ in 0..5000{if rows.len()==500{break}let r=row(c,&mut rng);if !rows.contains(&r){rows.push(r)}}
    let sv=if rows.len()<p{f64::INFINITY}else{vif(&rows,p,b)};
    Ok((row_variety>=(2*c.t())as u64&&sv.is_finite()&&sv<=c.max_vif,sv))
}
fn finite_json(value: f64) -> Value { if value.is_finite() { json!(value) } else { Value::Null } }

fn main() {
    let trace_enabled = env::var("GOLDEN_TRACE").ok().as_deref() == Some("1");
    let input = match env::args().nth(1) {
        Some(path) => fs::read_to_string(&path).unwrap_or_else(|error| { eprintln!("Failed to read {path}: {error}"); std::process::exit(2) }),
        None => { let mut value=String::new(); io::stdin().read_to_string(&mut value).unwrap_or_else(|error| { eprintln!("Failed to read stdin: {error}"); std::process::exit(2) }); value }
    };
    let mut c:Config=serde_json::from_str(&input).unwrap_or_else(|error| { eprintln!("Invalid configuration JSON: {error}"); std::process::exit(2) });
    if c.expand_respondents { c.n_respondents = generated_respondent_count(c.n_respondents, c.respondent_cap); }
    if c.tasks_per_respondent==0 { c.tasks_per_respondent=(3*c.p()+1)/2; }
    c.validate().unwrap_or_else(|error| { eprintln!("Invalid configuration: {error}"); std::process::exit(2) });
    let path=env::var("NUMPY_OPENBLAS_LIB")
        .or_else(|_| env::var("NUMPY_OPENBLAS_DLL"))
        .unwrap_or_else(|_| { eprintln!("NUMPY_OPENBLAS_LIB is required"); std::process::exit(2) });
    let b=unsafe{openblas_compat::OpenBlas::load(&path)}.unwrap_or_else(|error| { eprintln!("OpenBLAS load failed: {error}"); std::process::exit(2) });
    let(pffeasible,sv)=preflight(&c,&b).unwrap_or_else(|error| { eprintln!("Preflight calculation failed: {error}"); std::process::exit(2) });
    if !pffeasible { if trace_enabled { println!("{}",json!({"status":"PREFLIGHT_FAIL","structural_vif":finite_json(sv)})); } eprintln!("Preflight failed"); std::process::exit(1) }

    let p=c.p(); let w=c.weights();
    let local_scalar=c.t()as f64*c.avg()/p as f64;
    let global_scalar=(c.n_respondents*c.t())as f64*c.avg()/p as f64;
    let local_target:Vec<f64>=w.iter().map(|x|local_scalar*x).collect();
    let global_target:Vec<f64>=w.iter().map(|x|global_scalar*x).collect();
    let skip=sv.is_finite()&&sv<c.max_vif*0.5;
    let mut max_vif=c.max_vif; let mut last=String::new(); let mut attempt_traces=Vec::new();

    for attempt in 0..c.max_attempts {
        let threshold_before=max_vif;
        let seed=c.seed.wrapping_mul(1_000_003).wrapping_add(attempt as i64+42)as u64;
        eprintln!("Golden Matrix: attempt {}/{}", attempt + 1, c.max_attempts);
        let _ = std::io::Write::flush(&mut std::io::stderr());
        let mut rng=Pcg64::from_seed_sequence(seed as u128);
        let mut matrix=Vec::new(); let mut global=vec![0i32;p]; let mut seen=HashSet::new();
        let mut failed=false; let mut respondent_traces=Vec::new();
        for respondent in 0..c.n_respondents {
            let mut accepted=false; let mut retry_traces=Vec::new();
            for retry in 0..c.max_block_retries {
                let candidate=match block(&c,&mut rng,&global,&local_target,&global_target,skip,&b) {
                    Ok(value)=>value,
                    Err(error)=>{last=error.clone(); if trace_enabled { retry_traces.push(json!({"retry":retry+1,"outcome":"block_error","reason":error})); } continue}
                };
                let block_rank=match b.binary_rank(&candidate,p) {
                    Ok(value)=>value,
                    Err(error)=>{ eprintln!("Rank calculation failed: {error}"); std::process::exit(2) }
                };
                if block_rank<p {
                    last=format!("Respondent block rank {block_rank} < {p}");
                    if trace_enabled { retry_traces.push(json!({"retry":retry+1,"outcome":"rank_rejection","rank":block_rank,"block":candidate})); }
                    continue
                }
                if seen.contains(&candidate) { if trace_enabled { retry_traces.push(json!({"retry":retry+1,"outcome":"duplicate_block","block":candidate})); } continue }
                if !skip {
                    let mut trial=matrix.clone(); trial.extend(candidate.clone()); let trial_vif=vif(&trial,p,&b);
                    if trial_vif>max_vif { if trace_enabled { retry_traces.push(json!({"retry":retry+1,"outcome":"global_vif_rejection","vif":finite_json(trial_vif),"threshold":max_vif,"block":candidate})); } continue }
                }
                for row in &candidate { for j in 0..p { global[j]+=row[j]as i32 } }
                seen.insert(candidate.clone()); matrix.extend(candidate.clone()); accepted=true;
                let done=respondent+1;
                eprintln!("Golden Matrix: respondent {done}/{} done, tasks {}", c.n_respondents, done*c.t());
                let _ = std::io::Write::flush(&mut std::io::stderr());
                if trace_enabled { retry_traces.push(json!({"retry":retry+1,"outcome":"accepted","block":candidate,"global_exposures":global})); }
                break
            }
            if trace_enabled { respondent_traces.push(json!({"respondent":respondent+1,"accepted":accepted,"retries":retry_traces})); }
            if !accepted { last=format!("Block construction failed for respondent {}",respondent+1); failed=true; break }
        }
        if failed {
            if trace_enabled { attempt_traces.push(json!({"attempt":attempt+1,"seed":seed,"max_vif_before":threshold_before,"outcome":"block_construction_failure","reason":last,"respondents":respondent_traces})); }
            continue
        }

        let mut ranks=Vec::with_capacity(c.n_respondents); let mut rank_error=None;
        for respondent in 0..c.n_respondents {
            match b.binary_rank(&matrix[respondent*c.t()..(respondent+1)*c.t()],p) { Ok(value)=>ranks.push(value), Err(error)=>{rank_error=Some(error);break} }
        }
        if let Some(error)=rank_error { eprintln!("Rank calculation failed: {error}"); std::process::exit(2) }
        let rank_ok=ranks.iter().all(|rank|*rank>=p);
        let mut cols=HashSet::new(); let mut valid_cols=true;
        for j in 0..p { let col:Vec<u8>=matrix.iter().map(|r|r[j]).collect(); let sum:usize=col.iter().map(|x|*x as usize).sum(); if !cols.insert(col)||sum==0||sum==matrix.len(){valid_cols=false} }
        let final_vif=vif(&matrix,p,&b);
        if !rank_ok { last="Individual Solvability Failed".into(); if trace_enabled { attempt_traces.push(json!({"attempt":attempt+1,"seed":seed,"max_vif_before":threshold_before,"outcome":"rank_failure","ranks":ranks,"respondents":respondent_traces})); } continue }
        if !valid_cols { last="Duplicate/dead/fixed column".into(); if trace_enabled { attempt_traces.push(json!({"attempt":attempt+1,"seed":seed,"max_vif_before":threshold_before,"outcome":"column_validation_failure","respondents":respondent_traces})); } continue }
        if final_vif>max_vif {
            max_vif=(max_vif+5.0).min(30.0); last=format!("Global Max VIF {final_vif}");
            if trace_enabled { attempt_traces.push(json!({"attempt":attempt+1,"seed":seed,"max_vif_before":threshold_before,"max_vif_after":max_vif,"outcome":"global_vif_failure","global_vif":finite_json(final_vif),"respondents":respondent_traces})); }
            continue
        }
        if trace_enabled { attempt_traces.push(json!({"attempt":attempt+1,"seed":seed,"max_vif_before":threshold_before,"max_vif_after":max_vif,"outcome":"accepted","global_vif":finite_json(final_vif),"ranks":ranks,"respondents":respondent_traces})); }
        eprintln!("Golden Matrix: generated {} respondents, {} tasks", c.n_respondents, matrix.len());
        let _ = std::io::Write::flush(&mut std::io::stderr());
        println!("{}",serde_json::to_string(&json!({"matrix":matrix,"attempt":attempt+1,"global_vif":final_vif,"skip_vif":skip,"structural_vif":sv,"status":"PASS","trace":if trace_enabled{json!(attempt_traces)}else{Value::Null}})).unwrap()); return
    }
    if trace_enabled { println!("{}",serde_json::to_string(&json!({"status":"FAIL","attempts":c.max_attempts,"reason":last,"skip_vif":skip,"structural_vif":finite_json(sv),"trace":attempt_traces})).unwrap()); }
    eprintln!("Could not generate valid matrix after {} attempts: {}",c.max_attempts,last); std::process::exit(1)
}
