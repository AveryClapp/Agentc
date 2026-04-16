//! Shared embedding + LSH primitives.
//!
//! Owns the `model2vec potion-base-8M` inference path and the bundled LSH
//! hyperplane asset. Consumed by `agentc-core` (for per-span embeddings),
//! `agentc-memo` (for cache lookup), and `agentc-optimizer` (transitively,
//! via the memo cache).

pub mod model;
pub mod lsh;
pub mod hyperplane_gen;

pub use lsh::{hyperplanes, lsh_bands, lsh_signature, NUM_BANDS, NUM_HYPERPLANES, ROWS_PER_BAND};
pub use model::{
    cosine_similarity, embed_text, embed_text_f32, extract_text_for_embedding, f16_bytes_to_f32,
    f32_to_f16_bytes, is_zero_embedding, EMBEDDING_BYTES, EMBEDDING_DIM,
};
