from datetime import timezone

import pytest

from aiops_v3.contracts import ContractError, load_contract, parse_time, validate_prediction


def valid_record():
    return {
        "prediction_id": "v3-0001",
        "start_time": "2026-07-28T12:39:00Z",
        "end_time": "2026-07-28T12:52:00Z",
        "root_cause_top5": [
            {"rank": i + 1, "network_element_id": f"xian-service-vm-{i + 1}"}
            for i in range(3)
        ] + [
            {"rank": 4, "network_element_id": "xian-br-1"},
            {"rank": 5, "network_element_id": "xian-br-2"},
        ],
        "fault_category": {"major_category": "resource", "sub_category": "cpu_pressure"},
    }


def test_official_contract_accepts_five_unique_nodes_and_category():
    contract = load_contract()
    assert len(contract.nodes) == 80
    assert len(contract.categories) == 32
    assert {("routing", name) for name in (
        "bgp_route_filter", "ospf6_interface_flap", "route_loop", "long_path_interruption",
    )} <= contract.categories
    validate_prediction(valid_record(), contract)
    assert parse_time("2026-07-28T12:39:00Z").tzinfo == timezone.utc


@pytest.mark.parametrize("change", ["duplicate", "rank", "category", "naive", "end", "too_long"])
def test_invalid_public_prediction_is_rejected(change):
    record = valid_record()
    if change == "duplicate":
        record["root_cause_top5"][1]["network_element_id"] = "xian-service-vm-1"
    elif change == "rank":
        record["root_cause_top5"][1]["rank"] = 3
    elif change == "category":
        record["fault_category"]["sub_category"] = "invented"
    elif change == "naive":
        record["start_time"] = "2026-07-28T12:39:00"
    elif change == "too_long":
        record["end_time"] = "2026-07-28T13:11:00Z"
    else:
        record["end_time"] = "2026-07-28T12:38:00Z"
    with pytest.raises(ContractError):
        validate_prediction(record, load_contract())
