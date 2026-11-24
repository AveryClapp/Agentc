from __future__ import annotations

import json
import math
import random

import numpy as np
import pytest

from bench.change_safety.analysis import (
    auroc,
    classify,
    cluster_bootstrap_weights,
    first_divergence,
    holm,
    rankdata,
    spearman,
    summarize,
)
from tests.change_safety.conftest import build_records, make_trajectory


def test_spearman_matches_hand_computation_with_ties() -> None:
    # ranks y = [1, 2, 3.5, 5, 3.5]; cov 8, var 10 and 9.5
    assert spearman([1, 2, 3, 4, 5], [5, 6, 7, 8, 7]) == pytest.approx(8 / math.sqrt(95))
    assert spearman([1, 2, 3], [3, 2, 1]) == pytest.approx(-1.0)
    assert math.isnan(spearman([1, 1, 1], [1, 2, 3]))
    rows = spearman(np.array([[1, 2, 3], [3, 2, 1]]), np.array([1, 2, 3]))
    assert rows.tolist() == pytest.approx([1.0, -1.0])  # type: ignore[union-attr]


def test_rankdata_averages_ties() -> None:
    assert rankdata(np.array([10, 20, 20, 5])).tolist() == [2.0, 3.5, 3.5, 1.0]


def test_auroc_matches_pair_counting() -> None:
    scores = [0.9, 0.4, 0.6, 0.5, 0.4, 0.1]
    labels = [1, 1, 1, 0, 0, 0]
    # 3 + (0 + 0.5 + 1) + 3 = 7.5 of 9 pairs
    assert auroc(scores, labels) == pytest.approx(7.5 / 9)
    assert auroc([1, 2], [0, 1]) == 1.0
    assert math.isnan(auroc([1, 2], [1, 1]))


def test_holm_matches_known_example() -> None:
    result = holm([0.01, 0.04, 0.03, 0.005])
    assert [r["p_holm"] for r in result] == pytest.approx([0.03, 0.06, 0.06, 0.02])
    assert [r["reject"] for r in result] == [True, False, False, True]
    assert holm([None, 0.01])[0]["p_holm"] is None


def test_classify() -> None:
    assert classify(0.06, 0.2, 0.05) == "harmful"
    assert classify(-0.04, 0.04, 0.05) == "benign"
    assert classify(0.0, 0.1, 0.05) == "indeterminate"
    assert classify(None, None, 0.05) == "indeterminate"


def test_cluster_weights_resample_whole_clusters() -> None:
    w = cluster_bootstrap_weights(7, 100, np.random.default_rng(0))
    assert w.shape == (100, 7) and (w.sum(axis=1) == 7).all()


def test_first_divergence() -> None:
    assert first_divergence("abc", "abc") is None
    assert first_divergence("abc", "abx") == 2
    assert first_divergence("ab", "abc") == 2


def _damage_dataset(seed: int, n_tasks: int, effect: float) -> list:  # type: ignore[type-arg]
    rng = random.Random(seed)
    out = []
    for t in range(n_tasks):
        level = rng.uniform(0.3, 0.7)  # task difficulty, shared across arms
        for rep in range(2):
            base = min(1.0, max(0.0, level + rng.uniform(-0.25, 0.25)))
            changed = min(1.0, max(0.0, level - effect + rng.uniform(-0.25, 0.25)))
            out.append(make_trajectory("w", "m", f"t{t}", rep, "baseline", base, 5))
            out.append(make_trajectory("w", "m", f"t{t}", rep, "X1", changed, 5))
    return out


def test_task_cluster_bootstrap_covers_known_effect() -> None:
    effect, runs, covered = 0.2, 120, 0
    for seed in range(runs):
        cell = summarize(_damage_dataset(seed, 30, effect), n_boot=400, n_perm=0, seed=seed)["cells"]["w|m|X1"]
        covered += cell["damage"]["lo"] <= effect <= cell["damage"]["hi"]
    assert covered / runs >= 0.88


def test_paired_damage_averages_repetitions_first() -> None:
    records = [
        make_trajectory("w", "m", "a", 0, "baseline", 1.0, 4),
        make_trajectory("w", "m", "a", 1, "baseline", 0.0, 4),
        make_trajectory("w", "m", "a", 0, "X1", 0.0, 6),
        make_trajectory("w", "m", "b", 0, "baseline", 1.0, 4),
        make_trajectory("w", "m", "b", 0, "X1", 1.0, 4),
        make_trajectory("w", "m", "b", 1, "X1", 0.0, 4),
        make_trajectory("w", "m", "c", 0, "X1", 0.0, 4),  # unpaired task: excluded
    ]
    cell = summarize(records, n_boot=200, n_perm=0)["cells"]["w|m|X1"]
    # task a: 0.5 - 0 = 0.5; task b: 1 - 0.5 = 0.5
    assert cell["damage"]["point"] == pytest.approx(0.5)
    assert cell["n_tasks_paired"] == 2
    assert cell["added_model_calls"]["point"] == pytest.approx(1.0)  # (2 + 0) / 2


def test_summarize_end_to_end() -> None:
    arms = {
        "X1": (0.9, 0.3, 0.9, 1.0),
        "X2": (0.5, 0.1, 0.4, 0.6),
        "C1": (0.3, 0.05, 0.2, 0.3),
        "B1": (0.1, 0.0, 0.04, 0.0),
        "B2": (0.1, 0.0, 0.04, 0.0),
    }
    records = build_records(arms, workloads=("tau2", "swe"), n_tasks=24)
    report = summarize(records, n_boot=1000, n_perm=500, seed=3)
    json.dumps(report, allow_nan=False)

    floor = report["h1_aa_floor"]["tau2|m"]
    assert 0.05 < floor["action_disagreement_rate"]["point"] < 0.15
    assert floor["action_disagreement_rate"]["lo"] < floor["action_disagreement_rate"]["point"]

    cells = report["cells"]
    assert cells["tau2|m|X1"]["label"] == "harmful"
    assert cells["tau2|m|B1"]["label"] == "benign"
    s4 = cells["tau2|m|X1"]["signals"]["S4"]
    assert s4["excess_mean"]["point"] == pytest.approx(s4["fork_mean"] - s4["aa_mean"])
    assert cells["tau2|m|X1"]["signals"]["S1"]["exceed_rate"]["point"] > 0.8
    assert cells["tau2|m|B1"]["signals"]["S1"]["exceed_rate"]["point"] < 0.15
    assert cells["tau2|m|X1"]["added_model_calls"]["point"] == pytest.approx(2.0)

    for scope in ("tau2", "swe", "pooled"):
        h2 = report["h2_association"][scope]
        assert h2["spearman"]["S4"]["point"] > 0.8
        assert h2["auroc"]["S4"]["point"] == 1.0
        assert h2["spearman"]["S4"]["p_holm"] >= h2["spearman"]["S4"]["p_perm"]
        assert set(report["h3_action_vs_text"][scope]) == {"S4-S1", "S4-S2", "S4-S3"}
    assert report["h2_association"]["pooled"]["n_cells"] == 10

    h4 = report["h4"]
    assert h4["family_ratio"]["seeded_harm"]["ratio"]["S4"]["point"] > 0
    profile = h4["step_profiles"]["tau2|m|X1"]["S4"]["relative"]
    assert len(profile) == 10 and profile[0]["excess"] > 0.5
    first = h4["trajectory_first_divergence"]
    assert first["tau2|m|X1"]["first_index_median"] == 5.0
    assert first["tau2|m|baseline_aa"]["diverged_rate"] == 0.0
