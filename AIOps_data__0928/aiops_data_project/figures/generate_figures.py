#!/usr/bin/env python3
"""Generate publication-style figures from the pipeline outputs.

The diagrams are drawn programmatically because no Gemini API key is available
in this environment. The numerical figures are fully data-driven and export
both vector PDF and 300-DPI PNG files.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Dict, List

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch, FancyArrowPatch
import numpy as np


PROJECT_DIR = Path(__file__).resolve().parents[1]
OUTPUT_DIR = PROJECT_DIR / "outputs"
FIGURE_DIR = PROJECT_DIR / "figures"

OCEAN_DUSK = ["#264653", "#2A9D8F", "#E9C46A", "#F4A261", "#E76F51"]
OKABE_ITO = ["#0072B2", "#56B4E9", "#009E73", "#E69F00", "#D55E00", "#CC79A7"]


plt.rcParams.update({
    "font.family": "DejaVu Sans",
    "font.size": 9,
    "axes.titlesize": 11,
    "axes.titleweight": "bold",
    "axes.labelsize": 9,
    "legend.fontsize": 8,
    "legend.frameon": False,
    "figure.dpi": 150,
    "savefig.dpi": 300,
    "savefig.bbox": "tight",
    "axes.spines.top": False,
    "axes.spines.right": False,
    "axes.grid": True,
    "grid.alpha": 0.18,
    "grid.linestyle": "-",
})


def save(fig: plt.Figure, name: str) -> None:
    FIGURE_DIR.mkdir(parents=True, exist_ok=True)
    fig.savefig(FIGURE_DIR / f"{name}.png", dpi=300)
    fig.savefig(FIGURE_DIR / f"{name}.pdf")
    plt.close(fig)


def load_json(name: str, default):
    path = OUTPUT_DIR / name
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def figure_pipeline() -> None:
    fig, ax = plt.subplots(figsize=(10.2, 3.6))
    ax.set_xlim(0, 10.2)
    ax.set_ylim(0, 3.6)
    ax.axis("off")
    boxes = [
        (0.25, 1.30, 1.55, 0.98, "RAW DATA", "archives +\ncity_data CSVs", "#E8EDF2", OCEAN_DUSK[0]),
        (2.18, 1.30, 1.55, 0.98, "INVENTORY", "schema, time,\nquality, cardinality", "#E8F2EE", OCEAN_DUSK[1]),
        (4.11, 1.30, 1.55, 0.98, "CANONICAL", "UTC + entity\nnormalization", "#FFF8E8", OCEAN_DUSK[2]),
        (6.04, 1.30, 1.55, 0.98, "CASE BUILDER", "window detection +\nmultisource evidence", "#FEF0E9", OCEAN_DUSK[3]),
        (7.97, 1.30, 1.95, 0.98, "ATTRIBUTION", "Top-5 RCA +\nfault taxonomy", "#FDECEC", OCEAN_DUSK[4]),
    ]
    for x, y, w, h, title, body, fill, accent in boxes:
        ax.add_patch(FancyBboxPatch((x, y), w, h, boxstyle="round,pad=0.03,rounding_size=0.08", facecolor=fill, edgecolor="#CBD5E1", linewidth=0.9))
        ax.add_patch(plt.Rectangle((x, y), 0.08, h, color=accent, clip_on=False))
        ax.text(x + 0.18, y + h - 0.16, title, fontsize=8.8, weight="bold", color="#243447", va="top")
        ax.text(x + 0.18, y + 0.14, body, fontsize=7.8, color="#44515E", va="bottom", linespacing=1.2)
    for left, right in zip(boxes[:-1], boxes[1:]):
        x1 = left[0] + left[2] + 0.03
        x2 = right[0] - 0.07
        y = left[1] + left[3] / 2
        ax.add_patch(FancyArrowPatch((x1, y), (x2, y), arrowstyle="-|>", mutation_scale=11, linewidth=1.2, color="#6B7280"))
    ax.text(0.25, 2.9, "Immutable observation layer", fontsize=9, color=OCEAN_DUSK[0], weight="bold")
    ax.text(4.11, 2.9, "Derived analytical layer", fontsize=9, color=OCEAN_DUSK[2], weight="bold")
    ax.text(7.97, 2.9, "Decision layer", fontsize=9, color=OCEAN_DUSK[4], weight="bold")
    ax.annotate("netflow: stream/filter before materialization", xy=(0.95, 1.27), xytext=(0.95, 0.48), ha="center", fontsize=8, color="#59636E", arrowprops={"arrowstyle": "-|>", "color": "#9CA3AF", "lw": 0.9})
    ax.text(5.1, 0.07, "Every output retains source, time, entity, and evidence lineage.", ha="center", fontsize=8.5, color="#59636E")
    save(fig, "fig_01_pipeline_overview")


def figure_volume() -> None:
    inventory = load_json("inventory.json", {})
    records = [item for item in inventory.get("source_summary", []) if item.get("files", 0)]
    records = sorted(records, key=lambda item: item.get("rows", 0))
    labels = [str(item["source"]).replace("_", "\n") for item in records]
    values = [max(1, int(item.get("rows", 0))) for item in records]
    fig, ax = plt.subplots(figsize=(6.75, 3.5))
    y = np.arange(len(labels))
    bars = ax.barh(y, values, color=OKABE_ITO[:len(values)], height=0.62, edgecolor="white")
    ax.set_xscale("log")
    ax.set_yticks(y)
    ax.set_yticklabels(labels)
    ax.set_xlabel("Extracted data rows (log scale)")
    ax.set_title("Observed data volume by source")
    for bar, value in zip(bars, values):
        ax.text(value * 1.08, bar.get_y() + bar.get_height() / 2, f"{value/1e6:.2f}M", va="center", fontsize=8)
    ax.set_xlim(1, max(values) * 4 if values else 10)
    save(fig, "fig_02_source_volume")


def figure_schema() -> None:
    inventory = load_json("inventory.json", {})
    records = [item for item in inventory.get("source_summary", []) if item.get("columns")]
    labels = [str(item["source"]).replace("_", "\n") for item in records]
    values = [int(item["columns"]) for item in records]
    colors = [OCEAN_DUSK[i % len(OCEAN_DUSK)] for i in range(len(values))]
    fig, ax = plt.subplots(figsize=(6.75, 3.2))
    x = np.arange(len(labels))
    bars = ax.bar(x, values, color=colors, width=0.64, edgecolor="white")
    ax.set_xticks(x)
    ax.set_xticklabels(labels)
    ax.set_ylabel("Number of columns")
    ax.set_title("Schema dimensionality across observation sources")
    for bar, value in zip(bars, values):
        ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + max(values) * 0.025, str(value), ha="center", va="bottom", fontsize=8)
    ax.set_ylim(0, max(values) * 1.18 if values else 10)
    save(fig, "fig_03_schema_dimensionality")


def figure_evidence_matrix() -> None:
    sources = ["node", "interface", "routing", "traffic", "FRR log", "scrape", "netflow"]
    categories = ["link", "firewall", "resource", "routing", "service"]
    matrix = np.array([
        [0.0, 0.0, 1.0, 0.0, 0.0],
        [1.0, 0.65, 0.0, 0.45, 0.0],
        [0.0, 0.0, 0.0, 1.0, 0.0],
        [0.55, 0.55, 0.0, 0.0, 1.0],
        [0.0, 0.0, 0.0, 1.25, 0.0],
        [0.0, 0.0, 0.0, 0.0, 0.0],
        [0.65, 0.65, 0.0, 0.35, 0.65],
    ])
    fig, ax = plt.subplots(figsize=(6.75, 3.8))
    im = ax.imshow(matrix, cmap="YlGnBu", vmin=0, vmax=1.25, aspect="auto")
    ax.set_xticks(np.arange(len(categories)))
    ax.set_xticklabels(categories)
    ax.set_yticks(np.arange(len(sources)))
    ax.set_yticklabels(sources)
    ax.set_title("Rule-based evidence support matrix")
    ax.set_xlabel("Fault family")
    ax.set_ylabel("Observation source")
    for i in range(matrix.shape[0]):
        for j in range(matrix.shape[1]):
            if matrix[i, j] > 0:
                ax.text(j, i, f"{matrix[i,j]:.2g}", ha="center", va="center", fontsize=8, color="#183B56" if matrix[i, j] < 0.85 else "white")
    fig.colorbar(im, ax=ax, shrink=0.78, label="Evidence weight")
    save(fig, "fig_04_evidence_matrix")


def figure_cases() -> None:
    cases = load_json("candidate_cases.json", [])
    fig, ax = plt.subplots(figsize=(6.75, 3.6))
    if not cases:
        ax.axis("off")
        ax.text(0.5, 0.5, "No candidate Case survived the current\nanomaly threshold.", ha="center", va="center", fontsize=12, color="#59636E")
        ax.set_title("Candidate Case attribution")
        save(fig, "fig_05_candidate_cases")
        return
    ranked = sorted(cases, key=lambda item: sum(float(x) for x in item.get("category_scores", {}).values()), reverse=True)[:15]
    labels = [item["case_id"].replace("candidate_case_", "C") for item in ranked][::-1]
    values = [sum(float(x) for x in item.get("category_scores", {}).values()) for item in ranked][::-1]
    colors = [OCEAN_DUSK[1] if item.get("fault_category", {}).get("major_category") == "routing" else OCEAN_DUSK[3] for item in ranked][::-1]
    y = np.arange(len(labels))
    ax.barh(y, values, color=colors, height=0.62, edgecolor="white")
    ax.set_yticks(y)
    ax.set_yticklabels(labels)
    ax.set_xlabel("Aggregated evidence score")
    ax.set_title("Candidate Cases ranked by diagnostic evidence")
    ax.grid(axis="y", visible=False)
    save(fig, "fig_05_candidate_cases")


def main() -> None:
    figure_pipeline()
    figure_volume()
    figure_schema()
    figure_evidence_matrix()
    figure_cases()
    print("[figures] generated", len(list(FIGURE_DIR.glob("fig_*.png"))), "PNG figures")


if __name__ == "__main__":
    main()
