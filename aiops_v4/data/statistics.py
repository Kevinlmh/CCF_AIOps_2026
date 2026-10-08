"""Bounded streaming statistics with explicit approximation metadata."""

import hashlib
import math
import random
from statistics import median

from .reader import is_missing


def _quantile(sorted_values: list[float], fraction: float) -> float | None:
    if not sorted_values:
        return None
    position = (len(sorted_values) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(sorted_values) - 1)
    weight = position - lower
    return sorted_values[lower] * (1 - weight) + sorted_values[upper] * weight


class ColumnStats:
    """Exact counts/moments, bounded distinct tracking and reservoir quantiles."""

    def __init__(self, numeric: bool, reservoir_size: int, cardinality_limit: int):
        self.numeric = numeric
        self.capacity = reservoir_size
        self.cardinality_limit = cardinality_limit
        self.count = self.missing = self.non_numeric = self.nonfinite = self.zeros = self.n = 0
        self.mean = self.m2 = 0.0
        self.minimum = self.maximum = None
        self.sample: list[float] = []
        self.random = random.Random(0)
        self.distinct: set[bytes] = set()
        self.distinct_capped = False

    def add(self, raw: str | None):
        self.count += 1
        if is_missing(raw):
            self.missing += 1
            return
        if not self.distinct_capped:
            self.distinct.add(hashlib.sha256(raw.encode()).digest())
            if len(self.distinct) > self.cardinality_limit:
                self.distinct_capped = True
        if not self.numeric:
            self.non_numeric += 1
            return
        try:
            value = float(raw)
        except (ValueError, TypeError):
            self.non_numeric += 1
            return
        if not math.isfinite(value):
            self.nonfinite += 1
            return
        self.n += 1
        self.zeros += value == 0
        self.minimum = value if self.minimum is None else min(self.minimum, value)
        self.maximum = value if self.maximum is None else max(self.maximum, value)
        delta = value - self.mean
        self.mean += delta / self.n
        self.m2 += delta * (value - self.mean)
        if len(self.sample) < self.capacity:
            self.sample.append(value)
        else:
            index = self.random.randrange(self.n)
            if index < self.capacity:
                self.sample[index] = value

    def report(self) -> dict:
        values = sorted(self.sample)
        center = median(values) if values else None
        result = {
            "count": self.count, "numeric_count": self.n, "missing_count": self.missing,
            "non_numeric_count": self.non_numeric, "nonfinite_count": self.nonfinite,
            "zero_count": self.zeros, "min": self.minimum, "max": self.maximum,
            "mean": self.mean if self.n else None,
            "std": math.sqrt(max(0.0, self.m2 / self.n)) if self.n else None,
            "median": center, "mad": median(abs(value - center) for value in values) if values else None,
            "quantile_sample_count": len(values), "quantile_capacity": self.capacity,
            "quantiles_exact": self.n <= self.capacity, "quantile_method": "reservoir_seed_0_linear",
            "distinct_exact": not self.distinct_capped,
            "distinct_count_lower_bound": len(self.distinct),
        }
        for name, fraction in [("p01", .01), ("p05", .05), ("q25", .25), ("q75", .75), ("p95", .95), ("p99", .99)]:
            result[name] = _quantile(values, fraction)
        # Finite source values can still overflow second moments; never emit invalid JSON numbers.
        overflow = [name for name, value in result.items() if isinstance(value, float) and not math.isfinite(value)]
        for name in overflow:
            result[name] = None
        result["overflowed_statistics"] = overflow
        return result
