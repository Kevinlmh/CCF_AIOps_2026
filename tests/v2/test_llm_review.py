from aiops_v2.models.llm_review import EventReview


def test_llm_backend_failure_keeps_local_result():
    class FailingBackend:
        def complete(self, prompt: str) -> str:
            raise RuntimeError("backend unavailable")

    local_category = {"major_category": "resource", "sub_category": "cpu_pressure"}
    reviewer = EventReview(
        FailingBackend(),
        {"fault_categories": [local_category]},
    )

    outcome = reviewer.review(
        {"allowed_evidence_ids": ["root:node-a"]},
        local_roots=("node-a",),
        local_category=local_category,
    )

    assert outcome.status == "fallback"
    assert outcome.root_cause_top5 == ("node-a",)
    assert outcome.fault_category == local_category
    assert outcome.suggested_root_cause_top5 is None
    assert outcome.suggested_fault_category is None
