from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import yaml


def parse_args() -> argparse.Namespace:
	parser = argparse.ArgumentParser(
		description="Generate Bitol and dbt contracts from profiled JSONL data."
	)
	parser.add_argument("--source", required=True, help="Path to the source JSONL file.")
	parser.add_argument(
		"--contract-id", required=True, help="Unique identifier for the generated contract."
	)
	parser.add_argument(
		"--lineage",
		required=True,
		help="Path to a lineage snapshot file or directory containing snapshots.",
	)
	parser.add_argument(
		"--output",
		required=True,
		help="Output directory or explicit Bitol YAML file path.",
	)
	return parser.parse_args()


def load_records(source_path: Path) -> list[dict[str, Any]]:
	if not source_path.exists():
		raise FileNotFoundError(f"Source file not found: {source_path}")

	if source_path.suffix == ".json":
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
			record = json.loads(raw_line)
			if not isinstance(record, dict):
				raise ValueError(
					f"Expected object per line in JSONL source, found {type(record).__name__} at line {line_number}."
				)
			records.append(record)
	return records


def normalize_scalar(value: Any) -> Any:
	if isinstance(value, (dict, list)):
		return json.dumps(value, sort_keys=True)
	return value


def flatten_record(
	payload: dict[str, Any],
	parent_key: str = "",
	separator: str = "__",
) -> dict[str, Any]:
	flattened: dict[str, Any] = {}
	for key, value in payload.items():
		composite_key = f"{parent_key}{separator}{key}" if parent_key else str(key)
		if isinstance(value, dict):
			flattened.update(flatten_record(value, composite_key, separator))
			continue
		flattened[composite_key] = normalize_scalar(value)
	return flattened


def flatten_for_profile(records: list[dict[str, Any]]) -> pd.DataFrame:
	flattened_rows: list[dict[str, Any]] = []

	for record in records:
		base_record = dict(record)
		extracted_facts = base_record.pop("extracted_facts", None)
		base_flat = flatten_record(base_record)

		if isinstance(extracted_facts, list) and extracted_facts:
			for fact_index, fact in enumerate(extracted_facts):
				fact_payload = fact if isinstance(fact, dict) else {"value": fact}
				fact_flat = flatten_record({"extracted_facts": fact_payload})
				row = {
					**base_flat,
					**fact_flat,
					"extracted_facts__fact_index": fact_index,
				}
				flattened_rows.append(row)
			continue

		if isinstance(extracted_facts, list):
			flattened_rows.append({**base_flat, "extracted_facts__fact_count": 0})
			continue

		if extracted_facts is not None:
			fact_payload = extracted_facts if isinstance(extracted_facts, dict) else {"value": extracted_facts}
			flattened_rows.append({**base_flat, **flatten_record({"extracted_facts": fact_payload})})
			continue

		flattened_rows.append(base_flat)

	if not flattened_rows:
		return pd.DataFrame()
	return pd.DataFrame(flattened_rows)


def infer_contract_type(series: pd.Series) -> str:
	non_null = series.dropna()
	if non_null.empty:
		return "string"

	if pd.api.types.is_bool_dtype(series):
		return "boolean"
	if pd.api.types.is_integer_dtype(series):
		return "integer"
	if pd.api.types.is_float_dtype(series):
		return "number"
	if pd.api.types.is_datetime64_any_dtype(series):
		return "timestamp"

	numeric_values = pd.to_numeric(non_null, errors="coerce")
	if numeric_values.notna().all():
		return "integer" if np.allclose(numeric_values % 1, 0, equal_nan=True) else "number"

	lowered = non_null.astype(str).str.lower()
	if lowered.isin({"true", "false"}).all():
		return "boolean"

	datetime_values = pd.to_datetime(non_null, errors="coerce", utc=True)
	if datetime_values.notna().all():
		return "timestamp"

	return "string"


def round_metric(value: Any) -> float | int | None:
	if value is None:
		return None
	if isinstance(value, (np.floating, float)):
		if np.isnan(value):
			return None
		return round(float(value), 6)
	if isinstance(value, (np.integer, int)):
		return int(value)
	return value


def compute_numeric_metrics(series: pd.Series) -> dict[str, float | int | None]:
	numeric_values = pd.to_numeric(series.dropna(), errors="coerce").dropna()
	if numeric_values.empty:
		return {"min": None, "max": None, "mean": None, "stddev": None}

	return {
		"min": round_metric(numeric_values.min()),
		"max": round_metric(numeric_values.max()),
		"mean": round_metric(numeric_values.mean()),
		"stddev": round_metric(numeric_values.std(ddof=0)),
	}


def profile_dataframe(frame: pd.DataFrame) -> list[dict[str, Any]]:
	profiles: list[dict[str, Any]] = []
	for column in frame.columns:
		series = frame[column]
		contract_type = infer_contract_type(series)
		unique_non_null = [value for value in series.dropna().unique().tolist()]
		profiles.append(
			{
				"name": column,
				"type": contract_type,
				"pandas_dtype": str(series.dtype),
				"null_fraction": round(float(series.isna().mean()), 6),
				"row_count": int(len(series)),
				"unique_count": int(series.nunique(dropna=True)),
				"accepted_values": accepted_values_candidate(unique_non_null),
				**compute_numeric_metrics(series),
			}
		)
	return sorted(profiles, key=lambda item: item["name"])


def accepted_values_candidate(values: list[Any]) -> list[Any] | None:
	if not values:
		return None

	normalized_values = [to_yaml_scalar(value) for value in values]
	if len(normalized_values) <= 10:
		return sorted(normalized_values, key=lambda item: str(item))
	return None


def to_yaml_scalar(value: Any) -> Any:
	if isinstance(value, (np.integer, int)):
		return int(value)
	if isinstance(value, (np.floating, float)):
		if np.isnan(value):
			return None
		return float(value)
	if isinstance(value, (np.bool_, bool)):
		return bool(value)
	return value


def map_profile_to_contract_field(profile: dict[str, Any]) -> dict[str, Any]:
	field: dict[str, Any] = {
		"name": profile["name"],
		"type": profile["type"],
		"required": profile["null_fraction"] == 0,
		"metrics": {
			"null_fraction": profile["null_fraction"],
			"dtype": profile["pandas_dtype"],
			"min": profile["min"],
			"max": profile["max"],
			"mean": profile["mean"],
			"stddev": profile["stddev"],
		},
	}

	if profile["type"] in {"integer", "number"}:
		if profile["min"] is not None:
			field["minimum"] = profile["min"]
		if profile["max"] is not None:
			field["maximum"] = profile["max"]

	if "confidence" in profile["name"].lower():
		field["minimum"] = 0.0
		field["maximum"] = 1.0

	return field


def slugify(value: str) -> str:
	normalized = re.sub(r"[^a-zA-Z0-9]+", "_", value).strip("_").lower()
	return normalized or "contract"


def parse_snapshot_file(snapshot_path: Path) -> Any:
	raw_text = snapshot_path.read_text(encoding="utf-8").strip()
	if not raw_text:
		return None

	if snapshot_path.suffix == ".jsonl":
		last_payload: Any = None
		for line in raw_text.splitlines():
			stripped = line.strip()
			if stripped:
				last_payload = json.loads(stripped)
		return last_payload

	return yaml.safe_load(raw_text)


def normalize_consumers(values: Any) -> list[str]:
	if isinstance(values, str):
		return [values]
	if not isinstance(values, list):
		return []

	consumers: list[str] = []
	for item in values:
		if isinstance(item, str):
			consumers.append(item)
			continue
		if isinstance(item, dict):
			for key in ("name", "id", "consumer", "target"):
				if item.get(key):
					consumers.append(str(item[key]))
					break
	return consumers


def extract_downstream_consumers(payload: Any) -> list[str]:
	if payload is None:
		return []

	if isinstance(payload, dict):
		for key in ("downstream_consumers", "downstreamConsumers", "consumers"):
			if key in payload:
				consumers = normalize_consumers(payload[key])
				if consumers:
					return consumers
		for value in payload.values():
			consumers = extract_downstream_consumers(value)
			if consumers:
				return consumers

	if isinstance(payload, list):
		for item in payload:
			consumers = extract_downstream_consumers(item)
			if consumers:
				return consumers

	return []


def load_downstream_consumers(lineage_path: Path) -> tuple[list[str], str | None]:
	if not lineage_path.exists():
		return [], None

	if lineage_path.is_file():
		payload = parse_snapshot_file(lineage_path)
		return extract_downstream_consumers(payload), lineage_path.name

	candidate_files = sorted(
		[
			path
			for path in lineage_path.rglob("*")
			if path.is_file() and path.suffix.lower() in {".yaml", ".yml", ".json", ".jsonl"}
		],
		key=lambda path: (path.stat().st_mtime, path.name),
		reverse=True,
	)

	for candidate in candidate_files:
		payload = parse_snapshot_file(candidate)
		consumers = extract_downstream_consumers(payload)
		if consumers:
			return consumers, str(candidate.relative_to(lineage_path))

	if candidate_files:
		return [], str(candidate_files[0].relative_to(lineage_path))
	return [], None


def build_bitol_contract(
	contract_id: str,
	source_path: Path,
	profiles: list[dict[str, Any]],
	downstream_consumers: list[str],
	lineage_snapshot: str | None,
) -> dict[str, Any]:
	model_name = slugify(contract_id)
	return {
		"dataContractSpecification": "1.1.0",
		"id": contract_id,
		"info": {
			"title": contract_id,
			"version": "1.0.0",
			"description": f"Auto-generated from {source_path.name}",
			"owner": "data-engineering",
		},
		"lineage": {
			"source": str(source_path),
			"lineage_snapshot": lineage_snapshot,
			"downstream_consumers": downstream_consumers,
		},
		"models": [
			{
				"name": model_name,
				"type": "table",
				"fields": [map_profile_to_contract_field(profile) for profile in profiles],
			}
		],
	}


def build_dbt_schema(contract_id: str, source_path: Path, profiles: list[dict[str, Any]]) -> dict[str, Any]:
	model_name = slugify(contract_id)
	columns: list[dict[str, Any]] = []
	for profile in profiles:
		tests: list[Any] = []
		if profile["null_fraction"] == 0:
			tests.append("not_null")
		if profile["accepted_values"]:
			tests.append({"accepted_values": {"values": profile["accepted_values"]}})

		column_entry: dict[str, Any] = {
			"name": profile["name"],
			"description": f"Auto-generated from {source_path.name}",
			"data_type": profile["type"],
		}
		if tests:
			column_entry["tests"] = tests
		columns.append(column_entry)

	return {
		"version": 2,
		"models": [
			{
				"name": model_name,
				"description": f"Generated schema contract for {contract_id}",
				"columns": columns,
			}
		],
	}


def resolve_output_paths(output_arg: str, contract_id: str) -> tuple[Path, Path]:
	output_path = Path(output_arg)
	if output_path.suffix.lower() in {".yaml", ".yml"}:
		output_path.parent.mkdir(parents=True, exist_ok=True)
		return output_path, output_path.with_name("schema.yml")

	output_path.mkdir(parents=True, exist_ok=True)
	return output_path / f"{slugify(contract_id)}.bitol.yaml", output_path / "schema.yml"


def write_yaml(target_path: Path, payload: dict[str, Any]) -> None:
	target_path.write_text(
		yaml.safe_dump(payload, sort_keys=False, allow_unicode=True),
		encoding="utf-8",
	)


def main() -> None:
	args = parse_args()
	source_path = Path(args.source)
	lineage_path = Path(args.lineage)

	records = load_records(source_path)
	profiled_frame = flatten_for_profile(records)
	profiles = profile_dataframe(profiled_frame)
	downstream_consumers, lineage_snapshot = load_downstream_consumers(lineage_path)

	bitol_contract = build_bitol_contract(
		contract_id=args.contract_id,
		source_path=source_path,
		profiles=profiles,
		downstream_consumers=downstream_consumers,
		lineage_snapshot=lineage_snapshot,
	)
	dbt_schema = build_dbt_schema(args.contract_id, source_path, profiles)
	bitol_path, dbt_path = resolve_output_paths(args.output, args.contract_id)

	write_yaml(bitol_path, bitol_contract)
	write_yaml(dbt_path, dbt_schema)

	print(f"Wrote Bitol contract to {bitol_path}")
	print(f"Wrote dbt schema to {dbt_path}")


if __name__ == "__main__":
	main()
