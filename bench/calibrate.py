"""Threshold calibration for waste detectors.

Generates synthetic traces with known ground truth (waste vs. not-waste),
runs waste detection at multiple cosine similarity thresholds, and computes
precision/recall curves to find optimal thresholds.

Calibration methodology:
- Generate embedding pairs with known similarity scores
- Label pairs as "true waste" or "not waste" (ground truth)
- Sweep thresholds: 0.80, 0.85, 0.90, 0.95
- Compute precision = TP / (TP + FP), recall = TP / (TP + FN)
- Target: precision >= 0.85 for all detectors
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import Any


# Candidate thresholds to evaluate
CANDIDATE_THRESHOLDS: list[float] = [0.80, 0.85, 0.90, 0.95]


@dataclass
class EmbeddingPair:
    """A pair of embeddings with known ground truth."""

    id_a: str
    id_b: str
    embedding_a: list[float]
    embedding_b: list[float]
    true_similarity: float
    is_waste: bool  # Ground truth label


@dataclass
class ThresholdResult:
    """Precision/recall result at a specific threshold."""

    threshold: float
    true_positives: int
    false_positives: int
    false_negatives: int
    true_negatives: int

    @property
    def precision(self) -> float:
        denom = self.true_positives + self.false_positives
        return self.true_positives / denom if denom > 0 else 1.0

    @property
    def recall(self) -> float:
        denom = self.true_positives + self.false_negatives
        return self.true_positives / denom if denom > 0 else 1.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if (p + r) > 0 else 0.0


@dataclass
class DetectorCalibration:
    """Calibration results for a single detector."""

    detector_name: str
    results: list[ThresholdResult] = field(default_factory=list)
    recommended_threshold: float = 0.90

    def calibrate(self) -> None:
        """Select the lowest threshold that achieves precision >= 0.85."""
        for result in sorted(self.results, key=lambda r: r.threshold):
            if result.precision >= 0.85:
                self.recommended_threshold = result.threshold
                return
        # If no threshold achieves target, use the highest
        if self.results:
            self.recommended_threshold = max(r.threshold for r in self.results)


@dataclass
class CalibrationReport:
    """Full calibration report across all detectors."""

    seed: int
    n_calibration_tasks: int
    detectors: list[DetectorCalibration] = field(default_factory=list)
    passed: bool = False

    def check_all_pass(self) -> bool:
        """Check if all detectors achieve precision >= 0.85 at their recommended threshold."""
        for det in self.detectors:
            best = None
            for r in det.results:
                if abs(r.threshold - det.recommended_threshold) < 1e-6:
                    best = r
                    break
            if best is None or best.precision < 0.85:
                return False
        self.passed = True
        return True


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Compute cosine similarity between two vectors."""
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a < 1e-10 or norm_b < 1e-10:
        return 0.0
    return dot / (norm_a * norm_b)


def generate_embedding(dim: int, rng: random.Random) -> list[float]:
    """Generate a random unit-normalized embedding."""
    raw = [rng.gauss(0, 1) for _ in range(dim)]
    norm = math.sqrt(sum(x * x for x in raw))
    if norm < 1e-10:
        raw[0] = 1.0
        norm = 1.0
    return [x / norm for x in raw]


def generate_similar_embedding(
    base: list[float],
    target_similarity: float,
    rng: random.Random,
) -> list[float]:
    """Generate an embedding with approximately the target cosine similarity to base.

    Uses spherical interpolation (slerp) between the base vector and a random
    orthogonal vector.
    """
    dim = len(base)

    # Generate a random vector
    noise = [rng.gauss(0, 1) for _ in range(dim)]

    # Make it orthogonal to base via Gram-Schmidt
    dot = sum(b * n for b, n in zip(base, noise))
    ortho = [n - dot * b for b, n in zip(base, noise)]
    ortho_norm = math.sqrt(sum(x * x for x in ortho))
    if ortho_norm < 1e-10:
        # Degenerate case: base and noise are parallel, pick another direction
        ortho = [0.0] * dim
        ortho[0 if abs(base[0]) < 0.9 else 1] = 1.0
        dot = sum(b * o for b, o in zip(base, ortho))
        ortho = [o - dot * b for b, o in zip(base, ortho)]
        ortho_norm = math.sqrt(sum(x * x for x in ortho))

    ortho = [x / ortho_norm for x in ortho]

    # Slerp: result = cos(theta) * base + sin(theta) * ortho
    # where cos(theta) = target_similarity
    theta = math.acos(min(1.0, max(-1.0, target_similarity)))
    result = [
        math.cos(theta) * b + math.sin(theta) * o
        for b, o in zip(base, ortho)
    ]

    # Re-normalize
    result_norm = math.sqrt(sum(x * x for x in result))
    if result_norm > 1e-10:
        result = [x / result_norm for x in result]

    return result


def generate_calibration_pairs(
    n_pairs: int = 200,
    dim: int = 256,
    seed: int = 42,
) -> list[EmbeddingPair]:
    """Generate embedding pairs with known ground truth for calibration.

    Generates pairs across the similarity spectrum:
    - True waste pairs: similarity in [0.85, 0.99] — genuinely redundant content
    - Not-waste pairs: similarity in [0.60, 0.89] — similar but different intent

    The overlap zone [0.85, 0.89] is intentional: it tests the detector's
    ability to discriminate near the decision boundary.
    """
    rng = random.Random(seed)
    pairs: list[EmbeddingPair] = []

    # True waste: high similarity (genuinely redundant)
    n_waste = n_pairs // 2
    for i in range(n_waste):
        base = generate_embedding(dim, rng)
        # True waste has similarity 0.88-0.99
        target_sim = rng.uniform(0.88, 0.99)
        similar = generate_similar_embedding(base, target_sim, rng)
        actual_sim = cosine_similarity(base, similar)

        pairs.append(EmbeddingPair(
            id_a=f"waste-a-{i:04d}",
            id_b=f"waste-b-{i:04d}",
            embedding_a=base,
            embedding_b=similar,
            true_similarity=actual_sim,
            is_waste=True,
        ))

    # Not waste: moderate similarity (different intent)
    n_not_waste = n_pairs - n_waste
    for i in range(n_not_waste):
        base = generate_embedding(dim, rng)
        # Not-waste has similarity 0.55-0.92 (intentional overlap with waste zone)
        target_sim = rng.uniform(0.55, 0.92)
        similar = generate_similar_embedding(base, target_sim, rng)
        actual_sim = cosine_similarity(base, similar)

        pairs.append(EmbeddingPair(
            id_a=f"notwaste-a-{i:04d}",
            id_b=f"notwaste-b-{i:04d}",
            embedding_a=base,
            embedding_b=similar,
            true_similarity=actual_sim,
            is_waste=False,
        ))

    return pairs


def evaluate_threshold(
    pairs: list[EmbeddingPair],
    threshold: float,
) -> ThresholdResult:
    """Evaluate a threshold against labeled pairs."""
    tp = fp = fn = tn = 0

    for pair in pairs:
        predicted_waste = pair.true_similarity >= threshold
        if pair.is_waste and predicted_waste:
            tp += 1
        elif pair.is_waste and not predicted_waste:
            fn += 1
        elif not pair.is_waste and predicted_waste:
            fp += 1
        else:
            tn += 1

    return ThresholdResult(
        threshold=threshold,
        true_positives=tp,
        false_positives=fp,
        false_negatives=fn,
        true_negatives=tn,
    )


def calibrate_redundant_call(
    n_pairs: int = 200,
    seed: int = 42,
) -> DetectorCalibration:
    """Calibrate the redundant_call detector's similarity threshold."""
    pairs = generate_calibration_pairs(n_pairs=n_pairs, seed=seed)

    cal = DetectorCalibration(detector_name="redundant_call")
    for threshold in CANDIDATE_THRESHOLDS:
        result = evaluate_threshold(pairs, threshold)
        cal.results.append(result)

    cal.calibrate()
    return cal


def calibrate_retry_storm(
    n_pairs: int = 200,
    seed: int = 42,
) -> DetectorCalibration:
    """Calibrate the retry_storm detector's similarity threshold.

    Retry storms require higher similarity (0.95 default) since retries
    should be near-identical.
    """
    rng = random.Random(seed)
    pairs: list[EmbeddingPair] = []
    dim = 256

    # True retries: very high similarity (0.94-0.999)
    n_waste = n_pairs // 2
    for i in range(n_waste):
        base = generate_embedding(dim, rng)
        target_sim = rng.uniform(0.94, 0.999)
        similar = generate_similar_embedding(base, target_sim, rng)
        actual_sim = cosine_similarity(base, similar)
        pairs.append(EmbeddingPair(
            id_a=f"retry-a-{i:04d}",
            id_b=f"retry-b-{i:04d}",
            embedding_a=base,
            embedding_b=similar,
            true_similarity=actual_sim,
            is_waste=True,
        ))

    # Not retries: moderate-to-high similarity (0.80-0.96)
    n_not = n_pairs - n_waste
    for i in range(n_not):
        base = generate_embedding(dim, rng)
        target_sim = rng.uniform(0.80, 0.96)
        similar = generate_similar_embedding(base, target_sim, rng)
        actual_sim = cosine_similarity(base, similar)
        pairs.append(EmbeddingPair(
            id_a=f"notretry-a-{i:04d}",
            id_b=f"notretry-b-{i:04d}",
            embedding_a=base,
            embedding_b=similar,
            true_similarity=actual_sim,
            is_waste=False,
        ))

    cal = DetectorCalibration(detector_name="retry_storm")
    for threshold in CANDIDATE_THRESHOLDS:
        result = evaluate_threshold(pairs, threshold)
        cal.results.append(result)

    cal.calibrate()
    return cal


def run_calibration(
    n_calibration_tasks: int = 20,
    seed: int = 42,
) -> CalibrationReport:
    """Run full calibration across all similarity-based detectors.

    Returns a CalibrationReport with per-detector results and recommendations.
    """
    report = CalibrationReport(
        seed=seed,
        n_calibration_tasks=n_calibration_tasks,
    )

    # Calibrate similarity-based detectors
    report.detectors.append(calibrate_redundant_call(n_pairs=200, seed=seed))
    report.detectors.append(calibrate_retry_storm(n_pairs=200, seed=seed))

    # Note: context_bloat, model_overkill, cache_miss_repeat use
    # non-similarity thresholds (token counts, pricing) — they don't
    # need embedding-based calibration.

    report.check_all_pass()
    return report


def format_calibration_report(report: CalibrationReport) -> str:
    """Format calibration results as a human-readable report."""
    lines = [
        "THRESHOLD CALIBRATION REPORT",
        "=" * 60,
        f"Seed: {report.seed}",
        f"Calibration tasks: {report.n_calibration_tasks}",
        "",
    ]

    for det in report.detectors:
        lines.append(f"Detector: {det.detector_name}")
        lines.append("-" * 40)
        lines.append(
            f"  {'THRESHOLD':>10} {'PREC':>8} {'RECALL':>8} {'F1':>8} "
            f"{'TP':>5} {'FP':>5} {'FN':>5} {'TN':>5}"
        )

        for r in det.results:
            marker = " <--" if abs(r.threshold - det.recommended_threshold) < 1e-6 else ""
            lines.append(
                f"  {r.threshold:>10.2f} {r.precision:>8.3f} {r.recall:>8.3f} "
                f"{r.f1:>8.3f} {r.true_positives:>5} {r.false_positives:>5} "
                f"{r.false_negatives:>5} {r.true_negatives:>5}{marker}"
            )

        lines.append(f"  Recommended threshold: {det.recommended_threshold:.2f}")
        lines.append("")

    lines.append(f"OVERALL: {'PASS' if report.passed else 'FAIL'}")
    lines.append(
        "(PASS = all detectors achieve precision >= 0.85 at recommended threshold)"
    )

    return "\n".join(lines)
