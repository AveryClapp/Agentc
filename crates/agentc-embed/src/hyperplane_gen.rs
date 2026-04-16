//! Deterministic hyperplane generator.
//!
//! Produces the exact bytes that live in `data/hyperplanes.f32`. Used by the
//! regen example (`examples/regen_hyperplanes.rs`) and by
//! `tests/hyperplane_stability.rs` to enforce reproducibility across releases.
//!
//! The generator uses SplitMix64 for PRNG output and Box–Muller to map uniform
//! samples to standard normals. Each hyperplane is 256 standard-normal samples,
//! L2-normalized to a unit vector. This is the canonical construction for
//! hyperplane LSH over cosine similarity.

use crate::lsh::{HYPERPLANE_BYTES_LEN, NUM_HYPERPLANES};
use crate::model::EMBEDDING_DIM;

/// Seed for hyperplane generation. Do not change: existing caches depend on
/// the resulting signature space being stable.
pub const HYPERPLANE_SEED: u64 = 0x8F7E_6D5C_4B3A_2910;

/// Generate the canonical hyperplane byte matrix. Output length is exactly
/// `HYPERPLANE_BYTES_LEN` (65,536 bytes = 64 × 256 × f32 LE).
pub fn generate_hyperplane_bytes() -> Vec<u8> {
    let matrix = generate_hyperplane_matrix();
    let mut out = Vec::with_capacity(HYPERPLANE_BYTES_LEN);
    for v in &matrix {
        out.extend_from_slice(&v.to_le_bytes());
    }
    out
}

fn generate_hyperplane_matrix() -> Vec<f32> {
    let mut state = HYPERPLANE_SEED;
    let mut out = Vec::with_capacity(NUM_HYPERPLANES * EMBEDDING_DIM);
    for _ in 0..NUM_HYPERPLANES {
        // Draw 256 standard-normal samples.
        let mut plane: Vec<f32> = Vec::with_capacity(EMBEDDING_DIM);
        while plane.len() < EMBEDDING_DIM {
            let (z0, z1) = box_muller_pair(&mut state);
            plane.push(z0);
            if plane.len() < EMBEDDING_DIM {
                plane.push(z1);
            }
        }
        // Normalize to unit length.
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

fn splitmix64(state: &mut u64) -> u64 {
    *state = state.wrapping_add(0x9E37_79B9_7F4A_7C15);
    let mut z = *state;
    z = (z ^ (z >> 30)).wrapping_mul(0xBF58_476D_1CE4_E5B9);
    z = (z ^ (z >> 27)).wrapping_mul(0x94D0_49BB_1331_11EB);
    z ^ (z >> 31)
}

/// Uniform sample in the open interval (0, 1). The `(0.0)` endpoint is excluded
/// so `ln()` in Box–Muller never blows up.
fn uniform_open01(state: &mut u64) -> f64 {
    loop {
        let u = splitmix64(state);
        // Use 53 bits for f64 precision.
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

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn generator_output_has_expected_size() {
        let bytes = generate_hyperplane_bytes();
        assert_eq!(bytes.len(), HYPERPLANE_BYTES_LEN);
    }

    #[test]
    fn generator_is_deterministic() {
        let a = generate_hyperplane_bytes();
        let b = generate_hyperplane_bytes();
        assert_eq!(a, b, "generator output is not deterministic");
    }

    #[test]
    fn generated_hyperplanes_are_unit_length() {
        let matrix = generate_hyperplane_matrix();
        for i in 0..NUM_HYPERPLANES {
            let plane = &matrix[i * EMBEDDING_DIM..(i + 1) * EMBEDDING_DIM];
            let norm: f32 = plane.iter().map(|x| x * x).sum::<f32>().sqrt();
            assert!(
                (norm - 1.0).abs() < 1e-5,
                "hyperplane {i} not unit-length: {norm}"
            );
        }
    }

    #[test]
    fn splitmix64_well_known_output() {
        // Regression: fix the output sequence so we notice if the PRNG changes.
        let mut state = 0u64;
        let v1 = splitmix64(&mut state);
        let v2 = splitmix64(&mut state);
        assert_eq!(v1, 0xE220_A839_7B1D_CDAF);
        assert_eq!(v2, 0x6E78_9E6A_A1B9_65F4);
    }
}
