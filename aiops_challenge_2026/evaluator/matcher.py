"""Global maximum-weight one-to-one interval matching."""

from __future__ import annotations

from typing import Sequence


def dice(start_a, end_a, start_b, end_b) -> float:
    overlap = max(0.0, (min(end_a, end_b) - max(start_a, start_b)).total_seconds())
    denominator = (end_a - start_a).total_seconds() + (end_b - start_b).total_seconds()
    return 2.0 * overlap / denominator if denominator > 0 else 0.0


def _hungarian_min(cost: list[list[float]]) -> list[tuple[int, int]]:
    """Return a minimum-cost assignment using only the Python standard library.

    The implementation accepts a rectangular matrix with no fewer columns than
    rows.  Zero-weight padding is used by the caller so assignments below the
    Dice threshold can remain unmatched.
    """
    if not cost:
        return []
    rows, columns = len(cost), len(cost[0])
    if rows > columns:
        transposed = [list(column) for column in zip(*cost)]
        return [(column, row) for row, column in _hungarian_min(transposed)]
    if any(len(row) != columns for row in cost):
        raise ValueError("cost matrix must be rectangular")

    u = [0.0] * (rows + 1)
    v = [0.0] * (columns + 1)
    matching = [0] * (columns + 1)
    predecessor = [0] * (columns + 1)
    for row in range(1, rows + 1):
        matching[0] = row
        column0 = 0
        minimum = [float("inf")] * (columns + 1)
        used = [False] * (columns + 1)
        while True:
            used[column0] = True
            row0 = matching[column0]
            delta = float("inf")
            column1 = 0
            for column in range(1, columns + 1):
                if used[column]:
                    continue
                current = cost[row0 - 1][column - 1] - u[row0] - v[column]
                if current < minimum[column]:
                    minimum[column] = current
                    predecessor[column] = column0
                if minimum[column] < delta:
                    delta = minimum[column]
                    column1 = column
            for column in range(columns + 1):
                if used[column]:
                    u[matching[column]] += delta
                    v[column] -= delta
                else:
                    minimum[column] -= delta
            column0 = column1
            if matching[column0] == 0:
                break
        while True:
            previous = predecessor[column0]
            matching[column0] = matching[previous]
            column0 = previous
            if column0 == 0:
                break

    return [(matching[column] - 1, column - 1) for column in range(1, columns + 1) if matching[column]]


def maximum_weight_matches(truths: Sequence[dict], predictions: Sequence[dict], threshold: float = 0.4) -> list[tuple[int, int, float]]:
    if not truths or not predictions:
        return []
    weights = [[
        0.0
        if p.get("_invalid_core") or p.get("_start") is None or p.get("_end") is None
        else dice(t["_start"], t["_end"], p["_start"], p["_end"])
        for p in predictions
    ] for t in truths]
    matrix = [[0.0 for _ in predictions] for _ in truths]
    for i, row in enumerate(weights):
        for j, value in enumerate(row):
            if value >= threshold:
                matrix[i][j] = value
    assignments = _hungarian_min([[-value for value in row] for row in matrix])
    return [(i, j, float(matrix[i][j])) for i, j in assignments if i < len(truths) and j < len(predictions) and matrix[i][j] >= threshold]
