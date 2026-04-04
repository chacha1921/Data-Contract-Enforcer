from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

import yaml


PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_SNAPSHOTS_DIR = PROJECT_ROOT / "schema_snapshots"


@dataclass
class FieldSignature:
    name: str
    field_type: str
    required: bool
    format: str | None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Analyze schema evolution between the two most recent contract snapshots."
    )
    parser.add_argument("--contract-id", required=True, help="Contract identifier to analyze.")
    parser.add_argument("--output", required=True, help="Path to the migration impact JSON report.")
    return parser.parse_args()


def snapshot_dir(contract_id: str) -> Path:
    return SCHEMA_SNAPSHOTS_DIR / contract_id


def list_recent_snapshots(contract_id: str) -> list[Path]:
    directory = snapshot_dir(contract_id)
    if not directory.exists():
        raise FileNotFoundError(f"Snapshot directory not found: {directory}")
    snapshots = sorted(
        [path for path in directory.iterdir() if path.is_file() and path.suffix in {".yaml", ".yml"}],
        key=lambda path: path.name,
        reverse=True,
    )
    if len(snapshots) < 2:
        raise ValueError(f"Need at least two snapshots in {directory} to analyze evolution.")
    return snapshots[:2]


def load_yaml(path: Path) -> dict[str, Any]:
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Expected top-level YAML object in {path}")
    return payload


def flatten_fields(fields: list[dict[str, Any]], prefix: str = "") -> dict[str, FieldSignature]:
    flattened: dict[str, FieldSignature] = {}
    for field in fields:
        if not isinstance(field, dict) or not field.get("name"):
            continue
        name = f"{prefix}{field['name']}" if prefix else str(field["name"])
        if field.get("type") == "array" and isinstance(field.get("items"), dict):
            item_fields = field["items"].get("fields", [])
            if isinstance(item_fields, list):
                flattened.update(flatten_fields(item_fields, prefix=f"{name}__"))
            continue
        flattened[name] = FieldSignature(
            name=name,
            field_type=str(field.get("type", "string")),
            required=bool(field.get("required", False)),
            format=str(field.get("format")) if field.get("format") is not None else None,
        )
    return flattened


def extract_schema(payload: dict[str, Any]) -> dict[str, FieldSignature]:
    models = payload.get("models", [])
    if not isinstance(models, list):
        return {}
    extracted: dict[str, FieldSignature] = {}
    for model in models:
        if not isinstance(model, dict):
            continue
        fields = model.get("fields", [])
        if not isinstance(fields, list):
            continue
        extracted.update(flatten_fields(fields))
    return extracted


def similarity(left: str, right: str) -> float:
    return SequenceMatcher(a=left, b=right).ratio()


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
            if (old_sig.field_type, old_sig.required, old_sig.format) != (
                new_sig.field_type,
                new_sig.required,
                new_sig.format,
            ):
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
            {
                "change_type": "rename_field",
                "compatibility": "BREAKING",
                "from": old_name,
                "to": best_name,
                "details": {
                    "type": old_sig.field_type,
                    "required": old_sig.required,
                    "format": old_sig.format,
                },
            }
        )
    return changes, used_removed, used_added


def diff_schemas(previous: dict[str, FieldSignature], current: dict[str, FieldSignature]) -> list[dict[str, Any]]:
    removed = {name: sig for name, sig in previous.items() if name not in current}
    added = {name: sig for name, sig in current.items() if name not in previous}
    changes, used_removed, used_added = rename_pairs(removed, added)

    for name, signature in added.items():
        if name in used_added:
            continue
        compatibility = "COMPATIBLE" if not signature.required else "BREAKING"
        changes.append(
            {
                "change_type": "add_field",
                "compatibility": compatibility,
                "field": name,
                "details": {
                    "type": signature.field_type,
                    "required": signature.required,
                    "format": signature.format,
                },
            }
        )

    for name, signature in removed.items():
        if name in used_removed:
            continue
        changes.append(
            {
                "change_type": "remove_field",
                "compatibility": "BREAKING",
                "field": name,
                "details": {
                    "type": signature.field_type,
                    "required": signature.required,
                    "format": signature.format,
                },
            }
        )

    for name in sorted(set(previous.keys()) & set(current.keys())):
        before = previous[name]
        after = current[name]
        if before.field_type != after.field_type:
            changes.append(
                {
                    "change_type": "change_type",
                    "compatibility": "BREAKING",
                    "field": name,
                    "details": {
                        "from": before.field_type,
                        "to": after.field_type,
                    },
                }
            )
        elif before.required != after.required:
            compatibility = "BREAKING" if after.required else "COMPATIBLE"
            changes.append(
                {
                    "change_type": "change_required",
                    "compatibility": compatibility,
                    "field": name,
                    "details": {
                        "from": before.required,
                        "to": after.required,
                    },
                }
            )
        elif before.format != after.format:
            changes.append(
                {
                    "change_type": "change_format",
                    "compatibility": "BREAKING",
                    "field": name,
                    "details": {
                        "from": before.format,
                        "to": after.format,
                    },
                }
            )

    return sorted(changes, key=lambda item: (item["compatibility"], item.get("field", item.get("from", ""))))


def migration_checklist(changes: list[dict[str, Any]]) -> list[str]:
    checklist = ["Review all downstream registry subscribers for compatibility impact."]
    if not changes:
        checklist.append("No schema changes detected between the two most recent snapshots.")
        return checklist

    if any(change["compatibility"] == "BREAKING" for change in changes):
        checklist.extend(
            [
                "Plan a version bump before releasing the new contract.",
                "Coordinate a migration window for affected consumers.",
                "Update enforcement thresholds and downstream transformations.",
            ]
        )
    if any(change["change_type"] == "add_field" and change["compatibility"] == "COMPATIBLE" for change in changes):
        checklist.append("Document newly added nullable fields for downstream teams.")
    if any(change["change_type"] == "rename_field" for change in changes):
        checklist.append("Provide a field mapping from old names to new names for downstream consumers.")
    if any(change["change_type"] == "change_type" for change in changes):
        checklist.append("Backfill or cast historical data before cutover to the new type.")
    return checklist


def analyze(contract_id: str) -> dict[str, Any]:
    current_snapshot, previous_snapshot = list_recent_snapshots(contract_id)
    current_payload = load_yaml(current_snapshot)
    previous_payload = load_yaml(previous_snapshot)

    current_schema = extract_schema(current_payload)
    previous_schema = extract_schema(previous_payload)
    changes = diff_schemas(previous_schema, current_schema)
    compatibility_verdict = (
        "BREAKING" if any(change["compatibility"] == "BREAKING" for change in changes) else "COMPATIBLE"
    )

    return {
        "contract_id": contract_id,
        "migration_impact": {
            "analyzed_at": datetime_now_iso(),
            "previous_snapshot": str(previous_snapshot),
            "current_snapshot": str(current_snapshot),
            "diff": changes,
            "compatibility_verdict": compatibility_verdict,
            "migration_checklist": migration_checklist(changes),
        },
    }


def datetime_now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def main() -> None:
    args = parse_args()
    output_path = Path(args.output)
    try:
        payload = analyze(args.contract_id)
        write_json(output_path, payload)
        print(json.dumps({"output": args.output, "compatibility_verdict": payload["migration_impact"]["compatibility_verdict"]}, indent=2))
    except Exception as exc:
        payload = {
            "contract_id": args.contract_id,
            "migration_impact": None,
            "status": "ERROR",
            "message": str(exc),
        }
        write_json(output_path, payload)
        print(json.dumps({"output": args.output, "status": "ERROR", "message": str(exc)}, indent=2))
        raise SystemExit(1)


if __name__ == "__main__":
    main()
