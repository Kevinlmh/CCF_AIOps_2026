"""Shared citation catalog for prompts and deterministic adoption policy."""

from dataclasses import asdict


def evidence_catalog(pack):
    rows = {s.evidence_id: {**asdict(s), "kind": "signal"} for s in pack.signals}
    for card in pack.device_context:
        for metric in card["metrics"]:
            rows[metric["evidence_id"]] = {**metric, "node": card["node"], "kind": "metric"}
    for text in pack.text_evidence:
        rows[text["evidence_id"]] = {**text, "node": text.get("node_id"), "kind": "text"}
    return rows


def supported_category(row, root):
    label = row.get("category") if row.get("role") in {"direct", "symptom"} else row.get("supported_category")
    mapping = {"cpu_pressure": ("firewall" if root.endswith("-fw") else "resource", "cpu_pressure"),
               "link_error": ("link", "loss"), "link_loss": ("link", "loss"), "link_down": ("link", "loss")}
    if label in mapping:
        return mapping[label]
    if label in {"process_pressure", "memory_pressure", "disk_space_low", "disk_io_pressure"}:
        return "resource", label
    if label in {"bgp_session_down", "bgp_route_filter", "blackhole", "ospf6_neighbor_down",
                 "ospf6_interface_flap", "ospf6_cost_anomaly"}:
        return "routing", label
    feature = row.get("feature", "")
    if row.get("role") == "symptom":
        if feature.startswith("traffic.dns."):
            return "service", "dns_down"
        if feature.startswith("traffic.web."):
            return "service", "web_slow" if "latency" in feature else "web_5xx"
        if feature.startswith("traffic.auth."):
            return "service", "auth_timeout" if "latency" in feature else "auth_error"
    return None


def has_device_support(row, node):
    return row.get("node") == node and (
        row.get("role") == "direct" or bool(row.get("supported_category")))


def has_category_support(row, root, category):
    if supported_category(row, root) != category:
        return False
    if has_device_support(row, root):
        return True
    # City-level service evidence supports a class, not an individual instance ranking.
    parts = str(row.get("node", "")).split(":")
    return (row.get("role") == "symptom" and len(parts) >= 2 and
            parts[0] == "service-group" and root.startswith(parts[1] + "-service-vm-"))
