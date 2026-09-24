from __future__ import annotations

import numpy as np
import pytest

from aiops_challenge_2026.config import load_public_config
from aiops_v2.localization.ranking import EVENT_FEATURE_NAMES, ROOT_FEATURE_NAMES
from aiops_v2.training.synthetic import generate_synthetic_diagnosis_data


def test_synthetic_diagnosis_data_is_deterministic_and_covers_taxonomy() -> None:
    taxonomy = load_public_config("fault_taxonomy")
    first = generate_synthetic_diagnosis_data(
        taxonomy,
        examples_per_category=3,
        seed=19,
        root_feature_names=ROOT_FEATURE_NAMES,
        event_feature_names=EVENT_FEATURE_NAMES,
    )
    second = generate_synthetic_diagnosis_data(
        taxonomy,
        examples_per_category=3,
        seed=19,
        root_feature_names=ROOT_FEATURE_NAMES,
        event_feature_names=EVENT_FEATURE_NAMES,
    )

    assert len(first) == 3 * len(taxonomy["fault_categories"])
    assert {item.category_index for item in first} == set(
        range(len(taxonomy["fault_categories"]))
    )
    for left, right in zip(first, second, strict=True):
        assert np.array_equal(left.candidate_features, right.candidate_features)
        assert np.array_equal(left.event_features, right.event_features)
        assert np.array_equal(left.candidate_mask, right.candidate_mask)
        assert left.root_index == right.root_index
        assert left.category_index == right.category_index


def test_synthetic_diagnosis_examples_have_legal_shapes_and_positive_root() -> None:
    examples = generate_synthetic_diagnosis_data(
        load_public_config("fault_taxonomy"),
        examples_per_category=1,
        seed=7,
        root_feature_names=ROOT_FEATURE_NAMES,
        event_feature_names=EVENT_FEATURE_NAMES,
    )

    for example in examples:
        assert example.candidate_features.shape == (80, len(ROOT_FEATURE_NAMES))
        assert example.event_features.shape == (len(EVENT_FEATURE_NAMES),)
        assert example.candidate_mask.shape == (80,)
        assert example.candidate_mask.all()
        assert 0 <= example.root_index < 80
        assert np.isfinite(example.candidate_features).all()
        assert np.isfinite(example.event_features).all()
        assert example.candidate_features[example.root_index].max() > 0


def test_synthetic_generator_rejects_feature_schema_mismatch() -> None:
    with pytest.raises(ValueError, match="feature schema"):
        generate_synthetic_diagnosis_data(
            load_public_config("fault_taxonomy"),
            examples_per_category=1,
            seed=3,
            root_feature_names=("unrecognized",),
            event_feature_names=EVENT_FEATURE_NAMES,
        )
