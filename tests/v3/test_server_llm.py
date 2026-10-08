from dataclasses import asdict
from datetime import datetime, timezone
import json
import pytest

from aiops_v3.contracts import load_contract
from aiops_v3.detection import Event, Signal
from aiops_v3.diagnosis import build_evidence, diagnose_rules
from aiops_v3.optional_llm import choose_diagnosis
from aiops_v3.server_llm import diagnose_records, run_batch


class ClockStore:
    def time_at(self, minute):
        return datetime(2026, 9, 17, 4, minute, tzinfo=timezone.utc)


def _record():
    event = Event("v3-e000001", 5, 8, (
        Signal("node:5:0:node.cpu_usage", 5, "xian-service-vm-1", "node.cpu_usage",
               "cpu_pressure", 80, 10, 7, "direct"),
    ))
    pack = build_evidence(ClockStore(), event, load_contract())
    return {**asdict(pack), "evidence_sha256": pack.sha256()}, pack


def test_server_adapter_emits_importable_response_and_rejects_hallucinated_root():
    record, pack = _record()
    valid_roots = [item.node for item in pack.candidates[:5]]
    requests = []

    def valid_completion(request):
        requests.append(request)
        content = {
            "root_cause_top5": valid_roots,
            "fault_category": {"major_category": "resource", "sub_category": "cpu_pressure"},
            "evidence_ids": [pack.signals[0].evidence_id],
        }
        return {"choices": [{"message": {"content": json.dumps(content)}}]}

    responses, audit = diagnose_records([record], valid_completion, "Qwen/Qwen3-8B-AWQ", "revision-1")
    assert audit[0]["status"] == "accepted"
    assert responses[0]["evidence_sha256"] == pack.sha256()
    assert responses[0]["model"]["revision"] == "revision-1"
    assert requests[0]["response_format"]["type"] == "json_schema"
    diagnosis, reason = choose_diagnosis(pack, diagnose_rules(pack, load_contract()),
                                         responses[0], load_contract())
    assert reason is None
    assert diagnosis.roots == tuple(valid_roots)

    def hallucinated_completion(request):
        response = valid_completion(request)
        row = json.loads(response["choices"][0]["message"]["content"])
        row["root_cause_top5"][0] = "invented-device"
        response["choices"][0]["message"]["content"] = json.dumps(row)
        return response

    rejected, audit = diagnose_records([record], hallucinated_completion, "Qwen/Qwen3-8B-AWQ", "revision-1")
    assert rejected == []
    assert audit[0]["status"] == "invalid_llm_response"

    malformed, audit = diagnose_records(
        [record], lambda _: {"choices": [{"message": {"content": "[]"}}]},
        "Qwen/Qwen3-8B-AWQ", "revision-1")
    assert malformed == []
    assert audit[0]["status"] == "invalid_llm_response"


def test_server_prompt_identifies_traffic_observer_without_treating_it_as_root():
    class TrafficStore(ClockStore):
        edges = [{"source": "xian-traffic-vm", "target": "service-group:beida:dns", "relation": "traffic"}]

    event = Event("v3-e000011", 5, 8, (
        Signal("edge:5:0:traffic.dns.error_ratio", 5, "service-group:beida:dns",
               "traffic.dns.error_ratio", "dns", 1, 0, 8, "symptom"),
    ))
    pack = build_evidence(TrafficStore(), event, load_contract())
    record = {**asdict(pack), "evidence_sha256": pack.sha256()}
    requests = []

    def completion(payload):
        requests.append(payload)
        rule = diagnose_rules(pack, load_contract())
        return {"choices": [{"message": {"content": json.dumps({
            "root_cause_top5": list(rule.roots),
            "fault_category": {"major_category": rule.category[0], "sub_category": rule.category[1]},
            "evidence_ids": list(rule.evidence_ids),
        })}}]}

    accepted, audit = diagnose_records([record], completion, "local-test", "r1")
    assert audit[0]["status"] == "accepted"
    assert accepted[0]["evidence_sha256"] == record["evidence_sha256"]
    prompt = json.loads(requests[0]["messages"][1]["content"])
    assert prompt["traffic_observations"][0]["observer"] == "xian-traffic-vm"
    assert "observer" in requests[0]["messages"][0]["content"].lower()
    assert "xian-traffic-vm" not in prompt["rule_baseline"]["root_cause_top5"]


def test_server_batch_rejects_invalid_cached_response_before_resume(tmp_path):
    record, pack = _record()
    evidence = tmp_path / "evidence.jsonl"
    evidence.write_text(json.dumps(record) + "\n")
    output = tmp_path / "responses.jsonl"
    content = {
        "root_cause_top5": [item.node for item in pack.candidates[:5]],
        "fault_category": {"major_category": "resource", "sub_category": "cpu_pressure"},
        "evidence_ids": [pack.signals[0].evidence_id],
    }
    valid, _ = diagnose_records([record], lambda _: {
        "choices": [{"message": {"content": json.dumps(content)}}]},
        "Qwen/Qwen3-8B-AWQ", "r1")
    valid[0]["root_cause_top5"] = ["invented-device"] * 5
    output.write_text(json.dumps(valid[0]) + "\n")
    with pytest.raises(ValueError, match="invalid cached response"):
        run_batch(evidence, output, tmp_path / "audit.jsonl", model="Qwen/Qwen3-8B-AWQ",
                  revision="r1", base_url="http://127.0.0.1:8000/v1")


def test_server_batch_rejects_duplicate_event_ids_before_writing(tmp_path):
    record, _ = _record()
    evidence = tmp_path / "evidence.jsonl"
    evidence.write_text((json.dumps(record) + "\n") * 2)
    output = tmp_path / "responses.jsonl"
    with pytest.raises(ValueError, match="duplicate event ids"):
        run_batch(evidence, output, tmp_path / "audit.jsonl", model="Qwen/Qwen3-8B-AWQ",
                  revision="r1", base_url="http://127.0.0.1:8000/v1")
    assert not output.exists()


def test_server_batch_rejects_cached_response_from_another_prompt(tmp_path):
    record, pack = _record()
    evidence = tmp_path / "evidence.jsonl"
    evidence.write_text(json.dumps(record) + "\n")
    output = tmp_path / "responses.jsonl"
    output.write_text(json.dumps({
        "event_id": record["event_id"], "evidence_sha256": record["evidence_sha256"],
        "root_cause_top5": [item.node for item in pack.candidates[:5]],
        "fault_category": {"major_category": "resource", "sub_category": "cpu_pressure"},
        "evidence_ids": [pack.signals[0].evidence_id],
        "model": {"repository": "Qwen/Qwen3-8B-AWQ", "revision": "r1"},
        "prompt_sha256": "0" * 64,
    }) + "\n")
    with pytest.raises(ValueError, match="prompt"):
        run_batch(evidence, output, tmp_path / "audit.jsonl", model="Qwen/Qwen3-8B-AWQ",
                  revision="r1", base_url="http://127.0.0.1:8000/v1")


def test_server_batch_resumes_matching_prompt_without_request(tmp_path):
    record, pack = _record()
    content = {
        "root_cause_top5": [item.node for item in pack.candidates[:5]],
        "fault_category": {"major_category": "resource", "sub_category": "cpu_pressure"},
        "evidence_ids": [pack.signals[0].evidence_id],
    }
    accepted, _ = diagnose_records([record], lambda _: {
        "choices": [{"message": {"content": json.dumps(content)}}]},
        "Qwen/Qwen3-8B-AWQ", "r1")
    evidence = tmp_path / "evidence.jsonl"
    evidence.write_text(json.dumps(record) + "\n")
    output = tmp_path / "responses.jsonl"
    output.write_text(json.dumps(accepted[0]) + "\n")
    summary = run_batch(evidence, output, tmp_path / "audit.jsonl", model="Qwen/Qwen3-8B-AWQ",
                        revision="r1", base_url="http://127.0.0.1:1/v1")
    assert (summary["already_accepted"], summary["new_accepted"]) == (1, 0)


def test_vllm_request_uses_supported_bounded_schema():
    from aiops_v3.server_llm import _request_payload
    _, pack = _record()
    schema = _request_payload(pack, "test", load_contract())["response_format"]["json_schema"]["schema"]
    assert "uniqueItems" not in json.dumps(schema)
    assert schema["properties"]["evidence_ids"]["maxItems"] == 6


def test_complete_json_at_token_limit_is_not_accepted():
    record, pack = _record()
    body = {"root_cause_top5": [c.node for c in pack.candidates[:5]],
            "fault_category": {"major_category": "resource", "sub_category": "cpu_pressure"},
            "evidence_ids": [pack.signals[0].evidence_id]}
    responses, audit = diagnose_records([record], lambda _: {
        "choices": [{"message": {"content": json.dumps(body)}, "finish_reason": "length"}],
        "usage": {"prompt_tokens": 200, "completion_tokens": 512}}, "test", "r1")
    assert responses == []
    assert audit[0]["status"] == "truncated_response"
    assert audit[0]["finish_reason"] == "length"
    assert audit[0]["usage"]["completion_tokens"] == 512


def test_http_failure_includes_status_and_response_body():
    import io
    from urllib.error import HTTPError
    record, _ = _record()

    def complete(_):
        raise HTTPError("http://127.0.0.1/v1", 400, "Bad Request", {},
                        io.BytesIO(b'{"error":{"message":"unsupported grammar"}}'))

    responses, audit = diagnose_records([record], complete, "test", "r1")
    assert responses == []
    assert audit[0]["http_status"] == 400
    assert "unsupported grammar" in audit[0]["detail"]


@pytest.mark.parametrize("field,value", [
    ("evidence_ids", ["node:5:0:node.cpu_usage", "node:5:0:node.cpu_usage"]),
    ("evidence_ids", [{}]),
    ("fault_category", {"major_category": [], "sub_category": "cpu_pressure"}),
])
def test_importer_rejects_invalid_evidence_and_category_without_crashing(field, value):
    _, pack = _record()
    body = {"event_id": pack.event_id, "evidence_sha256": pack.sha256(),
            "root_cause_top5": [c.node for c in pack.candidates[:5]],
            "fault_category": {"major_category": "resource", "sub_category": "cpu_pressure"},
            "evidence_ids": [pack.signals[0].evidence_id]}
    body[field] = value
    diagnosis, reason = choose_diagnosis(pack, diagnose_rules(pack, load_contract()), body, load_contract())
    assert reason == "invalid_llm_response"
    assert diagnosis.source == "rules"
