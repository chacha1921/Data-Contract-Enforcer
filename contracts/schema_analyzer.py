from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

import yaml


PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_SNAPSHOTS_DIR = PROJECT_ROOT / "schema_snapshots"
REGISTRY_PATH = PROJECT_ROOT / "contract_registry" / "subscriptions.yaml"
LINEAGE_PATH = PROJECT_ROOT / "outputs" / "week4" / "lineage_snapshots.jsonl"
TIMESTAMP_PATTERN = "%Y%m%d_%H%M%S"
SINCE_PATTERN = re.compile(r"^(?P<count>\d+)\s+(?P<unit>minute|minutes|hour|hours|day|days|week|weeks)\s+ago$", re.IGNORECASE)
WIDENING_TYPE_PAIRS = {("integer", "number")}
NARROWING_TYPE_PAIRS = {("number", "integer")}


@dataclass(frozen=True)
class FieldSignature:
    name: str
    field_type: str
    required: bool
    format: str | None
    enum_values: tuple[str, ...]
    minimum: float | None
    maximum: float | None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Analyze schema evolution across consecutive contract snapshots."
    )
    parser.add_argument("--contract-id", required=True, help="Contract identifier to analyze.")
    parser.add_argument(
        "--since",
        required=False,
        help='Optional time window such as "7 days ago" or an ISO timestamp.',
    )
    parser.add_argument("--output", required=True, help="Path to the schema evolution JSON report.")
    return parser.parse_args()


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def datetime_now_iso() -> str:
    return now_utc().isoformat()


def slugify(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")


def normalize_identifier(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value).lower())


def normalize_field_name(value: str) -> str:
    text = str(value).replace("[*]", "")
    text = text.replace("__", ".")
    text = text.replace("..", ".")
    return text.strip(".")


def snapshot_dir(contract_id: str) -> Path:
    return SCHEMA_SNAPSHOTS_DIR / contract_id


def parse_snapshot_timestamp(path: Path) -> datetime:
    timestamp = datetime.strptime(path.stem, TIMESTAMP_PATTERN)
    return timestamp.replace(tzinfo=timezone.utc)


def parse_since(value: str | None) -> datetime | None:
    if not value:
        return None
    candidate = value.strip()
    matched = SINCE_PATTERN.fullmatch(candidate)
    if matched:
        count = int(matched.group("count"))
        unit = matched.group("unit").lower()
        if unit.startswith("minute"):
            delta = timedelta(minutes=count)
        elif unit.startswith("hour"):
            delta = timedelta(hours=count)
        elif unit.startswith("day"):
            delta = timedelta(days=count)
        else:
            delta = timedelta(weeks=count)
        return now_utc() - delta

    normalized = candidate.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def available_snapshots(contract_id: str) -> list[Path]:
    directory = snapshot_dir(contract_id)
    if not directory.exists():
        raise FileNotFoundError(f"Snapshot directory not found: {directory}")
    snapshots = sorted(
        [path for path in directory.iterdir() if path.is_file() and path.suffix in {".yaml", ".yml"}],
        key=parse_snapshot_timestamp,
    )
    if len(snapshots) < 2:
        raise ValueError(f"Need at least two snapshots in {directory} to analyze evolution.")
    return snapshots


def resolve_snapshot_pairs(contract_id: str, since: str | None) -> list[tuple[Path, Path]]:
    snapshots = available_snapshots(contract_id)
    since_dt = parse_since(since)
    if since_dt is None:
        return [(snapshots[-2], snapshots[-1])]

    pairs: list[tuple[Path, Path]] = []
    for index in range(1, len(snapshots)):
        previous_snapshot = snapshots[index - 1]
        current_snapshot = snapshots[index]
        if parse_snapshot_timestamp(current_snapshot) >= since_dt:
            pairs.append((previous_snapshot, current_snapshot))

    if not pairs:
        raise ValueError(
            f"No consecutive snapshots found for {contract_id} since {since_dt.isoformat()}."
        )
    return pairs


def load_yaml(path: Path) -> dict[str, Any]:
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Expected top-level YAML object in {path}")
    return payload


def load_registry() -> list[dict[str, Any]]:
    if not REGISTRY_PATH.exists():
        return []
    payload = yaml.safe_load(REGISTRY_PATH.read_text(encoding="utf-8")) or {}
    subscriptions = payload.get("subscriptions", []) if isinstance(payload, dict) else []
    return [item for item in subscriptions if isinstance(item, dict)]


def load_lineage_edges() -> list[dict[str, Any]]:
    if not LINEAGE_PATH.exists():
        return []
    lines = [line.strip() for line in LINEAGE_PATH.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not lines:
        return []
    payload = json.loads(lines[-1])
    edges = payload.get("edges", []) if isinstance(payload, dict) else []
    return [edge for edge in edges if isinstance(edge, dict)]


def make_relative_path(path: Path) -> str:
    try:
        return str(path.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path)


def coerce_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def flatten_model_fields(fields: list[dict[str, Any]], prefix: str = "") -> dict[str, FieldSignature]:
    flattened: dict[str, FieldSignature] = {}
    for field in fields:
        if not isinstance(field, dict) or not field.get("name"):
            continue
        name = f"{prefix}{field['name']}" if prefix else str(field["name"])
        if field.get("type") == "array" and isinstance(field.get("items"), dict):
            item_fields = field["items"].get("fields", [])
            if isinstance(item_fields, list):
                flattened.update(flatten_model_fields(item_fields, prefix=f"{name}__"))
            continue
        enum_values = tuple(sorted(str(item) for item in field.get("enum", []) if item is not None))
        flattened[name] = FieldSignature(
            name=name,
            field_type=str(field.get("type", "string")),
            required=bool(field.get("required", False)),
            format=str(field.get("format")) if field.get("format") is not None else None,
            enum_values=enum_values,
            minimum=coerce_float(field.get("minimum")),
            maximum=coerce_float(field.get("maximum")),
        )
    return flattened


def flatten_schema_entries(schema: dict[str, Any], prefix: str = "") -> dict[str, FieldSignature]:
    flattened: dict[str, FieldSignature] = {}
    for field_name, entry in schema.items():
        if not isinstance(entry, dict):
            continue
        name = f"{prefix}{field_name}" if prefix else str(field_name)
        if entry.get("type") == "array" and isinstance(entry.get("items"), dict):
            item_payload = entry["items"]
            item_fields = item_payload.get("fields")
            if isinstance(item_fields, list):
                flattened.update(flatten_model_fields(item_fields, prefix=f"{name}__"))
                continue
            nested_schema = {
                key: value
                for key, value in item_payload.items()
                if key not in {"type", "description"}
            }
            if nested_schema:
                flattened.update(flatten_schema_entries(nested_schema, prefix=f"{name}__"))
            continue
        enum_values = tuple(sorted(str(item) for item in entry.get("enum", []) if item is not None))
        flattened[name] = FieldSignature(
            name=name,
            field_type=str(entry.get("type", "string")),
            required=bool(entry.get("required", False)),
            format=str(entry.get("format")) if entry.get("format") is not None else None,
            enum_values=enum_values,
            minimum=coerce_float(entry.get("minimum")),
            maximum=coerce_float(entry.get("maximum")),
        )
    return flattened


def extract_schema(payload: dict[str, Any]) -> dict[str, FieldSignature]:
    models = payload.get("models", [])
    if isinstance(models, list) and models:
        extracted: dict[str, FieldSignature] = {}
        for model in models:
            if not isinstance(model, dict):
                continue
            fields = model.get("fields", [])
            if not isinstance(fields, list):
                continue
            extracted.update(flatten_model_fields(fields))
        if extracted:
            return extracted

    schema = payload.get("schema", {})
    if isinstance(schema, dict):
        return flatten_schema_entries(schema)
    return {}


def similarity(left: str, right: str) -> float:
    return SequenceMatcher(a=left, b=right).ratio()


def human_field_name(field_name: str) -> str:
    return normalize_field_name(field_name)


def type_change_category(before_type: str, after_type: str) -> tuple[str, str, str, str]:
    pair = (before_type, after_type)
    if pair in WIDENING_TYPE_PAIRS:
        return (
            "widen_type",
            "COMPATIBLE",
            "Validate no precision loss. Re-run statistical checks to confirm distribution unchanged.",
            "Confluent FULL mode: allows. Most tools: pass silently.",
        )
    if pair in NARROWING_TYPE_PAIRS:
        return (
            "narrow_type",
            "BREAKING",
            "CRITICAL. Requires migration plan with rollback. Registry blast radius report mandatory. Statistical baseline must be re-established after migration.",
            "Confluent FORWARD mode: blocks. Great Expectations: catches via distribution check.",
        )
    return (
        "change_type",
        "BREAKING",
        "Coordinate producers and consumers before deployment. Treat as incompatible unless a dual-write or cast strategy exists.",
        "Confluent compatibility modes typically block incompatible type changes.",
    )


def range_change_category(field_name: str, before: FieldSignature, after: FieldSignature) -> tuple[str, str, str, str] | None:
    if before.minimum == after.minimum and before.maximum == after.maximum:
        return None
    field_key = field_name.lower()
    if "confidence" in field_key and (before.maximum or 0.0) <= 1.0 and (after.maximum or 0.0) > 1.0:
        return (
            "confidence_scale_change",
            "BREAKING",
            "CRITICAL. Requires migration plan with rollback. Registry blast radius report mandatory. Statistical baseline must be re-established after migration.",
            "Confluent compatibility checks miss semantic scale shifts; distribution checks and contract review must block deploy.",
        )

    before_min = before.minimum if before.minimum is not None else float("-inf")
    before_max = before.maximum if before.maximum is not None else float("inf")
    after_min = after.minimum if after.minimum is not None else float("-inf")
    after_max = after.maximum if after.maximum is not None else float("inf")
    if after_min <= before_min and after_max >= before_max:
        return (
            "widen_constraints",
            "COMPATIBLE",
            "Validate downstream assumptions and re-run statistical checks to confirm the widened range is intentional.",
            "Registry tools usually allow widened numeric bounds unless an explicit validation rule blocks them.",
        )
    return (
        "narrow_constraints",
        "BREAKING",
        "Coordinate a migration window and confirm all subscribers can tolerate the narrower value range before deployment.",
        "Contract validation tools treat tighter bounds as breaking when existing payloads can fall outside the new range.",
    )


def build_change(
    *,
    change_type: str,
    compatibility: str,
    field: str | None = None,
    previous_field: str | None = None,
    current_field: str | None = None,
    summary: str,
    required_action: str,
    real_tool_handling: str,
    details: dict[str, Any],
) -> dict[str, Any]:
    payload = {
        "change_type": change_type,
        "compatibility": compatibility,
        "summary": summary,
        "human_diff": summary,
        "required_action": required_action,
        "real_tool_handling": real_tool_handling,
        "details": details,
    }
    if field is not None:
        payload["field"] = human_field_name(field)
    if previous_field is not None:
        payload["from"] = human_field_name(previous_field)
    if current_field is not None:
        payload["to"] = human_field_name(current_field)
    return payload


def rename_pairs(
    removed: dict[str, FieldSignature], added: dict[str, FieldSignature]
) -> tuple[list[dict[str, Any]], set[str], set[str]]:
    changes: list[dict[str, Any]] = []
    used_removed: set[str] = set()
    used_added: set[str] = set()

    for old_name, old_sig in removed.items():
        best_name: str | None = None
        best_score = 0.0
        for new_name, new_sig in added.items():
            if new_name in used_added:
                continue
            if old_sig != new_sig:
                continue
            score = similarity(old_name, new_name)
            if score > best_score:
                best_score = score
                best_name = new_name
        if best_name is None or best_score < 0.45:
            continue
        used_removed.add(old_name)
        used_added.add(best_name)
        changes.append(
            build_change(
                change_type="rename_field",
                compatibility="BREAKING",
                previous_field=old_name,
                current_field=best_name,
                summary=(
                    f"Rename field {human_field_name(old_name)} -> {human_field_name(best_name)}. "
                    "Provide a deprecation period with an alias before removing the old name."
                ),
                required_action="Deprecation period with alias. Notify all registry subscribers. One sprint minimum before alias removal.",
                real_tool_handling="Confluent blocks. dbt: manual. Pact: consumer pact fails immediately.",
                details={
                    "type": old_sig.field_type,
                    "required": old_sig.required,
                    "format": old_sig.format,
                },
            )
        )
    return changes, used_removed, used_added


def diff_schemas(previous: dict[str, FieldSignature], current: dict[str, FieldSignature]) -> list[dict[str, Any]]:
    removed = {name: sig for name, sig in previous.items() if name not in current}
    added = {name: sig for name, sig in current.items() if name not in previous}
    changes, used_removed, used_added = rename_pairs(removed, added)

    for name, signature in sorted(added.items()):
        if name in used_added:
            continue
        if signature.required:
            changes.append(
                build_change(
                    change_type="add_required_field",
                    compatibility="BREAKING",
                    field=name,
                    summary=f"Add required field {human_field_name(name)}.",
                    required_action="Coordinate with all producers. Provide default or migration script. Block deploy until all producers updated.",
                    real_tool_handling="Confluent BACKWARD mode: blocks.",
                    details={
                        "type": signature.field_type,
                        "required": signature.required,
                        "format": signature.format,
                    },
                )
            )
        else:
            changes.append(
                build_change(
                    change_type="add_nullable_field",
                    compatibility="COMPATIBLE",
                    field=name,
                    summary=f"Add nullable field {human_field_name(name)}.",
                    required_action="None. Consumers can ignore new fields.",
                    real_tool_handling="Confluent BACKWARD mode: allows.",
                    details={
                        "type": signature.field_type,
                        "required": signature.required,
                        "format": signature.format,
                    },
                )
            )

    for name, signature in sorted(removed.items()):
        if name in used_removed:
            continue
        changes.append(
            build_change(
                change_type="remove_field",
                compatibility="BREAKING",
                field=name,
                summary=f"Remove field {human_field_name(name)}.",
                required_action="Two-sprint deprecation minimum. Each registry subscriber must acknowledge removal. No silent drops.",
                real_tool_handling="Confluent blocks. Pact: consumer pact fails if field was declared.",
                details={
                    "type": signature.field_type,
                    "required": signature.required,
                    "format": signature.format,
                },
            )
        )

    for name in sorted(set(previous.keys()) & set(current.keys())):
        before = previous[name]
        after = current[name]
        if before.field_type != after.field_type:
            change_type, compatibility, required_action, real_tool_handling = type_change_category(
                before.field_type,
                after.field_type,
            )
            changes.append(
                build_change(
                    change_type=change_type,
                    compatibility=compatibility,
                    field=name,
                    summary=(
                        f"Change type for {human_field_name(name)} from {before.field_type} to {after.field_type}."
                    ),
                    required_action=required_action,
                    real_tool_handling=real_tool_handling,
                    details={"from": before.field_type, "to": after.field_type},
                )
            )

        if before.required != after.required:
            if before.required is False and after.required is True:
                changes.append(
                    build_change(
                        change_type="add_required_field",
                        compatibility="BREAKING",
                        field=name,
                        summary=f"Field {human_field_name(name)} changed from nullable to required.",
                        required_action="Coordinate with all producers. Provide default or migration script. Block deploy until all producers updated.",
                        real_tool_handling="Confluent BACKWARD mode: blocks.",
                        details={"from": before.required, "to": after.required},
                    )
                )
            else:
                changes.append(
                    build_change(
                        change_type="relax_required_field",
                        compatibility="COMPATIBLE",
                        field=name,
                        summary=f"Field {human_field_name(name)} changed from required to nullable.",
                        required_action="Notify subscribers so they can decide whether to harden null handling.",
                        real_tool_handling="Compatibility checks usually allow relaxing requiredness.",
                        details={"from": before.required, "to": after.required},
                    )
                )

        if before.format != after.format:
            changes.append(
                build_change(
                    change_type="change_format",
                    compatibility="BREAKING",
                    field=name,
                    summary=(
                        f"Change format for {human_field_name(name)} from {before.format} to {after.format}."
                    ),
                    required_action="Coordinate consumer parsing updates before release.",
                    real_tool_handling="Format-level compatibility generally requires manual consumer validation.",
                    details={"from": before.format, "to": after.format},
                )
            )

        if before.enum_values != after.enum_values:
            added_values = sorted(set(after.enum_values) - set(before.enum_values))
            removed_values = sorted(set(before.enum_values) - set(after.enum_values))
            if removed_values:
                changes.append(
                    build_change(
                        change_type="change_enum_values",
                        compatibility="BREAKING",
                        field=name,
                        summary=(
                            f"Remove enum values {removed_values} from {human_field_name(name)}"
                            + (f" and add {added_values}." if added_values else ".")
                        ),
                        required_action="Removal of existing value: treat as breaking — blast radius required.",
                        real_tool_handling="Confluent BACKWARD: blocks removals.",
                        details={
                            "added_values": added_values,
                            "removed_values": removed_values,
                        },
                    )
                )
            elif added_values:
                changes.append(
                    build_change(
                        change_type="change_enum_values",
                        compatibility="COMPATIBLE",
                        field=name,
                        summary=f"Add enum values {added_values} to {human_field_name(name)}.",
                        required_action="Additive additions: notify subscribers.",
                        real_tool_handling="Confluent BACKWARD: allows additions, blocks removals.",
                        details={"added_values": added_values, "removed_values": []},
                    )
                )

        range_change = range_change_category(name, before, after)
        if range_change is not None:
            change_type, compatibility, required_action, real_tool_handling = range_change
            changes.append(
                build_change(
                    change_type=change_type,
                    compatibility=compatibility,
                    field=name,
                    summary=(
                        f"Change numeric constraints for {human_field_name(name)} from "
                        f"[{before.minimum}, {before.maximum}] to [{after.minimum}, {after.maximum}]."
                    ),
                    required_action=required_action,
                    real_tool_handling=real_tool_handling,
                    details={
                        "from": {"minimum": before.minimum, "maximum": before.maximum},
                        "to": {"minimum": after.minimum, "maximum": after.maximum},
                    },
                )
            )

    return sorted(
        changes,
        key=lambda item: (
            item["compatibility"] != "BREAKING",
            item.get("field", item.get("from", "")),
            item["change_type"],
        ),
    )


def failure_mode_for_change(change: dict[str, Any]) -> str:
    change_type = str(change.get("change_type", ""))
    field_name = str(change.get("field") or change.get("from") or "field")
    if change_type == "rename_field":
        return f"Consumers referencing {field_name} fail until an alias or field mapping is introduced."
    if change_type == "remove_field":
        return f"Consumers expecting {field_name} receive missing data and may fail immediately."
    if change_type == "add_required_field":
        return f"Writers that do not populate {field_name} fail validation as soon as the new schema is enforced."
    if change_type in {"narrow_type", "confidence_scale_change", "narrow_constraints"}:
        return f"{field_name} can no longer be safely coerced without loss or semantic drift, risking silent corruption."
    if change_type == "change_enum_values":
        return f"Branching logic and accepted-values checks tied to {field_name} can reject new or removed enum values."
    return f"{field_name} requires consumer review before deployment."


def changed_field_candidates(change: dict[str, Any]) -> set[str]:
    candidates: set[str] = set()
    for key in ("field", "from", "to"):
        value = change.get(key)
        if isinstance(value, str):
            normalized = normalize_field_name(value)
            candidates.add(normalized)
            candidates.add(normalized.split(".", 1)[0])
    return {candidate for candidate in candidates if candidate}


def contract_aliases(contract_id: str, payload: dict[str, Any]) -> set[str]:
    aliases = {normalize_identifier(contract_id)}
    if isinstance(payload.get("contract_id"), str):
        aliases.add(normalize_identifier(str(payload["contract_id"])))
    if isinstance(payload.get("id"), str):
        aliases.add(normalize_identifier(str(payload["id"])))

    servers = payload.get("servers", {}) if isinstance(payload.get("servers"), dict) else {}
    local = servers.get("local", {}) if isinstance(servers.get("local"), dict) else {}
    path_text = local.get("path")
    if isinstance(path_text, str) and path_text.strip():
        source_path = Path(path_text)
        aliases.add(normalize_identifier(source_path.stem))
        aliases.add(normalize_identifier(f"{source_path.parent.name}_{source_path.stem}"))

    source_contract_path = payload.get("source_contract_path")
    if isinstance(source_contract_path, str) and source_contract_path.strip():
        contract_path = Path(source_contract_path)
        aliases.add(normalize_identifier(contract_path.stem))
    return {alias for alias in aliases if alias}


def lineage_blast_radius(contract_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    edges = load_lineage_edges()
    start_aliases = contract_aliases(contract_id, payload)
    graph: dict[str, list[str]] = {}
    label_lookup: dict[str, str] = {}
    for edge in edges:
        source = edge.get("source") or edge.get("from")
        target = edge.get("target") or edge.get("to")
        if not isinstance(source, str) or not isinstance(target, str):
            continue
        normalized_source = normalize_identifier(source)
        normalized_target = normalize_identifier(target)
        graph.setdefault(normalized_source, []).append(normalized_target)
        label_lookup.setdefault(normalized_source, source)
        label_lookup.setdefault(normalized_target, target)

    queue: list[tuple[str, int]] = [(alias, 0) for alias in start_aliases]
    visited: set[str] = set(start_aliases)
    downstream: list[dict[str, Any]] = []

    while queue:
        node, depth = queue.pop(0)
        for child in graph.get(node, []):
            next_depth = depth + 1
            downstream.append({"node_id": label_lookup.get(child, child), "depth": next_depth})
            if child not in visited:
                visited.add(child)
                queue.append((child, next_depth))

    deduped: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()
    for item in downstream:
        key = (str(item["node_id"]), int(item["depth"]))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)

    return {
        "contract_aliases": sorted(start_aliases),
        "downstream_nodes": deduped,
        "transitive_depth": max((item["depth"] for item in deduped), default=0),
    }


def consumer_impact(contract_id: str, changes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    subscriptions = load_registry()
    impacts: list[dict[str, Any]] = []
    contract_key = normalize_identifier(contract_id)
    for subscription in subscriptions:
        if normalize_identifier(str(subscription.get("contract_id", ""))) != contract_key:
            continue

        consumed_fields = {
            normalize_field_name(item)
            for item in subscription.get("fields_consumed", [])
            if isinstance(item, str)
        }
        breaking_fields = {
            normalize_field_name(item.get("field"))
            for item in subscription.get("breaking_fields", [])
            if isinstance(item, dict) and item.get("field")
        }

        impacted_changes = []
        for change in changes:
            field_candidates = changed_field_candidates(change)
            if field_candidates & (consumed_fields | breaking_fields):
                impacted_changes.append(change)

        failure_modes = list(dict.fromkeys(failure_mode_for_change(change) for change in impacted_changes))
        impacts.append(
            {
                "subscriber_id": subscription.get("subscriber_id"),
                "subscriber_team": subscription.get("subscriber_team"),
                "validation_mode": subscription.get("validation_mode"),
                "contact": subscription.get("contact"),
                "affected_fields": sorted(
                    {
                        field
                        for change in impacted_changes
                        for field in changed_field_candidates(change)
                        if field in consumed_fields or field in breaking_fields
                    }
                ),
                "failure_mode_analysis": failure_modes
                or ["No directly consumed changed fields detected in the registry entry."],
                "required_action": (
                    "Coordinate migration with this subscriber before deployment."
                    if impacted_changes and any(change.get("compatibility") == "BREAKING" for change in impacted_changes)
                    else "Notify subscriber for awareness only."
                ),
            }
        )
    return impacts


def migration_checklist(changes: list[dict[str, Any]], consumer_impacts: list[dict[str, Any]]) -> list[str]:
    checklist: list[str] = []
    if not changes:
        return ["No schema changes detected across the analyzed snapshot window."]

    checklist.append("Review the exact schema diff and confirm the intended contract version bump.")

    if any(change.get("compatibility") == "BREAKING" for change in changes):
        checklist.extend(
            [
                "Notify every affected registry subscriber and capture explicit acknowledgement before deployment.",
                "Prepare producer changes, default values, aliases, or migration scripts required by the breaking diff.",
                "Stage the new schema behind a controlled rollout and block deploy until downstream validation passes.",
                "Re-run ValidationRunner and refresh statistical baselines after the migration lands.",
            ]
        )

    if any(change.get("change_type") == "rename_field" for change in changes):
        checklist.append("Maintain an alias or dual-write mapping for at least one sprint before removing the old field name.")
    if any(change.get("change_type") in {"narrow_type", "confidence_scale_change"} for change in changes):
        checklist.append("Publish a rollback-safe data conversion plan and verify no precision or semantic loss in downstream systems.")
    if any(change.get("change_type") == "change_enum_values" for change in changes):
        checklist.append("Update downstream branch logic and accepted-values tests for every impacted enum change.")
    if any(item.get("affected_fields") for item in consumer_impacts):
        checklist.append("Track consumer validation sign-off in subscriber order from highest enforcement mode to lowest.")
    return checklist


def rollback_plan(changes: list[dict[str, Any]]) -> list[str]:
    plan = [
        "Keep the previous snapshot and generated contract available as the rollback version.",
        "Re-deploy the prior producer schema if downstream validation or migration checks fail.",
        "Restore the prior statistical baseline and rerun contract validation against the reverted payloads.",
        "Notify all affected subscribers that the migration was rolled back and freeze further schema edits until root cause review completes.",
    ]
    if any(change.get("change_type") == "rename_field" for change in changes):
        plan.append("Re-enable the old field name or alias immediately if rename-related failures appear downstream.")
    return plan


def compatibility_verdict(changes: list[dict[str, Any]]) -> str:
    return "BREAKING" if any(change.get("compatibility") == "BREAKING" for change in changes) else "COMPATIBLE"


def migration_report_path(output_path: Path, contract_id: str, current_snapshot: Path) -> Path:
    return output_path.parent / f"migration_impact_{slugify(contract_id)}_{current_snapshot.stem}.json"


def build_migration_report(
    *,
    contract_id: str,
    previous_snapshot: Path,
    current_snapshot: Path,
    current_payload: dict[str, Any],
    changes: list[dict[str, Any]],
    comparison_verdict: str,
    consumer_impacts: list[dict[str, Any]],
) -> dict[str, Any]:
    blast_radius = lineage_blast_radius(contract_id, current_payload)
    human_diff = [change["human_diff"] for change in changes]
    return {
        "contract_id": contract_id,
        "generated_at": datetime_now_iso(),
        "comparison": {
            "previous_snapshot": make_relative_path(previous_snapshot),
            "current_snapshot": make_relative_path(current_snapshot),
            "previous_captured_at": parse_snapshot_timestamp(previous_snapshot).isoformat(),
            "current_captured_at": parse_snapshot_timestamp(current_snapshot).isoformat(),
        },
        "compatibility_verdict": comparison_verdict,
        "human_readable_diff": human_diff,
        "exact_diff": changes,
        "blast_radius": {
            "registry_subscribers": [
                impact.get("subscriber_id")
                for impact in consumer_impacts
                if impact.get("subscriber_id")
            ],
            "lineage": blast_radius,
            "total_known_consumers": len(
                {
                    *(impact.get("subscriber_id") for impact in consumer_impacts if impact.get("subscriber_id")),
                    *(item.get("node_id") for item in blast_radius.get("downstream_nodes", []) if item.get("node_id")),
                }
            ),
        },
        "per_consumer_failure_mode_analysis": consumer_impacts,
        "ordered_migration_checklist": migration_checklist(changes, consumer_impacts),
        "rollback_plan": rollback_plan(changes),
    }


def analyze_pair(contract_id: str, previous_snapshot: Path, current_snapshot: Path) -> tuple[dict[str, Any], dict[str, Any], str]:
    previous_payload = load_yaml(previous_snapshot)
    current_payload = load_yaml(current_snapshot)
    previous_schema = extract_schema(previous_payload)
    current_schema = extract_schema(current_payload)
    changes = diff_schemas(previous_schema, current_schema)
    verdict = compatibility_verdict(changes)
    comparison = {
        "previous_snapshot": make_relative_path(previous_snapshot),
        "current_snapshot": make_relative_path(current_snapshot),
        "previous_captured_at": parse_snapshot_timestamp(previous_snapshot).isoformat(),
        "current_captured_at": parse_snapshot_timestamp(current_snapshot).isoformat(),
        "change_count": len(changes),
        "compatibility_verdict": verdict,
        "human_readable_diff": [change["human_diff"] for change in changes],
        "changes": changes,
    }
    return comparison, current_payload, verdict


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def analyze(contract_id: str, since: str | None, output_path: Path) -> dict[str, Any]:
    pairs = resolve_snapshot_pairs(contract_id, since)
    comparisons: list[dict[str, Any]] = []
    generated_reports: list[str] = []
    all_changes = 0
    breaking_comparisons = 0

    for previous_snapshot, current_snapshot in pairs:
        comparison, current_payload, verdict = analyze_pair(contract_id, previous_snapshot, current_snapshot)
        comparisons.append(comparison)
        all_changes += comparison["change_count"]
        if verdict == "BREAKING":
            breaking_comparisons += 1
            consumer_impacts = consumer_impact(contract_id, comparison["changes"])
            impact_report = build_migration_report(
                contract_id=contract_id,
                previous_snapshot=previous_snapshot,
                current_snapshot=current_snapshot,
                current_payload=current_payload,
                changes=comparison["changes"],
                comparison_verdict=verdict,
                consumer_impacts=consumer_impacts,
            )
            impact_path = migration_report_path(output_path, contract_id, current_snapshot)
            write_json(impact_path, impact_report)
            generated_reports.append(make_relative_path(impact_path))

    overall_verdict = "BREAKING" if breaking_comparisons > 0 else "COMPATIBLE"
    return {
        "contract_id": contract_id,
        "analyzed_at": datetime_now_iso(),
        "since": since,
        "snapshot_directory": make_relative_path(snapshot_dir(contract_id)),
        "snapshots_analyzed": [
            {
                "previous_snapshot": comparison["previous_snapshot"],
                "current_snapshot": comparison["current_snapshot"],
            }
            for comparison in comparisons
        ],
        "summary": {
            "comparisons_analyzed": len(comparisons),
            "total_changes_detected": all_changes,
            "breaking_comparisons": breaking_comparisons,
            "compatibility_verdict": overall_verdict,
        },
        "comparisons": comparisons,
        "migration_impact_reports": generated_reports,
    }


def main() -> None:
    args = parse_args()
    output_path = Path(args.output)
    try:
        payload = analyze(args.contract_id, args.since, output_path)
        write_json(output_path, payload)
        print(
            json.dumps(
                {
                    "output": str(output_path),
                    "compatibility_verdict": payload["summary"]["compatibility_verdict"],
                    "comparisons_analyzed": payload["summary"]["comparisons_analyzed"],
                    "migration_impact_reports": payload["migration_impact_reports"],
                },
                indent=2,
            )
        )
    except Exception as exc:
        payload = {
            "contract_id": args.contract_id,
            "status": "ERROR",
            "message": str(exc),
        }
        write_json(output_path, payload)
        print(
            json.dumps(
                {"output": str(output_path), "status": "ERROR", "message": str(exc)},
                indent=2,
            )
        )
        raise SystemExit(1)


if __name__ == "__main__":
    main()
