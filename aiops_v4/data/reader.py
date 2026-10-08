"""Stream lossless CSV records; normalize identity and time without inference."""

import csv
from datetime import datetime, timezone
import hashlib
import json
import math
import re

from aiops_challenge_2026.config import load_public_config
from aiops_common.data.observations import city_from_path

from .contracts import RawRecord, SourceFile
from .discovery import DIMENSION_FIELDS, TIME_FIELDS, numeric_columns


_CONFIG = load_public_config("network_elements")
_CITIES = tuple(_CONFIG["cities"])
_CHINESE = dict(zip(("北大", "沈阳", "西安", "成都", "武汉", "上海", "南京", "广州"), _CITIES))
_NODES = frozenset(f"{city}-{role}" for city in _CITIES for role in _CONFIG["device_roles"])
_ROLE_PATTERNS = [(role, re.compile(r"(?<![a-z0-9])" + re.escape(role) + r"(?![a-z0-9])"))
                  for role in sorted(_CONFIG["device_roles"], key=len, reverse=True)]
_SHORT_ROUTER = re.compile(r"(?<![a-z0-9])(br|cr)([12])(?![a-z0-9])")
_LABEL = re.compile(r'([A-Za-z0-9_]+)\s*=\s*"([^"\n]*)"')
MISSING_VALUES = frozenset({"", "null", "none", "\\n", "na", "n/a"})


def is_missing(value: str | None) -> bool:
    return value is None or value.strip().lower() in MISSING_VALUES


def utc_time(value: str, *, require_timezone: bool = False) -> tuple[str, bool]:
    parsed = datetime.fromisoformat(value.strip().strip('"').replace("Z", "+00:00"))
    assumed = parsed.tzinfo is None
    if assumed:
        if require_timezone:
            raise ValueError("query timestamp must include timezone")
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z"), assumed


def _city(value: str | None) -> str | None:
    if is_missing(value):
        return None
    clean = value.strip().lower()
    for city in _CITIES:
        if clean == city or clean.startswith((city + "-", city + "_")):
            return city
    return next((city for name, city in _CHINESE.items() if clean in {name, "ccf-aiops-" + name}), None)


def _node(value: str | None, city: str | None) -> str | None:
    if is_missing(value):
        return None
    clean = value.strip().strip('"').lower().replace("_", "-")
    clean = _SHORT_ROUTER.sub(r"\1-\2", clean)
    if clean in _NODES:
        return clean
    if city is None:
        return None
    for role, pattern in _ROLE_PATTERNS:
        if pattern.search(clean):
            return f"{city}-{role}"
    return None


class RecordReader:
    """Single-pass reader; diagnostics and content digest remain after close."""

    def __init__(self, file: SourceFile, batch: str, max_rows: int | None = None):
        if not isinstance(batch, str) or not batch.strip():
            raise ValueError("batch must be non-empty")
        if max_rows is not None and (type(max_rows) is not int or max_rows <= 0):
            raise ValueError("max_rows must be positive")
        self.file, self.batch, self.max_rows = file, batch, max_rows
        self.columns: list[str] = []
        self.schema_issues: list[str] = []
        self.rows_scanned = 0
        self.complete = False
        self.digest = hashlib.sha256()
        self._handle = None
        self._used = False
        self._path_city = city_from_path(file.path, {city: city for city in _CITIES})

    def __enter__(self):
        self._handle = self.file.path.open(newline="", encoding="utf-8-sig")
        try:
            self._csv = csv.reader(self._handle, strict=True)
            self.columns = next(self._csv, [])
            if len(set(self.columns)) != len(self.columns):
                raise ValueError(f"duplicate CSV columns: {self.file.relative_path}")
            if self.columns:
                if not any(name in self.columns for name in TIME_FIELDS[self.file.source]):
                    self.schema_issues.append("missing_timestamp_column")
                identity = ("hostname", "node") if self.file.source == "frr" else ("node_key", "node")
                if self.file.source != "traffic" and not any(name in self.columns for name in identity):
                    self.schema_issues.append("missing_entity_column")
                if self.file.source == "routing" and not {"metric_name", "value"} <= set(self.columns):
                    self.schema_issues.append("missing_routing_metric_columns")
            self.digest.update(json.dumps(self.columns, ensure_ascii=False).encode())
            self._numeric = numeric_columns(self.file.source, self.columns)
            return self
        except BaseException:
            self._handle.close()
            raise

    def __exit__(self, *_):
        self._handle.close()

    def __iter__(self):
        if self._handle is None or self._handle.closed or self._used:
            raise RuntimeError("RecordReader must be opened and consumed only once")
        self._used = True
        if not self.columns:
            self.complete = True
            return
        while True:
            line_start = self._csv.line_num + 1
            cells = next(self._csv, None)
            if cells is None:
                self.complete = True
                return
            if not cells:
                continue
            if self.max_rows is not None and self.rows_scanned >= self.max_rows:
                return
            self.rows_scanned += 1
            raw = {name: cells[index] if index < len(cells) else None
                   for index, name in enumerate(self.columns)}
            extra = tuple(cells[len(self.columns):])
            content = json.dumps([raw, extra], ensure_ascii=False, separators=(",", ":"))
            self.digest.update(content.encode() + b"\n")
            identity = json.dumps([self.batch, self.file.relative_path, self.rows_scanned, content],
                                  ensure_ascii=False, separators=(",", ":"))
            record_id = hashlib.sha256(identity.encode()).hexdigest()
            yield self._normalize(raw, extra, record_id, line_start, self._csv.line_num)

    def _normalize(self, raw, extra, record_id, line_start, line_end):
        source = self.file.source
        flags = []
        if extra:
            flags.append("extra_csv_cells")
        if len(raw) and any(value is None for value in raw.values()):
            flags.append("short_csv_row")
        timestamp = None
        had_time = False
        for field in TIME_FIELDS[source]:
            value = raw.get(field)
            if is_missing(value):
                continue
            had_time = True
            try:
                timestamp, assumed = utc_time(value)
            except ValueError:
                flags.append(f"invalid_time_field:{field}")
                continue
            if assumed:
                flags.append("assumed_utc")
            break
        if timestamp is None:
            flags.append("invalid_timestamp" if had_time else "missing_timestamp")
        city_fields = ("source_region",) if source == "traffic" else ("region_code", "region")
        cities = [_city(raw.get(field)) for field in city_fields]
        cities = [city for city in cities if city is not None]
        city = cities[0] if cities else self._path_city
        if len(set(cities + ([self._path_city] if self._path_city else []))) > 1:
            flags.append("city_conflict")
        names = ("hostname", "node") if source == "frr" else ("node_key", "node")
        node_id = next((node for field in names if (node := _node(raw.get(field), city))), None)
        if source == "traffic" and city:
            node_id = f"{city}-traffic-vm"
            flags.append("entity_from_source_role")
        if node_id is None:
            flags.append("unmapped_entity")
        elif city is None:
            city = node_id.split("-", 1)[0]
        elif not node_id.startswith(city + "-"):
            flags.append("city_entity_conflict")
        dimensions = {name: raw[name] for name in DIMENSION_FIELDS[source] if not is_missing(raw.get(name))}
        if source == "routing":
            # Raw label remains authoritative if a parsed label key collides with metadata.
            for name, value in _LABEL.findall(raw.get("label") or ""):
                dimensions.setdefault(name, value)
        values = {}
        for name in self._numeric:
            value = raw.get(name)
            if is_missing(value):
                continue
            try:
                number = float(value)
            except (ValueError, TypeError):
                flags.append(f"invalid_numeric:{name}")
                continue
            if not math.isfinite(number):
                flags.append(f"nonfinite_numeric:{name}")
                continue
            metric = raw.get("metric_name") if source == "routing" else name
            if not metric:
                flags.append("missing_metric_name")
                continue
            values[metric] = number
        text_fields = ("message", "program", "severity") if source == "frr" else ("scrape_error",)
        text = {name: raw[name] for name in text_fields if not is_missing(raw.get(name))}
        return RawRecord(self.batch, record_id, source, self.file.relative_path, self.rows_scanned,
                         line_start, line_end, timestamp, city, node_id, dimensions, values, text,
                         raw, extra, tuple(sorted(set(flags))))
