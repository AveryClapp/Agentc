//! Standalone hyperplane generator. Compile with rustc directly, not via Cargo:
//!
//!     rustc crates/agentc-embed/tools/gen_hyperplanes.rs -O \
//!         -o /tmp/gen-hyperplanes && /tmp/gen-hyperplanes
//!
//! Writes `crates/agentc-embed/data/hyperplanes.f32` (65,536 bytes).
//!
//! The math here is mirrored in `src/hyperplane_gen.rs`; the tests in that module
//! verify byte-for-byte equality between the two, so this tool is only used for
//! bootstrap and regeneration — production code path reads the bundled asset.

use std::fs;
use std::io::Write;
use std::path::Path;

const HYPERPLANE_SEED: u64 = 0x8F7E_6D5C_4B3A_2910;
const NUM_HYPERPLANES: usize = 64;
const EMBEDDING_DIM: usize = 256;

fn splitmix64(state: &mut u64) -> u64 {
    *state = state.wrapping_add(0x9E37_79B9_7F4A_7C15);
    let mut z = *state;
    z = (z ^ (z >> 30)).wrapping_mul(0xBF58_476D_1CE4_E5B9);
    z = (z ^ (z >> 27)).wrapping_mul(0x94D0_49BB_1331_11EB);
    z ^ (z >> 31)
}

fn uniform_open01(state: &mut u64) -> f64 {
    loop {
        let u = splitmix64(state);
        let v = (u >> 11) as f64 / (1u64 << 53) as f64;
        if v > 0.0 {
            return v;
        }
    }
}

fn box_muller_pair(state: &mut u64) -> (f32, f32) {
    let u1 = uniform_open01(state);
    let u2 = uniform_open01(state);
    let r = (-2.0 * u1.ln()).sqrt();
    let theta = 2.0 * std::f64::consts::PI * u2;
    ((r * theta.cos()) as f32, (r * theta.sin()) as f32)
}

fn generate() -> Vec<f32> {
    let mut state = HYPERPLANE_SEED;
    let mut out = Vec::with_capacity(NUM_HYPERPLANES * EMBEDDING_DIM);
    for _ in 0..NUM_HYPERPLANES {
        let mut plane: Vec<f32> = Vec::with_capacity(EMBEDDING_DIM);
        while plane.len() < EMBEDDING_DIM {
            let (z0, z1) = box_muller_pair(&mut state);
            plane.push(z0);
            if plane.len() < EMBEDDING_DIM {
                plane.push(z1);
            }
        }
        let norm: f32 = plane.iter().map(|x| x * x).sum::<f32>().sqrt();
        if norm > 0.0 {
            for x in plane.iter_mut() {
                *x /= norm;
            }
        }
        out.extend_from_slice(&plane);
    }
    out
}

fn main() {
    let matrix = generate();
    let mut bytes = Vec::with_capacity(matrix.len() * 4);
    for v in &matrix {
        bytes.extend_from_slice(&v.to_le_bytes());
    }
    let out_path = Path::new("crates/agentc-embed/data/hyperplanes.f32");
    if let Some(parent) = out_path.parent() {
        fs::create_dir_all(parent).expect("create data dir");
    }
    let mut f = fs::File::create(out_path).expect("create hyperplanes.f32");
    f.write_all(&bytes).expect("write hyperplanes.f32");
    eprintln!(
        "wrote {} bytes to {}",
        bytes.len(),
        out_path.display()
    );
}
