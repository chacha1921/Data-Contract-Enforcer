from __future__ import annotations

import argparse
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import yaml

try:
    import anthropic
except Exception:
    anthropic = None

try:
    from dotenv import load_dotenv
except Exception:
    load_dotenv = None

try:
    from ydata_profiling import ProfileReport
except Exception:
    ProfileReport = None


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_LINEAGE_PATH = PROJECT_ROOT / "outputs" / "week4" / "lineage_snapshots.jsonl"
DEFAULT_REGISTRY_PATH = PROJECT_ROOT / "contract_registry" / "subscriptions.yaml"
DEFAULT_EXPLODE_FIELDS = {"code_refs", "extracted_facts", "nodes", "edges"}
UUID_PATTERN = re.compile(r"^[0-9a-f-]{36}$")

if load_dotenv is not None:
    load_dotenv(PROJECT_ROOT / ".env")
SOURCE_PRESETS = {
    "outputs/week1/intent_records.jsonl": {
        "contract_id": "week1-intent-records",
        "output_name": "week1_intent_records",
        "model_name": "intent_records",
        "title": "Week 1 Intent Records",
        "owner": "week1-team",
        "description": "Intent classification records emitted from the Week 1 orchestration output.",
        "usage": "Internal workflow orchestration data contract.",
        "limitations": "Intent labels and source references must remain stable for downstream workflow routing.",
    },
    "outputs/week3/extractions.jsonl": {
        "contract_id": "week3-document-refinery-extractions",
        "output_name": "week3_extractions",
        "model_name": "extractions",
        "title": "Week 3 Document Refinery — Extraction Records",
        "owner": "week3-team",
        "description": "One record per processed document containing extracted facts and metadata.",
        "usage": "Internal inter-system data contract. Do not publish.",
        "limitations": "confidence must remain in 0.0–1.0 float range.",
    },
    "outputs/week4/lineage_snapshots.jsonl": {
        "contract_id": "week4-cartographer-lineage",
        "output_name": "week4_lineage",
        "model_name": "lineage_snapshots",
        "title": "Week 4 Cartographer — Lineage Snapshots",
        "owner": "week4-team",
        "description": "Lineage graph snapshots used for downstream impact analysis and blast-radius enrichment.",
        "usage": "Internal lineage enrichment contract.",
        "limitations": "Edge source and target identifiers must remain stable between snapshots.",
    },
    "outputs/week5/events.jsonl": {
        "contract_id": "week5-ledger-events",
        "output_name": "week5_events",
        "model_name": "events",
        "title": "Week 5 Ledger — Events",
        "owner": "week5-team",
        "description": "Replayable event records emitted by the Week 5 ledger pipeline.",
        "usage": "Internal event-stream contract.",
        "limitations": "Event timestamps and event taxonomy must remain stable for replay and attribution.",
    },
    "outputs/traces/runs.jsonl": {
        "contract_id": "langsmith-traces",
        "output_name": "langsmith_traces",
        "model_name": "langsmith_traces",
        "title": "LangSmith Traces — Runs",
        "owner": "week7-team",
        "description": "Operational trace export summarizing AI run metadata, token usage, and cost.",
        "usage": "Internal observability contract.",
        "limitations": "Token counts and cost metrics must remain numeric for monitoring and reporting.",
    },
}


def parse_args() -> argparse.Namespace:
    """Parse CLI arguments for the four-stage contract generator."""
    parser = argparse.ArgumentParser(
        description="Generate Bitol and dbt contracts from profiled JSONL data."
    )
    parser.add_argument("--source", required=False, help="Path to the source JSONL or JSON file.")
    parser.add_argument(
        "--contract-id",
        required=False,
        help="Optional contract identifier. Defaults to a slug derived from the source path.",
    )
    parser.add_argument(
        "--lineage",
        required=False,
        default=str(DEFAULT_LINEAGE_PATH),
        help="Path to a Week 4 lineage JSONL/JSON/YAML file or directory of snapshots.",
    )
    parser.add_argument(
        "--registry",
        required=False,
        default=str(DEFAULT_REGISTRY_PATH),
        help="Path to the contract registry subscriptions YAML file.",
    )
    parser.add_argument(
        "--output",
        required=True,
        help="Output directory or explicit Bitol YAML file path.",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Generate contracts for all required Week 7 sources.",
    )
    return parser.parse_args()


def slugify(value: str) -> str:
    """Convert arbitrary identifiers into stable, file-safe slugs."""
    normalized = re.sub(r"[^a-zA-Z0-9]+", "_", value).strip("_").lower()
    return normalized or "contract"


def derive_contract_id(source_path: Path) -> str:
    """Create a default contract id from the dataset location."""
    parts = [source_path.parent.name, source_path.stem]
    return slugify("_".join(part for part in parts if part))


def load_records(source_path: Path) -> list[dict[str, Any]]:
    """Load JSON or JSONL records into a list of dictionaries."""
    if not source_path.exists():
        raise FileNotFoundError(f"Source file not found: {source_path}")

    if source_path.suffix.lower() == ".json":
        payload = json.loads(source_path.read_text(encoding="utf-8"))
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        if isinstance(payload, dict):
            return [payload]
        raise ValueError("JSON source must contain an object or a list of objects.")

    records: list[dict[str, Any]] = []
    with source_path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            raw_line = line.strip()
            if not raw_line:
                continue
            payload = json.loads(raw_line)
            if not isinstance(payload, dict):
                raise ValueError(
                    f"Expected one JSON object per line in {source_path} at line {line_number}."
                )
            records.append(payload)
    return records


def normalize_scalar(value: Any) -> Any:
    """Convert nested non-exploded structures into stable scalars for profiling."""
    if isinstance(value, (dict, list)):
        return json.dumps(value, sort_keys=True, ensure_ascii=False)
    return value


def flatten_object(
    payload: dict[str, Any], parent_key: str = "", separator: str = "__"
) -> dict[str, Any]:
    """Recursively flatten a nested dictionary using Bitol/dbt-friendly field names."""
    flattened: dict[str, Any] = {}
    for key, value in payload.items():
        composite_key = f"{parent_key}{separator}{key}" if parent_key else str(key)
        if isinstance(value, dict):
            flattened.update(flatten_object(value, composite_key, separator))
        else:
            flattened[composite_key] = normalize_scalar(value)
    return flattened


def expand_list_field(field_name: str, values: list[Any]) -> list[dict[str, Any]]:
    """Explode array values like Week 3 extracted_facts and Week 4 nodes/edges."""
    if not values:
        return [{f"{field_name}__count": 0}]

    rows: list[dict[str, Any]] = []
    for index, item in enumerate(values):
        if isinstance(item, dict):
            flattened_item = flatten_object(item, field_name)
            flattened_item[f"{field_name}__index"] = index
            rows.append(flattened_item)
        else:
            rows.append(
                {
                    f"{field_name}__value": normalize_scalar(item),
                    f"{field_name}__index": index,
                }
            )
    return rows


def flatten_records(
    records: list[dict[str, Any]],
    explode_fields: set[str] | None = None,
) -> list[dict[str, Any]]:
    """
    Flatten records for profiling.

    Arrays listed in explode_fields are exploded into one row per nested item so that
    fields like extracted_facts__confidence and edges__target can be profiled directly.
    """
    explode_fields = explode_fields or set(DEFAULT_EXPLODE_FIELDS)
    flattened_rows: list[dict[str, Any]] = []

    for record in records:
        row_variants: list[dict[str, Any]] = [{}]
        for key, value in record.items():
            if isinstance(value, dict):
                flattened_value = flatten_object(value, str(key))
                row_variants = [{**row, **flattened_value} for row in row_variants]
                continue

            if isinstance(value, list) and str(key) in explode_fields:
                expanded_rows = expand_list_field(str(key), value)
                new_variants: list[dict[str, Any]] = []
                for row in row_variants:
                    for expanded in expanded_rows:
                        new_variants.append({**row, **expanded})
                row_variants = new_variants
                continue

            normalized_value = normalize_scalar(value)
            row_variants = [{**row, str(key): normalized_value} for row in row_variants]

        flattened_rows.extend(row_variants)

    return flattened_rows


def flatten_for_profile(records: list[dict[str, Any]]) -> pd.DataFrame:
    """Compatibility wrapper used by runner.py."""
    rows = flatten_records(records)
    return pd.DataFrame(rows) if rows else pd.DataFrame()


def infer_type(dtype_str: str) -> str:
    """Map pandas dtypes to Bitol types."""
    mapping = {
        "float64": "number",
        "float32": "number",
        "Float64": "number",
        "int64": "integer",
        "int32": "integer",
        "Int64": "integer",
        "bool": "boolean",
        "boolean": "boolean",
        "object": "string",
        "string": "string",
    }
    return mapping.get(dtype_str, "string")


def safe_float(value: Any) -> float | None:
    if value is None or pd.isna(value):
        return None
    return round(float(value), 6)


def dominant_character_pattern(values: list[Any]) -> str | None:
    patterns: dict[str, int] = {}
    for value in values:
        text = str(value)
        pattern = []
        for char in text:
            if char.isdigit():
                pattern.append("9")
            elif char.isalpha():
                pattern.append("A")
            elif char.isspace():
                pattern.append(" ")
            else:
                pattern.append(char)
        normalized = "".join(pattern)
        patterns[normalized] = patterns.get(normalized, 0) + 1
    if not patterns:
        return None
    return max(patterns.items(), key=lambda item: item[1])[0]


def profile_column(series: pd.Series, column_name: str) -> dict[str, Any]:
    """Build structural and statistical profiles for a single flattened column."""
    non_null = series.dropna()
    string_lengths = [len(str(value)) for value in non_null.tolist()]
    numeric_series = pd.to_numeric(non_null, errors="coerce")
    numeric_non_null = numeric_series.dropna()
    numeric_fraction = (
        float(numeric_non_null.shape[0]) / float(non_null.shape[0]) if non_null.shape[0] else 0.0
    )
    all_numeric_integral = bool(
        not numeric_non_null.empty
        and numeric_non_null.shape[0] == non_null.shape[0]
        and numeric_non_null.apply(lambda value: float(value).is_integer()).all()
    )

    profile = {
        "name": column_name,
        "dtype": str(series.dtype),
        "non_null_count": int(non_null.shape[0]),
        "null_fraction": round(float(series.isna().mean()), 6),
        "cardinality_estimate": int(series.nunique(dropna=True)),
        "sample_values": [
            normalize_scalar(value) for value in non_null.unique().tolist()[:5]
        ],
        "avg_string_length": round(sum(string_lengths) / len(string_lengths), 6)
        if string_lengths
        else 0.0,
        "numeric_fraction": round(numeric_fraction, 6),
        "all_numeric_integral": all_numeric_integral,
        "dominant_character_pattern": dominant_character_pattern(non_null.tolist()[:50]) if str(series.dtype) == "object" else None,
        "stats": {
            "min": None,
            "max": None,
            "mean": None,
            "p25": None,
            "p50": None,
            "p75": None,
            "std": None,
            "p95": None,
            "p99": None,
        },
    }

    if not numeric_non_null.empty:
        profile["dtype"] = str(numeric_non_null.dtype)
        profile["stats"] = {
            "min": safe_float(numeric_non_null.min()),
            "max": safe_float(numeric_non_null.max()),
            "mean": safe_float(numeric_non_null.mean()),
            "p25": safe_float(numeric_non_null.quantile(0.25)),
            "p50": safe_float(numeric_non_null.quantile(0.50)),
            "p75": safe_float(numeric_non_null.quantile(0.75)),
            "std": safe_float(numeric_non_null.std(ddof=0)),
            "p95": safe_float(numeric_non_null.quantile(0.95)),
            "p99": safe_float(numeric_non_null.quantile(0.99)),
        }

    return profile


def profile_records(frame: pd.DataFrame) -> list[dict[str, Any]]:
    """Profile all flattened columns in a dataframe."""
    if frame.empty:
        return []
    return sorted(
        (profile_column(frame[column], str(column)) for column in frame.columns),
        key=lambda item: item["name"],
    )


def sample_preview(value: Any, max_length: int = 48) -> str:
    rendered = str(value)
    if len(rendered) <= max_length:
        return rendered
    return f"{rendered[: max_length - 3]}..."


def render_samples(profile: dict[str, Any], limit: int = 3) -> str:
    samples = [sample_preview(value) for value in profile.get("sample_values", [])[:limit]]
    return ", ".join(samples)


def samples_match_uuid(profile: dict[str, Any]) -> bool:
    samples = [str(value).lower() for value in profile.get("sample_values", []) if value is not None]
    return bool(samples) and all(UUID_PATTERN.fullmatch(value) for value in samples)


def force_string_for_textual_numeric(profile: dict[str, Any], field_name: str) -> bool:
    leaf_name = field_name.split("__")[-1].lower()
    avg_string_length = float(profile.get("avg_string_length", 0.0) or 0.0)
    numeric_fraction = float(profile.get("numeric_fraction", 0.0) or 0.0)
    text_like_leaf = leaf_name in {"value", "text"} or "text" in leaf_name
    return avg_string_length > 10.0 and (text_like_leaf or numeric_fraction < 1.0)


def infer_profile_type(profile: dict[str, Any], field_name: str) -> str:
    dtype_str = str(profile["dtype"])
    numeric_fraction = float(profile.get("numeric_fraction", 0.0) or 0.0)
    non_null_count = int(profile.get("non_null_count", 0) or 0)

    if field_name.endswith("_id"):
        return "string"
    if force_string_for_textual_numeric(profile, field_name):
        return "string"
    if non_null_count > 0 and numeric_fraction == 1.0:
        return "integer" if bool(profile.get("all_numeric_integral")) else "number"
    return infer_type(dtype_str)


def enum_values(profile: dict[str, Any], bitol_type: str, field_name: str) -> list[Any] | None:
    """Only create enums for low-cardinality string columns, per the manual."""
    if bitol_type != "string":
        return None
    if field_name.endswith("_at"):
        return None
    if int(profile["cardinality_estimate"]) > 8:
        return None
    values = [value for value in profile.get("sample_values", []) if value is not None]
    if not values:
        return None
    return sorted(values, key=lambda item: str(item))


def describe_field(profile: dict[str, Any], bitol_type: str, contract_id: str, field_name: str) -> str:
    """Generate clearer field descriptions using names, samples, and dataset-specific hints."""
    leaf_name = field_name.split("__")[-1].lower()
    sample_text = render_samples(profile)

    if "confidence" in leaf_name and bitol_type == "number":
        return "Confidence score. MUST be float 0.0-1.0. 0-100 scale is a breaking change."

    week3_hints = {
        "doc_id": "Document identifier for the extracted source document.",
        "source_doc_id": "Original source document identifier carried into the extraction output.",
        "block_id": "Identifier for the extracted page block or OCR segment.",
        "block_type": "Block classification emitted by the extraction pipeline.",
        "language": "Detected language code for the extracted block.",
        "language_confidence": "Confidence score for language detection on the extracted block.",
        "fact_type": "Semantic type assigned to each extracted fact item.",
        "value": "Extracted fact payload captured from document content.",
    }
    week5_hints = {
        "event_id": "Unique identifier for the emitted business event.",
        "aggregate_id": "Identifier for the event-sourced aggregate that emitted the event.",
        "aggregate_type": "Aggregate category responsible for the event.",
        "event_type": "Business event name emitted by the Week 5 ledger stream.",
        "event_version": "Version number of the event contract at emit time.",
        "occurred_at": "Timestamp when the event occurred in the source workflow.",
        "recorded_at": "Timestamp when the event was recorded in the ledger.",
    }

    description = None
    if contract_id.startswith("week3"):
        if field_name.startswith("extracted_facts__"):
            description = week3_hints.get(leaf_name, "Nested extracted fact attribute.")
        else:
            description = week3_hints.get(leaf_name)
    elif contract_id.startswith("week5"):
        description = week5_hints.get(leaf_name)

    if description is None:
        description = (
            f"{field_name.replace('__', ' -> ')} represented as {bitol_type}. "
            f"Null fraction {profile['null_fraction']:.3f}; distinct values {profile['cardinality_estimate']}."
        )

    if sample_text:
        description += f" Sample values: {sample_text}."
    if bitol_type in {"number", "integer"}:
        stats = profile["stats"]
        description += (
            f" Observed range {stats['min']} to {stats['max']}; "
            f"mean {stats['mean']}; p25 {stats['p25']}; p50 {stats['p50']}; p75 {stats['p75']}; std {stats['std']}; p95 {stats['p95']}; p99 {stats['p99']}."
        )
    return description


def column_to_clause(
    profile: dict[str, Any],
    contract_id: str,
    *,
    field_name_override: str | None = None,
) -> dict[str, Any]:
    """Translate a column profile into Bitol field clauses."""
    field_name = field_name_override or str(profile["name"])
    bitol_type = infer_profile_type(profile, field_name)
    numeric_metrics = profile["stats"] if bitol_type in {"number", "integer"} else {
        "min": None,
        "max": None,
        "mean": None,
        "std": None,
        "p95": None,
        "p99": None,
    }
    clause: dict[str, Any] = {
        "name": field_name,
        "type": bitol_type,
        "required": profile["null_fraction"] == 0.0,
        "description": describe_field(profile, bitol_type, contract_id, field_name),
        "metrics": {
            "dtype": profile["dtype"],
            "null_fraction": profile["null_fraction"],
            "cardinality": profile["cardinality_estimate"],
            "dominant_character_pattern": profile.get("dominant_character_pattern"),
            **numeric_metrics,
        },
    }

    enum = enum_values(profile, bitol_type, field_name)
    if enum:
        clause["enum"] = enum

    if bitol_type in {"number", "integer"}:
        if profile["stats"]["min"] is not None:
            clause["minimum"] = profile["stats"]["min"]
        if profile["stats"]["max"] is not None:
            clause["maximum"] = profile["stats"]["max"]

    if field_name.endswith("_id") and samples_match_uuid(profile):
        clause["format"] = "uuid"
    if field_name.endswith("_at"):
        clause["format"] = "date-time"

    if "confidence" in field_name.lower() and bitol_type == "number":
        clause["minimum"] = 0.0
        clause["maximum"] = 1.0

    return clause


def build_array_field_clauses(profiles: list[dict[str, Any]], contract_id: str) -> list[dict[str, Any]]:
    array_groups: dict[str, list[dict[str, Any]]] = {}
    for profile in profiles:
        name = str(profile["name"])
        root_name, separator, child_name = name.partition("__")
        if not separator or root_name not in DEFAULT_EXPLODE_FIELDS:
            continue
        if child_name in {"index", "count"}:
            continue
        array_groups.setdefault(root_name, []).append(profile)

    clauses: list[dict[str, Any]] = []
    for root_name, child_profiles in sorted(array_groups.items()):
        item_fields = [
            column_to_clause(
                profile,
                contract_id,
                field_name_override=str(profile["name"]).split("__", 1)[1],
            )
            for profile in child_profiles
        ]
        clauses.append(
            {
                "name": root_name,
                "type": "array",
                "description": (
                    f"Collection field {root_name} exploded for profiling and represented as array items. "
                    f"Item fields: {', '.join(field['name'] for field in item_fields)}."
                ),
                "items": {
                    "type": "object",
                    "fields": item_fields,
                },
            }
        )
    return clauses


def build_model_fields(profiles: list[dict[str, Any]], contract_id: str) -> list[dict[str, Any]]:
    array_child_names: set[str] = set()
    for profile in profiles:
        name = str(profile["name"])
        root_name, separator, child_name = name.partition("__")
        if separator and root_name in DEFAULT_EXPLODE_FIELDS:
            array_child_names.add(name)

    scalar_fields = [
        column_to_clause(profile, contract_id)
        for profile in profiles
        if str(profile["name"]) not in array_child_names
    ]
    return scalar_fields + build_array_field_clauses(profiles, contract_id)


def expand_contract_fields_for_columns(
    fields: list[dict[str, Any]],
    prefix: str = "",
) -> list[dict[str, Any]]:
    expanded_fields: list[dict[str, Any]] = []
    for field in fields:
        if not isinstance(field, dict) or not field.get("name"):
            continue

        field_name = f"{prefix}{field['name']}" if prefix else str(field["name"])
        if field.get("type") == "array" and isinstance(field.get("items"), dict):
            item_fields = field["items"].get("fields", [])
            if isinstance(item_fields, list):
                expanded_fields.extend(
                    expand_contract_fields_for_columns(item_fields, prefix=f"{field_name}__")
                )
            continue

        flattened_field = dict(field)
        flattened_field["name"] = field_name
        expanded_fields.append(flattened_field)

    return expanded_fields


def parse_lineage_payload(path: Path) -> tuple[dict[str, Any] | list[Any] | None, str | None]:
    """Load the latest lineage snapshot from a file or directory."""
    if not path.exists():
        return None, None

    if path.is_dir():
        candidates = sorted(
            [
                candidate
                for candidate in path.rglob("*")
                if candidate.is_file()
                and candidate.suffix.lower() in {".json", ".jsonl", ".yaml", ".yml"}
            ],
            key=lambda candidate: candidate.stat().st_mtime,
            reverse=True,
        )
        if not candidates:
            return None, None
        path = candidates[0]

    if path.suffix.lower() == ".jsonl":
        lines = [
            line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
        ]
        payload = json.loads(lines[-1]) if lines else None
    else:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))

    try:
        relative = str(path.relative_to(PROJECT_ROOT))
    except ValueError:
        relative = str(path)
    return payload, relative


def extract_source_identifiers(
    contract_id: str, source_path: Path, records: list[dict[str, Any]]
) -> set[str]:
    """Build a robust identifier set for matching lineage sources to this contract."""
    identifiers = {
        contract_id,
        slugify(contract_id),
        source_path.stem,
        f"{source_path.parent.name}_{source_path.stem}",
    }
    for record in records[:50]:
        source_system = record.get("source_system")
        if isinstance(source_system, str) and source_system.strip():
            identifiers.add(source_system.strip())
    return {identifier.lower() for identifier in identifiers if identifier}


def lineage_downstream_nodes(
    lineage_payload: dict[str, Any] | list[Any] | None,
    contract_id: str,
    source_path: Path,
    records: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Pull downstream nodes for this contract from the latest Week 4 lineage snapshot."""
    if not isinstance(lineage_payload, dict):
        return []

    edges = lineage_payload.get("edges", [])
    if not isinstance(edges, list):
        return []

    identifiers = extract_source_identifiers(contract_id, source_path, records)
    downstream: list[dict[str, Any]] = []
    seen: set[tuple[str, str | None]] = set()

    for edge in edges:
        if not isinstance(edge, dict):
            continue
        source_value = edge.get("source") or edge.get("from")
        target_value = edge.get("target") or edge.get("to")
        relationship = edge.get("relationship")
        if not isinstance(source_value, str) or not isinstance(target_value, str):
            continue
        if source_value.lower() not in identifiers:
            continue
        if relationship and str(relationship).upper() not in {"PRODUCES", "WRITES"}:
            continue
        key = (target_value, str(relationship) if relationship is not None else None)
        if key in seen:
            continue
        seen.add(key)
        downstream.append(
            {
                "node_id": target_value,
                "relationship": relationship or "LINKED",
            }
        )

    return downstream


def normalize_contract_identity(value: str) -> str:
    """Normalize ids so hyphen/underscore variants compare cleanly."""
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def load_registry_subscribers(registry_path: Path, contract_id: str) -> list[str]:
    """Filter the registry down to the subscribers declared for this contract."""
    if not registry_path.exists():
        return []

    payload = yaml.safe_load(registry_path.read_text(encoding="utf-8")) or {}
    if not isinstance(payload, dict):
        return []

    subscriptions = payload.get("subscriptions", [])
    if not isinstance(subscriptions, list):
        return []

    contract_key = normalize_contract_identity(contract_id)
    subscribers: list[str] = []
    for subscription in subscriptions:
        if not isinstance(subscription, dict):
            continue
        subscription_contract = str(subscription.get("contract_id", ""))
        if normalize_contract_identity(subscription_contract) != contract_key:
            continue
        subscriber_id = subscription.get("subscriber_id")
        if isinstance(subscriber_id, str) and subscriber_id.strip():
            subscribers.append(subscriber_id.strip())
    return subscribers


def make_relative_path(path: Path) -> str:
    """Return project-relative paths when possible for human-readable YAML."""
    try:
        return str(path.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path)


def preset_for_source(source_path: Path) -> dict[str, Any]:
    key = make_relative_path(source_path)
    return SOURCE_PRESETS.get(
        key,
        {
            "contract_id": derive_contract_id(source_path),
            "output_name": derive_contract_id(source_path),
            "model_name": slugify(source_path.stem),
            "title": derive_contract_id(source_path),
            "owner": "data-engineering",
            "description": f"Auto-generated contract for {source_path.name}",
            "usage": "Internal data contract.",
            "limitations": "Consumers should validate schema compatibility before changes are deployed.",
        },
    )


def build_schema_map(model_fields: list[dict[str, Any]]) -> dict[str, Any]:
    schema: dict[str, Any] = {}
    for field in model_fields:
        if not isinstance(field, dict) or not field.get("name"):
            continue
        field_name = str(field["name"])
        entry = {key: value for key, value in field.items() if key != "name"}
        if entry.get("type") == "array" and isinstance(entry.get("items"), dict):
            item_fields = entry["items"].get("fields", [])
            if isinstance(item_fields, list):
                entry["items"] = {
                    str(item["name"]): {k: v for k, v in item.items() if k != "name"}
                    for item in item_fields
                    if isinstance(item, dict) and item.get("name")
                }
        schema[field_name] = entry
    return schema


def run_ydata_profile(frame: pd.DataFrame) -> dict[str, Any]:
    if frame.empty or ProfileReport is None:
        return {}
    try:
        report = ProfileReport(
            frame,
            minimal=True,
            correlations=None,
            progress_bar=False,
            explorative=False,
        )
        return {
            "engine": "ydata-profiling",
            "columns_profiled": int(frame.shape[1]),
            "rows_profiled": int(frame.shape[0]),
            "generated": bool(report is not None),
        }
    except Exception as exc:
        return {
            "engine": "ydata-profiling",
            "error": str(exc),
        }


def normalize_field_key(value: str) -> str:
    return str(value).replace(".", "__")


def load_registry_entries(registry_path: Path, contract_id: str) -> list[dict[str, Any]]:
    if not registry_path.exists():
        return []
    payload = yaml.safe_load(registry_path.read_text(encoding="utf-8")) or {}
    subscriptions = payload.get("subscriptions", []) if isinstance(payload, dict) else []
    contract_key = normalize_contract_identity(contract_id)
    return [
        entry
        for entry in subscriptions
        if isinstance(entry, dict)
        and normalize_contract_identity(str(entry.get("contract_id", ""))) == contract_key
    ]


def field_downstream_consumers(
    field_name: str,
    downstream_nodes: list[dict[str, Any]],
    registry_entries: list[dict[str, Any]],
) -> list[str]:
    consumers = {
        str(item.get("node_id"))
        for item in downstream_nodes
        if isinstance(item, dict) and item.get("node_id")
    }
    normalized_name = normalize_field_key(field_name)
    root_name = normalized_name.split("__", 1)[0]
    dot_name = normalized_name.replace("__", ".")

    for entry in registry_entries:
        subscriber_id = entry.get("subscriber_id")
        if not isinstance(subscriber_id, str) or not subscriber_id.strip():
            continue
        consumed = {
            normalize_field_key(item)
            for item in entry.get("fields_consumed", [])
            if isinstance(item, str)
        }
        breaking = {
            normalize_field_key(item.get("field"))
            for item in entry.get("breaking_fields", [])
            if isinstance(item, dict) and item.get("field")
        }
        if normalized_name in consumed or normalized_name in breaking:
            consumers.add(subscriber_id)
            continue
        if root_name in consumed or root_name in breaking:
            consumers.add(subscriber_id)
            continue
        if dot_name in {item.replace("__", ".") for item in consumed | breaking}:
            consumers.add(subscriber_id)
    return sorted(consumers)


def build_lineage_section(
    contract_id: str,
    source_path: Path,
    lineage_snapshot_ref: str | None,
    downstream_nodes: list[dict[str, Any]],
    registry_path: Path,
    registry_entries: list[dict[str, Any]],
) -> dict[str, Any]:
    downstream_registry = []
    for entry in registry_entries:
        subscriber_id = entry.get("subscriber_id")
        if not isinstance(subscriber_id, str) or not subscriber_id.strip():
            continue
        fields_consumed = [item for item in entry.get("fields_consumed", []) if isinstance(item, str)]
        breaking_if_changed = [
            item.get("field")
            for item in entry.get("breaking_fields", [])
            if isinstance(item, dict) and isinstance(item.get("field"), str)
        ]
        reason_text = "; ".join(
            str(item.get("reason"))
            for item in entry.get("breaking_fields", [])
            if isinstance(item, dict) and item.get("reason")
        )
        downstream_registry.append(
            {
                "id": subscriber_id,
                "description": reason_text or f"Subscriber {subscriber_id} consumes fields from {contract_id}.",
                "fields_consumed": fields_consumed,
                "breaking_if_changed": breaking_if_changed,
                "validation_mode": entry.get("validation_mode"),
            }
        )

    return {
        "source": make_relative_path(source_path),
        "lineage_snapshot": lineage_snapshot_ref,
        "registry": make_relative_path(registry_path),
        "upstream": [],
        "downstream": downstream_registry,
        "downstream_consumers_from_graph": downstream_nodes,
    }


def build_quality_spec(model_name: str, fields: list[dict[str, Any]]) -> dict[str, Any]:
    checks = [f"- row_count >= 1"]
    for field in fields:
        if not isinstance(field, dict):
            continue
        name = field.get("name")
        if not isinstance(name, str):
            continue
        if field.get("required") is True:
            checks.append(f"- missing_count({name}) = 0")
        if field.get("format") == "uuid":
            checks.append(f"- duplicate_count({name}) = 0")
        if field.get("minimum") is not None:
            checks.append(f"- min({name}) >= {field['minimum']}")
        if field.get("maximum") is not None:
            checks.append(f"- max({name}) <= {field['maximum']}")
        if "confidence" in name.lower():
            checks.append(f"- avg({name}) >= 0.0")
            checks.append(f"- avg({name}) <= 1.0")
    return {
        "type": "SodaChecks",
        "specification": {f"checks for {model_name}": checks},
    }


def build_llm_annotations(contract_id: str, model_fields: list[dict[str, Any]]) -> list[dict[str, Any]]:
    def is_ambiguous(field: dict[str, Any]) -> bool:
        leaf_name = str(field.get("name", "")).split("__")[-1]
        metrics = field.get("metrics", {}) if isinstance(field.get("metrics"), dict) else {}
        cardinality = int(metrics.get("cardinality", 0) or 0)
        return (
            leaf_name in {"value", "type", "status", "source", "target"}
            or (field.get("type") == "string" and cardinality > 20 and leaf_name not in {"doc_id", "event_id"})
        )

    def anthropic_client() -> Any | None:
        api_key = os.getenv("ANTHROPIC_API_KEY") or os.getenv("API_KEY")
        if not api_key or anthropic is None:
            return None
        try:
            return anthropic.Anthropic(api_key=api_key)
        except Exception:
            return None

    def parse_llm_text(raw_text: str) -> dict[str, str] | None:
        text = raw_text.strip()
        if "```json" in text:
            text = text.split("```json", 1)[1].split("```", 1)[0].strip()
        elif text.startswith("```"):
            text = text.split("```", 1)[1].split("```", 1)[0].strip()
        try:
            payload = json.loads(text)
        except Exception:
            return None
        if not isinstance(payload, dict):
            return None
        return {
            "plain_english_description": str(payload.get("plain_english_description", "")).strip(),
            "validation_expression": str(payload.get("validation_expression", "")).strip(),
            "cross_column_relationship": str(payload.get("cross_column_relationship", "")).strip(),
        }

    def annotate_with_claude(client: Any, field: dict[str, Any], adjacent_names: list[str]) -> dict[str, str] | None:
        field_name = str(field.get("name", ""))
        prompt = (
            "You are documenting a data contract column. Return JSON with keys "
            'plain_english_description, validation_expression, cross_column_relationship. '
            f"Table/contract: {contract_id}. Field: {field_name}. "
            f"Sample values: {field.get('metrics', {}).get('cardinality')} cardinality; "
            f"adjacent columns: {', '.join(adjacent_names[:8])}. "
            f"Existing description: {field.get('description', '')}"
        )
        try:
            response = client.messages.create(
                model=os.getenv("ANTHROPIC_MODEL", "claude-3-5-sonnet-latest"),
                max_tokens=300,
                temperature=0,
                messages=[{"role": "user", "content": prompt}],
            )
        except Exception:
            return None
        response_text_parts = []
        for block in getattr(response, "content", []):
            text_value = getattr(block, "text", None)
            if isinstance(text_value, str):
                response_text_parts.append(text_value)
        if not response_text_parts:
            return None
        return parse_llm_text("\n".join(response_text_parts))

    annotations: list[dict[str, Any]] = []
    client = anthropic_client()
    adjacent_names = [str(field.get("name", "")) for field in model_fields if isinstance(field, dict)]
    for field in model_fields:
        if not isinstance(field, dict):
            continue
        field_name = str(field.get("name", ""))
        if not is_ambiguous(field):
            continue
        llm_payload = annotate_with_claude(client, field, adjacent_names) if client is not None else None
        annotations.append(
            {
                "field": field_name,
                "plain_english_description": (
                    llm_payload.get("plain_english_description")
                    if llm_payload and llm_payload.get("plain_english_description")
                    else field.get("description")
                ),
                "validation_expression": (
                    llm_payload.get("validation_expression")
                    if llm_payload and llm_payload.get("validation_expression")
                    else f"{field_name} must remain semantically consistent within contract {contract_id}."
                ),
                "cross_column_relationship": (
                    llm_payload.get("cross_column_relationship")
                    if llm_payload and llm_payload.get("cross_column_relationship")
                    else "Review alongside adjacent metadata columns during contract review."
                ),
                "source": "claude" if llm_payload else "heuristic_fallback",
            }
        )
    return annotations


def build_bitol_contract(
    contract_id: str,
    model_name: str,
    source_path: Path,
    profiles: list[dict[str, Any]],
    lineage_snapshot_ref: str | None,
    downstream_nodes: list[dict[str, Any]],
    registry_path: Path,
    registry_entries: list[dict[str, Any]],
    profile_summary: dict[str, Any],
) -> dict[str, Any]:
    """Assemble the final Bitol YAML document."""
    preset = preset_for_source(source_path)
    model_fields = build_model_fields(profiles, contract_id)
    expanded_fields = expand_contract_fields_for_columns(model_fields)
    for field in expanded_fields:
        field["downstream_consumers"] = field_downstream_consumers(
            str(field.get("name", "")), downstream_nodes, registry_entries
        )
        if str(field.get("name", "")).endswith("_id") and field.get("required") is True:
            field["unique"] = bool(
                field.get("metrics", {}).get("cardinality")
                == next(
                    (
                        profile.get("non_null_count")
                        for profile in profiles
                        if str(profile.get("name")) == str(field.get("name"))
                    ),
                    None,
                )
            )
        if "confidence" in str(field.get("name", "")).lower():
            mean_value = field.get("metrics", {}).get("mean")
            quality_flags: list[str] = []
            if isinstance(mean_value, (int, float)) and mean_value > 0.99:
                quality_flags.append("mean_above_0_99_possible_clamped_distribution")
            if isinstance(mean_value, (int, float)) and mean_value < 0.01:
                quality_flags.append("mean_below_0_01_possible_broken_distribution")
            if quality_flags:
                field["quality_flags"] = quality_flags
    schema_map = build_schema_map(model_fields)
    for field in expanded_fields:
        name = str(field.get("name", ""))
        if name in schema_map:
            schema_map[name]["downstream_consumers"] = field.get("downstream_consumers", [])
            if field.get("unique") is True:
                schema_map[name]["unique"] = True
            if field.get("quality_flags"):
                schema_map[name]["quality_flags"] = field["quality_flags"]
    return {
        "kind": "DataContract",
        "apiVersion": "v3.0.0",
        "dataContractSpecification": "1.1.0",
        "id": contract_id,
        "info": {
            "title": preset["title"],
            "version": "1.0.0",
            "description": preset["description"],
            "owner": preset["owner"],
        },
        "servers": {
            "local": {
                "type": "local",
                "path": make_relative_path(source_path),
                "format": "jsonl",
            }
        },
        "terms": {
            "usage": preset["usage"],
            "limitations": preset["limitations"],
        },
        "schema": schema_map,
        "quality": build_quality_spec(model_name, expanded_fields),
        "lineage": build_lineage_section(
            contract_id,
            source_path,
            lineage_snapshot_ref,
            downstream_nodes,
            registry_path,
            registry_entries,
        ),
        "profiling": profile_summary,
        "llm_annotations": build_llm_annotations(contract_id, expanded_fields),
        "models": [
            {
                "name": model_name,
                "type": "table",
                "fields": model_fields,
            }
        ],
    }


def build_dbt_schema(contract: dict[str, Any], source_path: Path) -> dict[str, Any]:
    """Emit a companion dbt schema.yml with tests for required and enum fields."""
    preset = preset_for_source(source_path)
    models = contract.get("models", [])
    model = models[0] if isinstance(models, list) and models else {}
    model_fields = model.get("fields", []) if isinstance(model, dict) else []
    fields = expand_contract_fields_for_columns(model_fields if isinstance(model_fields, list) else [])
    columns: list[dict[str, Any]] = []

    for field in fields:
        if not isinstance(field, dict):
            continue
        tests: list[Any] = []
        if field.get("required") is True:
            tests.append("not_null")
        if field.get("enum"):
            tests.append({"accepted_values": {"values": field["enum"]}})
        if field.get("unique") is True:
            tests.append("unique")
        if str(field.get("name", "")).endswith("_id"):
            candidate_keys = {
                str(other.get("name"))
                for other in fields
                if isinstance(other, dict) and other.get("name")
            }
            if "doc_id" in candidate_keys and field.get("name") == "source_doc_id":
                tests.append({"relationships": {"to": "ref('week3_extractions')", "field": "doc_id"}})

        column_entry = {
            "name": field["name"],
            "description": f"Auto-generated from {source_path.name}",
            "data_type": field.get("type", "string"),
        }
        if tests:
            column_entry["tests"] = tests
        columns.append(column_entry)

    return {
        "version": 2,
        "models": [
            {
                "name": str(preset.get("output_name", slugify(str(contract.get("id", source_path.stem))))),
                "description": f"Generated schema contract for {contract.get('id', source_path.stem)}",
                "columns": columns,
            }
        ],
    }


def resolve_output_paths(output_arg: str, contract_id: str) -> tuple[Path, Path]:
    """Resolve the primary Bitol YAML path and the companion dbt schema path."""
    output_path = Path(output_arg)
    file_slug = slugify(contract_id)
    if output_path.suffix.lower() in {".yaml", ".yml"}:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        return output_path, output_path.parent / f"{file_slug}_dbt.yml"

    output_path.mkdir(parents=True, exist_ok=True)
    return output_path / f"{file_slug}.yaml", output_path / f"{file_slug}_dbt.yml"


def resolve_output_paths_for_preset(output_arg: str, output_name: str) -> tuple[Path, Path]:
    output_path = Path(output_arg)
    if output_path.suffix.lower() in {".yaml", ".yml"}:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        return output_path, output_path.parent / f"{output_name}_dbt.yml"
    output_path.mkdir(parents=True, exist_ok=True)
    return output_path / f"{output_name}.yaml", output_path / f"{output_name}_dbt.yml"


def write_yaml(target_path: Path, payload: dict[str, Any]) -> None:
    """Write YAML using stable, human-friendly formatting."""
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_text(
        yaml.safe_dump(payload, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )


def build_snapshot_payload(
    contract_payload: dict[str, Any],
    contract_id: str,
    primary_contract_path: Path,
) -> dict[str, Any]:
    """Persist a disciplined schema snapshot with enough context for later diffs."""
    return {
        "snapshot_version": "1.0",
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "contract_id": contract_id,
        "source_contract_path": make_relative_path(primary_contract_path),
        "info": contract_payload.get("info", {}),
        "servers": contract_payload.get("servers", {}),
        "schema": contract_payload.get("schema", {}),
        "models": contract_payload.get("models", []),
        "lineage": contract_payload.get("lineage", {}),
    }


def write_snapshot(primary_contract_path: Path, contract_payload: dict[str, Any], contract_id: str) -> Path:
    """Save a timestamped schema snapshot for future schema-evolution analysis."""
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    snapshot_dir = PROJECT_ROOT / "schema_snapshots" / contract_id
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    snapshot_path = snapshot_dir / f"{timestamp}.yaml"
    snapshot_payload = build_snapshot_payload(contract_payload, contract_id, primary_contract_path)
    write_yaml(snapshot_path, snapshot_payload)
    return snapshot_path


def ensure_trace_source(path: Path) -> None:
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    seed_record = {
        "session_id": "week7-baseline-session",
        "run_type": "baseline",
        "total_tokens": 0,
        "total_cost": 0.0,
        "source": "synthetic_bootstrap",
    }
    path.write_text(json.dumps(seed_record, ensure_ascii=False) + "\n", encoding="utf-8")


def generate_contract_for_source(source_path: Path, output_dir: Path, lineage_path: Path, registry_path: Path) -> tuple[Path, Path, Path]:
    preset = preset_for_source(source_path)
    contract_id = str(preset["contract_id"])
    output_name = str(preset.get("output_name", slugify(contract_id)))
    model_name = str(preset.get("model_name", slugify(contract_id)))
    records = load_records(source_path)
    profile_frame = flatten_for_profile(records)
    profile_summary = run_ydata_profile(profile_frame)
    profiles = profile_records(profile_frame)

    lineage_payload, lineage_snapshot_ref = parse_lineage_payload(lineage_path)
    downstream_nodes = lineage_downstream_nodes(lineage_payload, contract_id, source_path, records)
    registry_entries = load_registry_entries(registry_path, contract_id)

    bitol_contract = build_bitol_contract(
        contract_id=contract_id,
        model_name=model_name,
        source_path=source_path,
        profiles=profiles,
        lineage_snapshot_ref=lineage_snapshot_ref,
        downstream_nodes=downstream_nodes,
        registry_path=registry_path,
        registry_entries=registry_entries,
        profile_summary=profile_summary,
    )
    dbt_schema = build_dbt_schema(bitol_contract, source_path)
    bitol_path, dbt_path = resolve_output_paths_for_preset(str(output_dir), output_name)
    write_yaml(bitol_path, bitol_contract)
    write_yaml(dbt_path, dbt_schema)
    snapshot_path = write_snapshot(bitol_path, bitol_contract, contract_id)
    return bitol_path, dbt_path, snapshot_path


def main() -> None:
    args = parse_args()
    lineage_path = Path(args.lineage)
    registry_path = Path(args.registry)
    output_path = Path(args.output)

    if args.all:
        sources = [
            PROJECT_ROOT / "outputs" / "week1" / "intent_records.jsonl",
            PROJECT_ROOT / "outputs" / "week3" / "extractions.jsonl",
            PROJECT_ROOT / "outputs" / "week4" / "lineage_snapshots.jsonl",
            PROJECT_ROOT / "outputs" / "week5" / "events.jsonl",
            PROJECT_ROOT / "outputs" / "traces" / "runs.jsonl",
        ]
        ensure_trace_source(PROJECT_ROOT / "outputs" / "traces" / "runs.jsonl")
        for source_path in sources:
            if not source_path.exists():
                continue
            bitol_path, dbt_path, snapshot_path = generate_contract_for_source(
                source_path, output_path, lineage_path, registry_path
            )
            print(f"Wrote Bitol contract to {bitol_path}")
            print(f"Wrote dbt schema to {dbt_path}")
            print(f"Wrote schema snapshot to {snapshot_path}")
        return

    if not args.source:
        raise SystemExit("--source is required unless --all is provided.")

    source_path = Path(args.source)
    if make_relative_path(source_path) == "outputs/traces/runs.jsonl":
        ensure_trace_source(source_path)
    contract_id = args.contract_id or str(preset_for_source(source_path)["contract_id"])
    bitol_path, dbt_path = resolve_output_paths(args.output, contract_id)
    bitol_path, dbt_path, snapshot_path = generate_contract_for_source(
        source_path,
        bitol_path.parent if bitol_path.suffix.lower() in {".yaml", ".yml"} else bitol_path,
        lineage_path,
        registry_path,
    )

    print(f"Wrote Bitol contract to {bitol_path}")
    print(f"Wrote dbt schema to {dbt_path}")
    print(f"Wrote schema snapshot to {snapshot_path}")


if __name__ == "__main__":
    main()
