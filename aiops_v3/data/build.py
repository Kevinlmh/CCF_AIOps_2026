"""Build canonical feature stores from explicit stage-1 or stage-2 profiles."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
import re
import shutil
import tempfile

from .feature_store import build_feature_store
from .multisource import SOURCE_ORDER, iter_source_files, validate_source_header
from .observations import city_from_path
from .source import CanonicalObservationStream
from aiops_v3.store import open_store


_STAGE2_WINDOWS = (
    ("20260917040000", "20260919040000"),
    ("20260919040000", "20260921040000"),
    ("20260921040000", "20260924040000"),
)


def inspect_raw_sources(raw_root: Path, *, profile: str = "stage1") -> dict:
    """Check the whole bundle and every CSV header before an expensive build."""
    raw_root = Path(raw_root)
    if profile not in {"stage1", "stage2"}:
        raise ValueError(f"unknown source profile: {profile}")
    expected_sources = (
        SOURCE_ORDER if profile == "stage1"
        else ("node", "interface", "routing", "scrape", "netflow")
    )
    files = list(iter_source_files(raw_root, profile=profile))
    if not files:
        raise ValueError(f"no processed telemetry CSVs under {raw_root}")
    cases = {
        part for _, path in files for part in path.parts
        if re.fullmatch(r"case_\d+", part)
    }
    if len(cases) > 1:
        raise ValueError("independent cases must be built separately; choose one case directory")
    network = json.loads((Path(__file__).parent.parent / "config" / "network_elements.json").read_text())
    bundles: dict[Path, dict[str, Path]] = {}
    for source, path in files:
        if source not in expected_sources:
            raise ValueError(f"unexpected source in {profile} bundle: {source}: {path}")
        bundle = path.parent.parent
        by_source = bundles.setdefault(bundle, {})
        if source in by_source:
            raise ValueError(f"duplicate source in {bundle}: {source}: {by_source[source]}, {path}")
        by_source[source] = path
    for bundle, found in sorted(bundles.items()):
        missing = set(expected_sources) - set(found)
        if missing:
            raise ValueError(f"{bundle}: missing required source files: {sorted(missing)}")

    if profile == "stage2":
        expected_bundles = {
            raw_root / f"{city}_{start}_{end}"
            for city in network["cities"]
            for start, end in _STAGE2_WINDOWS
        }
        missing_bundles = expected_bundles - set(bundles)
        if missing_bundles:
            raise ValueError(f"missing stage2 bundle(s): {sorted(map(str, missing_bundles))}")
        extra_bundles = {path for path in raw_root.iterdir() if path.is_dir()} - expected_bundles
        if extra_bundles:
            raise ValueError(f"unexpected stage2 bundle(s): {sorted(map(str, extra_bundles))}")
        discovered = {path for _, path in files}
        unknown_csv = set(raw_root.glob("*/*/*.csv")) - discovered
        if unknown_csv:
            raise ValueError(f"unrecognized stage2 CSV file(s): {sorted(map(str, unknown_csv))}")
        for bundle, by_source in bundles.items():
            city = bundle.name.split("_", 1)[0]
            if any(path.parent.name != city for path in by_source.values()):
                raise ValueError(f"stage2 city directory does not match bundle: {bundle}")

    aliases = {city: city for city in network["cities"]}
    for source, path in files:
        if city_from_path(path, aliases) is None:
            raise ValueError(f"source file has no known city in its path: {path}")
        validate_source_header(path, source)
    source_files = [
        {"path": str(path), "bytes": path.stat().st_size, "source": source}
        for source, path in files
    ]
    return {
        "source_profile": profile,
        "expected_sources": list(expected_sources),
        "bundle_count": len(bundles),
        "file_count": len(files),
        "source_file_counts": dict(sorted(Counter(source for source, _ in files).items())),
        "raw_bytes": sum(item["bytes"] for item in source_files),
        "source_files": source_files,
    }


def build_raw_feature_store(raw_root: Path, destination: Path, *, profile: str = "stage1") -> Path:
    """Use the v2 disk-backed projector and publish a checked v3 store."""
    raw_root, destination = Path(raw_root), Path(destination)
    inspection = inspect_raw_sources(raw_root, profile=profile)
    if destination.exists():
        raise FileExistsError(f"feature store destination already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{destination.name}.building-", dir=destination.parent))
    try:
        network = json.loads((Path(__file__).parent.parent / "config" / "network_elements.json").read_text())
        aliases = {city: city for city in network["cities"]}
        stream = CanonicalObservationStream(
            raw_root, aliases=aliases, valid_roles=network["device_roles"], profile=profile
        )
        build_feature_store(stream, network, temporary)
        manifest_path = temporary / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        audit = manifest.get("parser_audit") or {}
        if audit.get("quality_status") != "clean":
            raise ValueError(f"parser quality failed: {audit.get('quality_issues', [])}")
        manifest["builder"] = "aiops_v3.data.build_raw_feature_store"
        manifest["source_profile"] = inspection["source_profile"]
        manifest["expected_sources"] = inspection["expected_sources"]
        manifest["source_files"] = inspection["source_files"]
        manifest["feature_units"] = {"traffic.*.requests_rate": "requests_per_minute"}
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        open_store(temporary)
        temporary.rename(destination)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return destination
