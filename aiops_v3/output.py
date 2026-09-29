"""Produce only fields accepted by the public prediction schema."""

from __future__ import annotations

from .contracts import OfficialContract, validate_prediction
from .diagnosis import Diagnosis, EvidencePack


def prediction_record(pack: EvidencePack, diagnosis: Diagnosis, contract: OfficialContract) -> dict:
    record = {
        "prediction_id": pack.event_id,
        "start_time": pack.start_time,
        "end_time": pack.end_time,
        "root_cause_top5": [
            {"rank": rank, "network_element_id": node}
            for rank, node in enumerate(diagnosis.roots, 1)
        ],
        "fault_category": {"major_category": diagnosis.category[0], "sub_category": diagnosis.category[1]},
    }
    validate_prediction(record, contract)
    return record
