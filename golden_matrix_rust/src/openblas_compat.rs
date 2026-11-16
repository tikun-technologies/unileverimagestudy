//! Small cross-platform FFI used by the numerical parity audit.
//!
//! The Python reference environment contains an ILP64 OpenBLAS library. This
//! module loads the exact library path supplied by the Python caller so the
//! normal Rust generator does not acquire a compile-time dependency on Python.

use std::ffi::c_void;

type CblasDsyrk = unsafe extern "C" fn(
    i32, i32, i32, i64, i64, f64, *const f64, i64, f64, *mut f64, i64,
);

type Dgesv = unsafe extern "C" fn(*const i64, *const i64, *mut f64, *const i64, *mut i64, *mut f64, *const i64, *mut i64);
type Dgetrf = unsafe extern "C" fn(*const i64, *const i64, *mut f64, *const i64, *mut i64, *mut i64);
type Dgesdd = unsafe extern "C" fn(*const u8, *const i64, *const i64, *mut f64, *const i64, *mut f64, *mut f64, *const i64, *mut f64, *const i64, *mut f64, *const i64, *mut i64, *mut i64);

#[cfg(windows)]
extern "system" {
    fn LoadLibraryW(name: *const u16) -> *mut c_void;
    fn GetProcAddress(handle: *mut c_void, name: *const u8) -> *const c_void;
}

#[cfg(unix)]
#[link(name = "dl")]
extern "C" {
    fn dlopen(filename: *const i8, flags: i32) -> *mut c_void;
    fn dlsym(handle: *mut c_void, symbol: *const i8) -> *mut c_void;
}

#[cfg(unix)]
const RTLD_NOW: i32 = 2;

pub struct OpenBlas {
    _handle: *mut c_void,
    dsyrk: CblasDsyrk,
    dgesv: Dgesv,
    dgetrf: Dgetrf,
    dgesdd: Dgesdd,
}

unsafe impl Send for OpenBlas {}
unsafe impl Sync for OpenBlas {}

impl OpenBlas {
    pub unsafe fn load(path: &str) -> Result<Self, String> {
        #[cfg(windows)]
        {
            let wide: Vec<u16> = path.encode_utf16().chain(std::iter::once(0)).collect();
            let handle = LoadLibraryW(wide.as_ptr());
            if handle.is_null() { return Err(format!("LoadLibraryW failed for {path}")); }
            let symbol = |name: &[u8]| -> Result<*const c_void, String> {
                let mut nul_name = name.to_vec();
                nul_name.push(0);
                let address = GetProcAddress(handle, nul_name.as_ptr());
                if address.is_null() { Err(format!("missing OpenBLAS symbol {}", String::from_utf8_lossy(name))) }
                else { Ok(address) }
            };
            Ok(Self {
                _handle: handle,
                dsyrk: std::mem::transmute(symbol(b"scipy_cblas_dsyrk64_")?),
                dgesv: std::mem::transmute(symbol(b"scipy_dgesv_64_")?),
                dgetrf: std::mem::transmute(symbol(b"scipy_dgetrf_64_")?),
                dgesdd: std::mem::transmute(symbol(b"scipy_dgesdd_64_")?),
            })
        }
        #[cfg(unix)]
        {
            use std::ffi::CString;

            let path_c = CString::new(path).map_err(|_| format!("invalid OpenBLAS path: {path}"))?;
            let handle = dlopen(path_c.as_ptr(), RTLD_NOW);
            if handle.is_null() {
                return Err(format!("dlopen failed for {path}"));
            }
            let symbol = |name: &[u8]| -> Result<*const c_void, String> {
                let name_c = CString::new(name).map_err(|_| "invalid OpenBLAS symbol".to_string())?;
                let address = dlsym(handle, name_c.as_ptr());
                if address.is_null() {
                    Err(format!("missing OpenBLAS symbol {}", String::from_utf8_lossy(name)))
                } else {
                    Ok(address as *const c_void)
                }
            };
            Ok(Self {
                _handle: handle,
                dsyrk: std::mem::transmute(symbol(b"scipy_cblas_dsyrk64_")?),
                dgesv: std::mem::transmute(symbol(b"scipy_dgesv_64_")?),
                dgetrf: std::mem::transmute(symbol(b"scipy_dgetrf_64_")?),
                dgesdd: std::mem::transmute(symbol(b"scipy_dgesdd_64_")?),
            })
        }
    }

    /// NumPy's matrix_rank uses LAPACK DGESDD with no singular vectors and the default tolerance
    /// max(M,N) * eps * largest_singular_value. Use the same LAPACK
    /// implementation as the pinned NumPy reference rather than a
    /// pivot-threshold Gaussian rank test.
    pub fn binary_rank(&self, rows: &[Vec<u8>], p: usize) -> Result<usize, String> {
        let m = rows.len();
        if m == 0 || p == 0 { return Ok(0); }
        let min_dim = m.min(p);
        let mut a = vec![0.0; m * p];
        for r in 0..m { for c in 0..p { a[c * m + r] = rows[r][c] as f64; } }
        let mut s = vec![0.0; min_dim];
        let mut u = vec![0.0; 1];
        let mut vt = vec![0.0; 1];
        let job = b'N';
        let m64 = m as i64; let p64 = p as i64;
        let lda = m64; let ldu = 1i64; let ldvt = 1i64;
        let mut iwork = vec![0i64; 8 * min_dim];
        let mut work_query = vec![0.0; 1]; let lwork_query = -1i64; let mut info = 0i64;
        unsafe { (self.dgesdd)(&job, &m64, &p64, a.as_mut_ptr(), &lda, s.as_mut_ptr(), u.as_mut_ptr(), &ldu, vt.as_mut_ptr(), &ldvt, work_query.as_mut_ptr(), &lwork_query, iwork.as_mut_ptr(), &mut info); }
        if info != 0 { return Err(format!("dgesdd workspace query failed: {info}")); }
        let lwork = (work_query[0].ceil() as usize).max(1);
        let mut work = vec![0.0; lwork]; let lwork64 = lwork as i64;
        unsafe { (self.dgesdd)(&job, &m64, &p64, a.as_mut_ptr(), &lda, s.as_mut_ptr(), u.as_mut_ptr(), &ldu, vt.as_mut_ptr(), &ldvt, work.as_mut_ptr(), &lwork64, iwork.as_mut_ptr(), &mut info); }
        if info != 0 { return Err(format!("dgesdd failed: {info}")); }
        let tol = (m.max(p) as f64) * f64::EPSILON * s[0];
        Ok(s.into_iter().filter(|value| *value > tol).count())
    }

    /// Computes X^T*X where `x` is a C-order n-by-p matrix. NumPy recognizes
    /// the symmetric self-product used by `cov(..., rowvar=False)` and calls
    /// the upper-triangle DSYRK path. DGEMM is mathematically equivalent but
    /// has a different reduction order and changes final VIF bits.
    pub fn covariance_product_col_major(&self, x: &[f64], n: usize, p: usize) -> Vec<f64> {
        let mut row_major_output = vec![0.0; p * p];
        unsafe {
            (self.dsyrk)(101, 121, 112, p as i64, n as i64,
                1.0, x.as_ptr(), p as i64, 0.0,
                row_major_output.as_mut_ptr(), p as i64);
        }
        for row in 0..p { for col in 0..row { row_major_output[row * p + col] = row_major_output[col * p + row]; } }
        row_major_output
    }

    pub fn inverse_col_major(&self, row_major: &[Vec<f64>]) -> Result<Vec<Vec<f64>>, i64> {
        let n = row_major.len();
        let mut matrix = vec![0.0; n * n];
        for row in 0..n { for col in 0..n { matrix[col * n + row] = row_major[row][col]; } }
        let mut rhs = vec![0.0; n * n];
        for i in 0..n { rhs[i * n + i] = 1.0; }
        let n64 = n as i64;
        let mut pivots = vec![0i64; n];
        let mut info = 0i64;
        unsafe { (self.dgesv)(&n64, &n64, matrix.as_mut_ptr(), &n64, pivots.as_mut_ptr(), rhs.as_mut_ptr(), &n64, &mut info); }
        if info != 0 { return Err(info); }
        let mut result = vec![vec![0.0; n]; n];
        for row in 0..n { for col in 0..n { result[row][col] = rhs[col * n + row]; } }
        Ok(result)
    }

    pub fn slogdet_cross_product(&self, x: &[f64], n: usize, p: usize) -> Result<(f64, f64), i64> {
        let mut matrix = self.covariance_product_col_major(x, n, p);
        let n64 = p as i64;
        let mut pivots = vec![0i64; p];
        let mut info = 0i64;
        unsafe { (self.dgetrf)(&n64, &n64, matrix.as_mut_ptr(), &n64, pivots.as_mut_ptr(), &mut info); }
        if info != 0 { return Err(info); }
        let mut sign = 1.0;
        for i in 0..p {
            if pivots[i] != i as i64 + 1 { sign = -sign; }
            let diagonal = matrix[i * p + i];
            if diagonal == 0.0 { return Ok((0.0, f64::NEG_INFINITY)); }
            sign *= diagonal.signum();
        }
        let logdet = (0..p).map(|i| matrix[i * p + i].abs().ln()).sum();
        Ok((sign, logdet))
    }
}

