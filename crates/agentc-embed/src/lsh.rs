//! Hyperplane LSH over cosine similarity.
//!
//! 64 hyperplanes drawn uniformly from the unit sphere in 256-dim space. Each
//! embedding produces a 64-bit signature (`sign(e · h_i)` packed into a `u64`).
//! The signature is partitioned into 8 bands of 8 bits each for multi-probe
//! candidate lookup.
//!
//! Hyperplane bytes live in `data/hyperplanes.f32` and are `include_bytes!`-ed
//! at compile time. The bytes are regenerated deterministically by
//! [`hyperplane_gen::generate_hyperplane_bytes`]; see
//! `tests/hyperplane_stability.rs` for the round-trip check.

use once_cell::sync::Lazy;

use crate::model::EMBEDDING_DIM;

/// Number of hyperplanes packed into a single LSH signature. Matches the bit
/// width of a `u64` and keeps signature math branch-free.
pub const NUM_HYPERPLANES: usize = 64;

/// Number of bands per signature. Spec requires 8×8 banding for the default
/// similarity threshold of 0.92.
pub const NUM_BANDS: usize = 8;

/// Rows (bits) per band.
pub const ROWS_PER_BAND: usize = 8;

const _: () = {
    assert!(NUM_BANDS * ROWS_PER_BAND == NUM_HYPERPLANES);
};

/// Expected length in bytes of `data/hyperplanes.f32` (64 × 256 × 4 = 65,536).
pub const HYPERPLANE_BYTES_LEN: usize = NUM_HYPERPLANES * EMBEDDING_DIM * 4;

const HYPERPLANE_BYTES: &[u8] = include_bytes!("../data/hyperplanes.f32");

static HYPERPLANES: Lazy<[f32; NUM_HYPERPLANES * EMBEDDING_DIM]> = Lazy::new(|| {
    assert_eq!(
        HYPERPLANE_BYTES.len(),
        HYPERPLANE_BYTES_LEN,
        "hyperplanes.f32 length mismatch: expected {HYPERPLANE_BYTES_LEN}, got {}",
        HYPERPLANE_BYTES.len()
    );
    let mut arr = [0f32; NUM_HYPERPLANES * EMBEDDING_DIM];
    for (i, chunk) in HYPERPLANE_BYTES.chunks_exact(4).enumerate() {
        arr[i] = f32::from_le_bytes([chunk[0], chunk[1], chunk[2], chunk[3]]);
    }
    arr
});

/// Returns the bundled hyperplane matrix as a flat array of
/// `NUM_HYPERPLANES * EMBEDDING_DIM` f32 values in row-major order. Hyperplane
/// `i` occupies indices `[i * EMBEDDING_DIM, (i + 1) * EMBEDDING_DIM)`.
pub fn hyperplanes() -> &'static [f32; NUM_HYPERPLANES * EMBEDDING_DIM] {
    &HYPERPLANES
}

/// Compute the 64-bit LSH signature for a 256-dim embedding. Bit `i` is set
/// when the dot product against hyperplane `i` is non-negative.
pub fn lsh_signature(embedding: &[f32]) -> u64 {
    assert_eq!(
        embedding.len(),
        EMBEDDING_DIM,
        "lsh_signature: expected {EMBEDDING_DIM}-dim embedding, got {}",
        embedding.len()
    );
    let planes = hyperplanes();
    let mut sig = 0u64;
    for i in 0..NUM_HYPERPLANES {
        let plane = &planes[i * EMBEDDING_DIM..(i + 1) * EMBEDDING_DIM];
        let dot: f32 = embedding
            .iter()
            .zip(plane.iter())
            .map(|(a, b)| a * b)
            .sum();
        if dot >= 0.0 {
            sig |= 1u64 << i;
        }
    }
    sig
}

/// Split a 64-bit LSH signature into 8 bands of 8 bits each. Output `out[i]`
/// is the SQL `bucket_id` for `band_ix = i`.
pub fn lsh_bands(signature: u64) -> [u8; NUM_BANDS] {
    let mut out = [0u8; NUM_BANDS];
    for i in 0..NUM_BANDS {
        out[i] = ((signature >> (i * ROWS_PER_BAND)) & 0xFF) as u8;
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::hyperplane_gen::generate_hyperplane_bytes;

    #[test]
    fn hyperplane_asset_has_expected_size() {
        assert_eq!(hyperplanes().len(), NUM_HYPERPLANES * EMBEDDING_DIM);
    }

    #[test]
    fn hyperplane_asset_matches_generator() {
        // This is the reproducibility contract: the committed bytes must match
        // what the seeded generator emits.
        let expected = generate_hyperplane_bytes();
        let bytes = HYPERPLANE_BYTES;
        assert_eq!(
            bytes.len(),
            expected.len(),
            "committed hyperplanes.f32 size differs from generator output"
        );
        assert_eq!(
            bytes, expected,
            "committed hyperplanes.f32 bytes differ from generator output. \
             Regenerate with: cargo run -p agentc-embed --example regen_hyperplanes"
        );
    }

    #[test]
    fn each_hyperplane_is_unit_length() {
        let planes = hyperplanes();
        for i in 0..NUM_HYPERPLANES {
            let plane = &planes[i * EMBEDDING_DIM..(i + 1) * EMBEDDING_DIM];
            let norm: f32 = plane.iter().map(|x| x * x).sum::<f32>().sqrt();
            assert!(
                (norm - 1.0).abs() < 1e-4,
                "hyperplane {i} is not unit-length: norm = {norm}"
            );
        }
    }

    #[test]
    fn signature_bit_count_reasonable_on_random_vector() {
        // A vector uncorrelated with the hyperplanes should produce roughly 32 bits
        // set out of 64. We accept 16..=48 to avoid flakes on any single vector.
        let v: Vec<f32> = (0..EMBEDDING_DIM).map(|i| ((i * 17) % 31) as f32).collect();
        let sig = lsh_signature(&v);
        let count = sig.count_ones();
        assert!(
            (16..=48).contains(&count),
            "signature bit count out of expected range: {count}"
        );
    }

    #[test]
    fn lsh_bands_partition_signature() {
        let sig = 0xDEADBEEFCAFEBABEu64;
        let bands = lsh_bands(sig);
        // Reconstruct the signature from the bands to verify the partition.
        let mut reconstructed = 0u64;
        for (i, &b) in bands.iter().enumerate() {
            reconstructed |= (b as u64) << (i * ROWS_PER_BAND);
        }
        assert_eq!(reconstructed, sig);
    }

    #[test]
    fn identical_embeddings_produce_identical_signatures() {
        let v: Vec<f32> = (0..EMBEDDING_DIM).map(|i| (i as f32) * 0.05).collect();
        assert_eq!(lsh_signature(&v), lsh_signature(&v));
    }

    #[test]
    fn opposite_embeddings_produce_inverted_signatures() {
        let v: Vec<f32> = (0..EMBEDDING_DIM).map(|i| (i as f32) * 0.05 - 5.0).collect();
        let neg: Vec<f32> = v.iter().map(|x| -x).collect();
        // sign flips on every plane where the dot product is non-zero; the zero-dot
        // planes stay at bit 1 (non-negative) in both. In practice with random-ish
        // hyperplanes the exact-zero case is vanishingly rare, so the signatures
        // should be strict bitwise complements.
        assert_eq!(lsh_signature(&v), !lsh_signature(&neg));
    }
}
