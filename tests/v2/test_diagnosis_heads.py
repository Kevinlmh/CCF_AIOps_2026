from __future__ import annotations

import torch

from aiops_challenge_2026.config import load_public_config
from aiops_v2.diagnosis_schema import EVENT_FEATURE_NAMES, ROOT_FEATURE_NAMES
from aiops_v2.models.heads import EventDiagnosisHeads
from aiops_v2.training.trainer import fit_diagnosis_heads


def test_diagnosis_heads_return_candidate_logits_and_mask_illegal_nodes() -> None:
    model = EventDiagnosisHeads(
        root_feature_count=5,
        event_feature_count=3,
        category_count=4,
        hidden_size=8,
    )
    candidates = torch.randn(2, 6, 5)
    context = torch.randn(2, 3)
    mask = torch.tensor(
        [[True, True, False, True, True, True], [False, True, True, True, True, True]]
    )

    root_logits, category_logits = model(candidates, context, mask)

    assert root_logits.shape == (2, 6)
    assert category_logits.shape == (2, 6, 4)
    assert torch.isneginf(root_logits[0, 2])
    assert torch.isneginf(category_logits[0, 2]).all()
    assert torch.isfinite(root_logits[mask]).all()
    assert torch.isfinite(category_logits[mask]).all()


def test_diagnosis_head_training_generalizes_to_unseen_magnitude_variants() -> None:
    taxonomy = load_public_config("fault_taxonomy")
    _, summary = fit_diagnosis_heads(
        taxonomy,
        len(ROOT_FEATURE_NAMES),
        len(EVENT_FEATURE_NAMES),
        seed=23,
        device="cpu",
        examples_per_category=8,
        epochs=10,
        batch_size=32,
        hidden_size=32,
    )

    assert summary.root_validation_accuracy >= 0.80
    assert summary.category_validation_accuracy >= 0.60
    assert summary.training_example_count == 8 * len(taxonomy["fault_categories"])
    assert summary.validation_example_count == len(taxonomy["fault_categories"])
