"""Build compact ``minute x service x observer`` statistics from raw NetFlow CSVs.

The raw five-tuple exports are large (tens of millions of rows per region and
day) and contain every flow seen by every exporting node.  Downstream detection
only needs the request/response packet and byte totals of the three target
services (see :data:`SERVICES`) per observing city, so this module streams the
CSVs, keeps one accumulator per ``(minute, service, observer_city)`` and writes
one JSON object per accumulator plus a reproducibility manifest.

Parsing is deliberately tolerant of the quirks in the public batches (quoted
fields, a UTF-8 BOM, CRLF line endings and ``\\N`` nulls) and deliberately
strict about what counts as a service flow.  An address such as
``fd00:1:40::31:5201`` is *not* the DNS target and is ignored; only exact
``fd00:<target>:40::31`` plus port matches are aggregated.
"""

from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

FORMAT = "v3-netflow-service-v1"

# service -> (target city digit, target port); the target address is
# ``fd00:<digit>:40::31`` for every service.
SERVICES = {"dns": (1, 1053), "web": (6, 80), "auth": (5, 8080)}
SERVICE_NODE = {
    "dns": "beida-service-vm-1",
    "web": "shanghai-service-vm-1",
    "auth": "wuhan-service-vm-1",
}
CITY_BY_DIGIT = {
    1: "beida",
    2: "shenyang",
    3: "xian",
    4: "chengdu",
    5: "wuhan",
    6: "shanghai",
    7: "nanjing",
    8: "guangzhou",
}

_TARGETS = {
    (f"fd00:{digit}:40::31", port): service
    for service, (digit, port) in SERVICES.items()
}
_SERVICE_SUFFIX = ":40::31"
_DIGITS = {str(digit) for digit in CITY_BY_DIGIT}
_NULL_TEXT = frozenset({"", "\\n", "null", "none", "nan"})

# Columns the aggregation cannot work without.  Failing loudly beats writing an
# empty or silently partial statistics file for a malformed export.
_REQUIRED_COLUMNS = (
    "minute_utc", "protocol", "src_addr", "src_port",
    "dst_addr", "dst_port", "packets", "bytes",
)


def _text(row: dict, field: str) -> str | None:
    """Return a stripped non-null cell value, or ``None`` for ``\\N``/empty."""
    value = row.get(field)
    if value is None:
        return None
    text = value.strip()
    if text.lower() in _NULL_TEXT:
        return None
    return text


def _number(text: str | None) -> float | None:
    if text is None:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _integer(text: str | None) -> int | None:
    if text is None:
        return None
    try:
        return int(text)
    except ValueError:
        try:
            return int(float(text))
        except ValueError:
            return None


def _city_from_address(address: str) -> str | None:
    """Match an observer address ``fd00:[1-8]:40::10`` exactly."""
    parts = address.split(":")
    if (
        len(parts) == 5
        and parts[0] == "fd00"
        and parts[1] in _DIGITS
        and parts[2] == "40"
        and parts[3] == ""
        and parts[4] == "10"
    ):
        return CITY_BY_DIGIT[int(parts[1])]
    return None


def _parse_timestamp(text: str) -> datetime | None:
    """Parse a raw ``minute_utc`` cell as a naive UTC datetime."""
    for shape in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(text, shape)
        except ValueError:
            continue
    try:
        value = datetime.fromisoformat(text)
    except ValueError:
        return None
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def _as_utc(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _input_files(inputs: list[Path]) -> list[Path]:
    """Expand directories and drop duplicate files, preserving input order."""
    files: list[Path] = []
    seen: set[Path] = set()
    for item in inputs:
        path = Path(item)
        candidates = sorted(path.rglob("*netflow*.csv")) if path.is_dir() else [path]
        for candidate in candidates:
            if not candidate.is_file():
                raise FileNotFoundError(f"NetFlow input does not exist: {candidate}")
            resolved = candidate.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            files.append(candidate)
    if not files:
        raise ValueError("no NetFlow CSV inputs were provided")
    return files


def _input_labels(files: list[Path]) -> list[str]:
    """Return unique, deterministic manifest labels for the input files.

    Labels are relative to the common parent directory, so a single input is
    just its basename while a batch of same-named exports keeps enough of the
    directory structure to stay distinguishable.  Full paths are a last resort.
    """
    resolved = [path.resolve() for path in files]
    prefix: list[str] = []
    for parts in zip(*(path.parts for path in resolved)):
        if len(set(parts)) != 1:
            break
        prefix.append(parts[0])
    root = Path(*prefix) if prefix else Path(resolved[0].anchor or ".")
    if len(files) == 1:
        root = root.parent
    labels: list[str] = []
    used: set[str] = set()
    for path in resolved:
        try:
            label = str(path.relative_to(root))
        except ValueError:
            label = str(path)
        if label in used:
            label = str(path)
        if label in used:
            label = f"{label}#{len(labels)}"
        used.add(label)
        labels.append(label)
    return labels


def _consume_row(
    row: dict,
    *,
    aggregates: dict[tuple[int, str, str], list[float]],
    minute_cache: dict[str, int | None],
    start_naive: datetime,
    minute_count: int,
    skipped: dict[str, int],
) -> None:
    src = _text(row, "src_addr")
    dst = _text(row, "dst_addr")
    if src is None or dst is None:
        return
    src = src.lower()
    dst = dst.lower()
    # Cheap pre-filter: both a service request and its response touch the exact
    # target address, while unrelated client flows never do.
    if not (dst.endswith(_SERVICE_SUFFIX) or src.endswith(_SERVICE_SUFFIX)):
        return

    protocol = _integer(_text(row, "protocol"))
    if protocol is None:
        skipped["malformed"] += 1
        return
    if protocol == 58:
        return

    minute_text = _text(row, "minute_utc")
    if minute_text is None:
        skipped["unparseable_minute"] += 1
        return
    if minute_text not in minute_cache:
        moment = _parse_timestamp(minute_text)
        minute_cache[minute_text] = (
            None if moment is None
            else int((moment - start_naive).total_seconds() // 60)
        )
    minute = minute_cache[minute_text]
    if minute is None:
        skipped["unparseable_minute"] += 1
        return
    if not 0 <= minute < minute_count:
        skipped["out_of_range"] += 1
        return

    dst_port = _integer(_text(row, "dst_port"))
    src_port = _integer(_text(row, "src_port"))
    if dst_port is None or src_port is None:
        skipped["malformed"] += 1
        return

    service = _TARGETS.get((dst, dst_port))
    if service is not None:
        kind = "req"
        city = _city_from_address(src)
    else:
        service = _TARGETS.get((src, src_port))
        if service is None:
            return
        kind = "resp"
        city = _city_from_address(dst)
    if city is None:
        skipped["unattributed"] += 1
        return

    packets = _number(_text(row, "packets"))
    octets = _number(_text(row, "bytes"))
    if packets is None or octets is None:
        skipped["malformed"] += 1
        return

    cell = aggregates.get((minute, service, city))
    if cell is None:
        cell = [0.0, 0.0, 0.0, 0.0]
        aggregates[(minute, service, city)] = cell
    if kind == "req":
        cell[0] += packets
        cell[2] += octets
    else:
        cell[1] += packets
        cell[3] += octets


def _consume(
    path: Path,
    aggregates: dict[tuple[int, str, str], list[float]],
    *,
    start_naive: datetime,
    minute_count: int,
    skipped: dict[str, int],
) -> None:
    minute_cache: dict[str, int | None] = {}
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise ValueError(f"{path}: missing CSV header")
        reader.fieldnames = [name.strip() for name in reader.fieldnames]
        missing = [name for name in _REQUIRED_COLUMNS if name not in reader.fieldnames]
        if missing:
            raise ValueError(f"{path}: missing columns: {', '.join(missing)}")
        for row in reader:
            _consume_row(
                row,
                aggregates=aggregates,
                minute_cache=minute_cache,
                start_naive=start_naive,
                minute_count=minute_count,
                skipped=skipped,
            )


def build(
    inputs: list[Path],
    output: Path,
    *,
    start_time: datetime,
    minute_count: int,
) -> dict:
    """Aggregate target-service NetFlow rows and write compact JSONL + manifest.

    ``start_time`` is converted to UTC and minute indices are computed as whole
    minutes since that instant.  Rows whose minute is unparseable or outside
    ``[0, minute_count)`` are skipped and counted in the manifest; minutes with
    no observed service flow are simply absent from the output.
    """
    if not isinstance(start_time, datetime):
        raise TypeError("start_time must be a datetime")
    if minute_count < 1:
        raise ValueError("minute_count must be positive")

    files = _input_files(inputs)
    labels = _input_labels(files)
    start_utc = _as_utc(start_time)
    start_naive = start_utc.replace(tzinfo=None)
    aggregates: dict[tuple[int, str, str], list[float]] = {}
    skipped = {
        "out_of_range": 0,
        "unparseable_minute": 0,
        "malformed": 0,
        "unattributed": 0,
    }
    for path in files:
        _consume(
            path,
            aggregates,
            start_naive=start_naive,
            minute_count=minute_count,
            skipped=skipped,
        )

    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.name + ".tmp")
    ordered = sorted(aggregates)
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        for key in ordered:
            minute, service, city = key
            req_pkts, resp_pkts, req_bytes, resp_bytes = aggregates[key]
            handle.write(json.dumps({
                "minute": minute,
                "service": service,
                "service_node": SERVICE_NODE[service],
                "observer_city": city,
                "req_pkts": req_pkts,
                "resp_pkts": resp_pkts,
                "req_bytes": req_bytes,
                "resp_bytes": resp_bytes,
            }, ensure_ascii=False) + "\n")
    temporary.replace(output)

    manifest = {
        "format": FORMAT,
        "start_time": start_utc.isoformat(),
        "minute_count": minute_count,
        "rows": len(ordered),
        "minutes": len({key[0] for key in aggregates}),
        "services": sorted({key[1] for key in aggregates}),
        "input_sha256": {
            label: _sha256(path) for label, path in zip(labels, files)
        },
        "input_files": labels,
        "input_paths": [str(path) for path in files],
        "code_sha256": _sha256(Path(__file__)),
        "skipped": dict(skipped),
    }
    manifest_path = Path(str(output) + ".manifest.json")
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return manifest


def load_stats(path: Path) -> tuple[dict, ...]:
    """Read a statistics JSONL file as an in-memory tuple (no interpretation)."""
    rows: list[dict] = []
    with Path(path).open("r", encoding="utf-8") as handle:
        for line in handle:
            text = line.strip()
            if text:
                rows.append(json.loads(text))
    return tuple(rows)


def _read_input_list(path: Path) -> list[Path]:
    entries: list[Path] = []
    with path.open("r", encoding="utf-8-sig") as handle:
        for line in handle:
            text = line.strip()
            if not text or text.startswith("#"):
                continue
            entries.append(Path(text))
    return entries


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build compact NetFlow service/observer minute statistics")
    parser.add_argument("--input-list", type=Path, action="append", default=[],
                        dest="input_lists",
                        help="file with one CSV path per line (repeatable)")
    parser.add_argument("--input", type=Path, action="append", default=[],
                        dest="inputs", help="CSV path (repeatable)")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--start-time", required=True)
    parser.add_argument("--minute-count", type=int, required=True)
    args = parser.parse_args()

    inputs: list[Path] = []
    for list_path in args.input_lists:
        inputs.extend(_read_input_list(list_path))
    inputs.extend(args.inputs)
    if not inputs:
        parser.error("at least one --input or --input-list entry is required")

    raw_start = args.start_time.strip()
    if raw_start.endswith("Z"):
        raw_start = raw_start[:-1] + "+00:00"
    try:
        start_time = datetime.fromisoformat(raw_start)
    except ValueError as error:
        parser.error(f"invalid --start-time: {error}")
    if start_time.tzinfo is None:
        parser.error("--start-time must include a timezone offset")

    manifest = build(inputs, args.output, start_time=start_time,
                     minute_count=args.minute_count)
    print(json.dumps({
        "output": str(args.output),
        "format": manifest["format"],
        "rows": manifest["rows"],
        "minutes": manifest["minutes"],
        "services": manifest["services"],
        "skipped": manifest["skipped"],
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
