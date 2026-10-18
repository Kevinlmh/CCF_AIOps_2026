"""Mechanism matrix shared by rule classification and role validators.

The matrix answers one question: given an official ``(major, sub)`` category,
which evidence feature families can actually justify it, and on which node
types may that category be rooted?  It deliberately contains no thresholds and
no I/O: callers pass evidence families (or full feature names) and the chosen
root element.
"""

from __future__ import annotations

from functools import lru_cache
import re

from .contracts import load_contract

# Feature families understood by the matrix.  A family is the first dotted
# component of a feature name (``node.cpu_usage`` -> ``node``).
EVIDENCE_FAMILIES = frozenset({"node", "interface", "routing", "traffic", "netflow"})

# Router-like node roles allowed for link/routing roots.
ROUTER_ROLES = frozenset({"br", "cr", "fw"})

# Fixed matrix: (major, sub-or-None, allowed feature templates).
# ``sub=None`` is the major-level fallback used only when no exact sub entry
# exists.  A template may contain ``*`` wildcards, e.g.
# ``traffic.*.latency_p95_seconds``.
MECHANISM_RULES: tuple[tuple[str, str | None, tuple[str, ...]], ...] = (
    ("resource", "cpu_pressure", ("node.cpu_usage",)),
    ("resource", "memory_pressure", ("node.memory_available_ratio",)),
    ("resource", "disk_io_pressure", (
        "node.disk_io_util",
        "node.disk_read_rate",
        "node.disk_write_rate",
    )),
    ("resource", "disk_space_low", (
        "node.filesystem_used_ratio",
        "node.inode_used_ratio",
    )),
    ("resource", "process_pressure", (
        "node.process_count",
        "node.open_fd_ratio",
    )),
    ("resource", "softirq_pressure", ("node.cpu_usage",)),
    ("firewall", "cpu_pressure", ("node.cpu_usage",)),
    ("link", "loss", (
        "interface.rx_drop_rate",
        "interface.tx_drop_rate",
        "interface.rx_error_rate",
        "interface.tx_error_rate",
        "interface.carrier_changes",
        "interface.rx_packets_rate",
        "interface.tx_packets_rate",
        "netflow.path.response_ratio",
    )),
    ("link", "ndp_failure", (
        "interface.carrier_changes",
        "interface.rx_error_rate",
        "interface.tx_error_rate",
        "netflow.path.response_ratio",
    )),
    ("link", "vxlan_misconfig", (
        "netflow.path.response_ratio",
        "netflow.bytes.protocol_17",
    )),
    ("link", "delay", (
        "traffic.*.latency_p95_seconds",
        "netflow.path.response_ratio",
    )),
    ("link", "rate_limit", (
        "traffic.*.latency_p95_seconds",
        "netflow.path.response_ratio",
        "interface.rx_packets_rate",
        "interface.tx_packets_rate",
    )),
    ("firewall", None, (
        "node.cpu_usage",
        "traffic.*",
        "netflow.path.response_ratio",
        "netflow.service.response_ratio",
    )),
    ("routing", None, (
        "routing.*",
        "netflow.service.response_ratio",
    )),
    ("service", None, (
        "traffic.*",
        "netflow.service.response_ratio",
    )),
)

# Exact (major, sub) view of the matrix, kept for callers that want to inspect
# a category without going through ``mechanism_compatible``.
FAMILY_RULES: dict[tuple[str, str], frozenset[str]] = {
    (major, sub): frozenset(templates)
    for major, sub, templates in MECHANISM_RULES
    if sub is not None
}

# Major-level fallbacks, consulted only when ``(major, sub)`` has no exact row.
MECHANISM_FALLBACKS: dict[str, frozenset[str]] = {
    major: frozenset(templates)
    for major, sub, templates in MECHANISM_RULES
    if sub is None
}


def evidence_family(feature: str) -> str:
    """Map an evidence feature name to its family.

    Unknown or malformed prefixes map to ``"unknown"`` so an unrecognised
    feature can never accidentally justify a category.
    """
    if not isinstance(feature, str) or not feature:
        return "unknown"
    prefix = feature.split(".", 1)[0]
    return prefix if prefix in EVIDENCE_FAMILIES else "unknown"


@lru_cache(maxsize=None)
def _template_pattern(template: str) -> re.Pattern[str]:
    """Compile a matrix template into a full-match regex (``*`` = any text)."""
    return re.compile(".*".join(re.escape(part) for part in template.split("*")))


def _template_matches(template: str, evidence: str) -> bool:
    """Return whether one evidence item satisfies one allowed template.

    ``evidence`` may be a bare family name (``"traffic"``) or a full feature
    name (``"traffic.dns.latency_p95_seconds"``).  A bare family matches any
    template of that family, which lets callers validate at family granularity;
    a full feature must also match the template's literal parts and wildcards.
    """
    if not isinstance(evidence, str) or not evidence:
        return False
    evidence = evidence.strip()
    if evidence in EVIDENCE_FAMILIES:
        return template.split(".", 1)[0] == evidence
    if evidence_family(evidence) == "unknown":
        return False
    return _template_pattern(template).fullmatch(evidence) is not None


def _node_role(node: str) -> str:
    """Return the device role token, tolerating trailing role indices.

    Official ids such as ``xian-br-1`` encode the role before the trailing
    instance number, so ``rsplit("-", 1)[-1]`` alone would yield ``"1"``.
    """
    head, _, tail = node.rpartition("-")
    if tail.isdigit() and head:
        _, _, role = head.rpartition("-")
        return role or head
    return tail


@lru_cache(maxsize=1)
def _official_nodes() -> frozenset[str]:
    return load_contract().nodes


def root_type_matches(major: str, root: str) -> bool:
    """Return whether ``root`` is a legal root element type for ``major``."""
    if not isinstance(root, str) or not root:
        return False
    if major == "service":
        return root.endswith(("-service-vm-1", "-service-vm-2", "-service-vm-3"))
    if major == "firewall":
        return root.endswith("-fw")
    if major in {"link", "routing"}:
        return _node_role(root) in ROUTER_ROLES
    if major == "resource":
        return root in _official_nodes()
    return False


def mechanism_compatible(major: str, sub: str, families: set[str], root: str) -> str | None:
    """Validate a category/evidence/root combination.

    Returns ``None`` when the category is justified, otherwise a rejection code:
    ``"mechanism_missing"`` when no allowed evidence family is present or the
    category has no matrix row, and ``"root_type_mismatch"`` when the evidence
    mechanism fits but ``root`` is the wrong element type for the category.

    Lookup order is exact ``(major, sub)`` first, then the major-level fallback.
    """
    templates = FAMILY_RULES.get((major, sub))
    if templates is None:
        templates = MECHANISM_FALLBACKS.get(major)
    if templates is None:
        return "mechanism_missing"
    if not any(
        _template_matches(template, evidence)
        for template in templates
        for evidence in families
    ):
        return "mechanism_missing"
    if not root_type_matches(major, root):
        return "root_type_mismatch"
    return None


def is_clear_event(pack, rules) -> bool:
    """Return whether a pack is unambiguous enough for the rules fast path.

    The pack must expose ``signals`` (with ``node``/``feature``/``category``/
    ``role``) and ``candidates`` (with ``node``/``score``); the rules diagnosis
    must expose ``roots`` and ``category``.  No store or contract access happens
    here beyond the root-type check inside ``mechanism_compatible``.
    """
    direct = [signal for signal in pack.signals if signal.role == "direct"]
    if not direct:
        return False
    direct_nodes = {signal.node for signal in direct}
    if len(direct_nodes) != 1:
        return False
    if len({signal.category for signal in direct}) != 1:
        return False
    node = next(iter(direct_nodes))

    scores: dict[str, float] = {}
    for candidate in pack.candidates:
        previous = scores.get(candidate.node)
        if previous is None or candidate.score > previous:
            scores[candidate.node] = candidate.score
    ordered = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
    if len(ordered) < 2 or ordered[0][0] != node:
        return False
    # Allow for float representation error at the exact 0.15 boundary.
    if ordered[0][1] - ordered[1][1] + 1e-9 < 0.15:
        return False

    major, sub = rules.category
    families = {evidence_family(signal.feature) for signal in direct}
    return mechanism_compatible(major, sub, families, node) is None
