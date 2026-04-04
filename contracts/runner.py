from __future__ import annotations

import argparse
import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import yaml


PROJECT_ROOT = Path(__file__).resolve().parent.parent
BASELINE_PATH = PROJECT_ROOT / "schema_snapshots" / "numeric_column_baselines.json"
LEGACY_BASELINE_PATH = PROJECT_ROOT / "schema_snapshots" / "baselines.json"
UUID_PATTERN = re.compile(r"^[0-9a-f-]{36}$")
NUMERIC_TYPES = {"number", "integer"}
VALID_MODES = {"AUDIT", "ENFORCE", "WARN"}
EXPLODE_FIELDS = {"code_refs", "extracted_facts", "nodes", "edges"}


def parse_args() -> argparse.Namespace:
	parser = argparse.ArgumentParser(
		description="Consumer-side contract enforcement for JSONL datasets."
	)
	parser.add_argument("--contract", required=True, help="Path to the contract YAML file.")
	parser.add_argument("--data", required=True, help="Path to the target JSONL file.")
	parser.add_argument(
		"--mode",
		required=True,
		choices=sorted(VALID_MODES),
		help="Execution mode: AUDIT, ENFORCE, or WARN.",
	)
	parser.add_argument(
		"--output",
		required=True,
		help="Path to the JSON report output file.",
	)
	return parser.parse_args()


def load_contract(contract_path: Path) -> dict[str, Any]:
	if not contract_path.exists():
		raise FileNotFoundError(f"Contract file not found: {contract_path}")

	payload = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
	if not isinstance(payload, dict):
		raise ValueError("Contract YAML must contain a top-level object.")
	return payload


def load_jsonl_records(data_path: Path) -> list[dict[str, Any]]:
	if not data_path.exists():
		raise FileNotFoundError(f"Data file not found: {data_path}")
	records: list[dict[str, Any]] = []
	with data_path.open("r", encoding="utf-8") as handle:
		for line_number, line in enumerate(handle, start=1):
			raw_line = line.strip()
			if not raw_line:
				continue
			payload = json.loads(raw_line)
			if not isinstance(payload, dict):
				raise ValueError(
					f"Expected one JSON object per line in {data_path} at line {line_number}."
				)
			records.append(payload)
	return records


def normalize_scalar(value: Any) -> Any:
	if isinstance(value, (dict, list)):
		return json.dumps(value, sort_keys=True, ensure_ascii=False)
	return value


def flatten_object(
	payload: dict[str, Any], parent_key: str = "", separator: str = "__"
) -> dict[str, Any]:
	flattened: dict[str, Any] = {}
	for key, value in payload.items():
		composite_key = f"{parent_key}{separator}{key}" if parent_key else str(key)
		if isinstance(value, dict):
			flattened.update(flatten_object(value, composite_key, separator))
		else:
			flattened[composite_key] = normalize_scalar(value)
	return flattened


def expand_list_field(field_name: str, values: list[Any]) -> list[dict[str, Any]]:
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
	records: list[dict[str, Any]], explode_fields: set[str] | None = None
) -> list[dict[str, Any]]:
	explode_fields = explode_fields or EXPLODE_FIELDS
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


def flatten_for_checks(records: list[dict[str, Any]]) -> pd.DataFrame:
	rows = flatten_records(records)
	return pd.DataFrame(rows) if rows else pd.DataFrame()


def contract_prefix(contract_id: str) -> str:
	return contract_id.split("-", 1)[0] if "-" in contract_id else contract_id


def pretty_column_name(column_name: str) -> str:
	root, separator, child = column_name.partition("__")
	if separator and root in EXPLODE_FIELDS:
		dotted_child = child.replace("__", ".")
		return f"{root}[*].{dotted_child}"
	return column_name.replace("__", ".")


def canonical_registry_field(column_name: str) -> str:
	root, separator, child = column_name.partition("__")
	if separator and root in EXPLODE_FIELDS:
		return f"{root}.{child.replace('__', '.')}"
	return column_name.replace("__", ".")


def check_identifier(contract_id: str, column_name: str, check_type: str) -> str:
	column_key = canonical_registry_field(column_name)
	return f"{contract_prefix(contract_id)}.{column_key}.{check_type}"


def extract_fields(contract: dict[str, Any]) -> list[dict[str, Any]]:
	models = contract.get("models", [])
	if not isinstance(models, list):
		return []

	def expand_fields(field_list: list[dict[str, Any]], prefix: str = "") -> list[dict[str, Any]]:
		expanded: list[dict[str, Any]] = []
		for field in field_list:
			if not isinstance(field, dict) or not field.get("name"):
				continue

			field_name = f"{prefix}{field['name']}" if prefix else str(field["name"])
			if field.get("type") == "array" and isinstance(field.get("items"), dict):
				item_fields = field["items"].get("fields", [])
				if isinstance(item_fields, list):
					expanded.extend(expand_fields(item_fields, prefix=f"{field_name}__"))
				continue

			flattened_field = dict(field)
			flattened_field["name"] = field_name
			expanded.append(flattened_field)
		return expanded

	fields: list[dict[str, Any]] = []
	for model in models:
		if not isinstance(model, dict):
			continue
		model_fields = model.get("fields", [])
		if not isinstance(model_fields, list):
			continue
		fields.extend(expand_fields(model_fields))
	return fields


def contract_id_from_contract(contract: dict[str, Any], contract_path: Path) -> str:
	contract_id = contract.get("id")
	if isinstance(contract_id, str) and contract_id.strip():
		return contract_id.strip()
	return contract_path.stem


def data_snapshot_id(data_path: Path) -> str:
	digest = hashlib.sha256()
	with data_path.open("rb") as handle:
		for chunk in iter(lambda: handle.read(8192), b""):
			digest.update(chunk)
	return digest.hexdigest()


def now_iso() -> str:
	return datetime.now(timezone.utc).isoformat()


def json_safe(value: Any) -> Any:
	if isinstance(value, dict):
		return {str(key): json_safe(item) for key, item in value.items()}
	if isinstance(value, (list, tuple)):
		return [json_safe(item) for item in value]
	if isinstance(value, pd.Timestamp):
		return value.isoformat()
	if isinstance(value, datetime):
		return value.isoformat()
	if hasattr(value, "item"):
		try:
			return value.item()
		except (ValueError, TypeError):
			pass
	if pd.isna(value):
		return None
	return value


def status_to_severity(status: str, *, fail_severity: str = "CRITICAL") -> str:
	mapping = {
		"PASS": "LOW",
		"WARNING": "WARNING",
		"FAIL": fail_severity,
		"ERROR": "CRITICAL",
	}
	return mapping.get(status, "LOW")


def make_result(
	*,
	check_id: str,
	column_name: str,
	check_type: str,
	status: str,
	actual_value: Any,
	expected: Any,
	message: str,
	records_failing: int | None = None,
	sample_failing: list[Any] | None = None,
	severity: str | None = None,
) -> dict[str, Any]:
	return {
		"check_id": check_id,
		"column_name": pretty_column_name(column_name),
		"check_type": check_type,
		"status": status,
		"actual_value": json_safe(actual_value),
		"expected": json_safe(expected),
		"records_failing": records_failing,
		"sample_failing": json_safe(sample_failing or []),
		"severity": severity or status_to_severity(status),
		"message": message,
	}


def missing_column_result(column_name: str, check_type: str, expected: Any) -> dict[str, Any]:
	return make_result(
		check_id=check_type,
		column_name=column_name,
		check_type=check_type,
		status="ERROR",
		actual_value=None,
		expected=expected,
		records_failing=0,
		sample_failing=[],
		severity="CRITICAL",
		message="Column defined in the contract is missing from the data.",
	)


def is_missing(value: Any) -> bool:
	return bool(pd.isna(value))


def parse_number(value: Any) -> float | None:
	if is_missing(value) or isinstance(value, bool):
		return None
	try:
		parsed = float(value)
	except (TypeError, ValueError):
		return None
	return parsed if pd.notna(parsed) else None


def matches_type(value: Any, expected_type: str) -> bool:
	if is_missing(value):
		return True

	expected = expected_type.lower()
	if expected == "string":
		return isinstance(value, str)
	if expected == "boolean":
		return isinstance(value, bool)
	if expected == "integer":
		parsed = parse_number(value)
		return parsed is not None and float(parsed).is_integer()
	if expected == "number":
		return parse_number(value) is not None
	return True


def is_iso_datetime(value: Any) -> bool:
	if is_missing(value) or not isinstance(value, str):
		return False
	candidate = value.replace("Z", "+00:00")
	try:
		datetime.fromisoformat(candidate)
	except ValueError:
		return False
	return True


def validate_required(frame: pd.DataFrame, field: dict[str, Any]) -> dict[str, Any]:
	column_name = str(field["name"])
	contract_id = str(field.get("_contract_id", "contract"))
	if column_name not in frame.columns:
		result = missing_column_result(column_name, "required", True)
		result["check_id"] = check_identifier(contract_id, column_name, "required")
		return result

	missing_count = int(frame[column_name].isna().sum())
	status = "PASS" if missing_count == 0 else "FAIL"
	return make_result(
		check_id=check_identifier(contract_id, column_name, "required"),
		column_name=column_name,
		check_type="required",
		status=status,
		actual_value=missing_count,
		expected=0,
		records_failing=missing_count,
		sample_failing=frame.index[frame[column_name].isna()].tolist()[:5],
		severity="CRITICAL" if status == "FAIL" else "LOW",
		message=(
			"Required field contains no nulls."
			if status == "PASS"
			else "Required field contains null or missing values."
		),
	)


def validate_type(frame: pd.DataFrame, field: dict[str, Any]) -> dict[str, Any]:
	column_name = str(field["name"])
	contract_id = str(field.get("_contract_id", "contract"))
	expected_type = str(field.get("type", "string"))
	if column_name not in frame.columns:
		result = missing_column_result(column_name, "type", expected_type)
		result["check_id"] = check_identifier(contract_id, column_name, "type")
		return result

	series = frame[column_name]
	invalid_mask = ~series.apply(lambda value: matches_type(value, expected_type))
	failing_values = series[invalid_mask]
	failing_count = int(len(failing_values.index))
	status = "PASS" if failing_count == 0 else "FAIL"
	return make_result(
		check_id=check_identifier(contract_id, column_name, "type"),
		column_name=column_name,
		check_type="type",
		status=status,
		actual_value=None if failing_count == 0 else failing_values.iloc[0],
		expected=expected_type,
		records_failing=failing_count,
		sample_failing=failing_values.astype(str).tolist()[:5],
		severity="CRITICAL" if status == "FAIL" else "LOW",
		message=(
			"Observed values conform to the contract type."
			if status == "PASS"
			else "Observed values do not conform to the contract type."
		),
	)


def validate_format(frame: pd.DataFrame, field: dict[str, Any]) -> dict[str, Any] | None:
	column_name = str(field["name"])
	contract_id = str(field.get("_contract_id", "contract"))
	expected_format = field.get("format")
	if expected_format not in {"uuid", "date-time"}:
		return None
	if column_name not in frame.columns:
		result = missing_column_result(column_name, "format", expected_format)
		result["check_id"] = check_identifier(contract_id, column_name, "format")
		return result

	series = frame[column_name].dropna()
	if expected_format == "uuid":
		invalid_mask = ~series.astype(str).str.fullmatch(UUID_PATTERN)
		failing_values = series[invalid_mask]
	else:
		invalid_mask = ~series.apply(is_iso_datetime)
		failing_values = series[invalid_mask]

	failing_count = int(len(failing_values.index))
	status = "PASS" if failing_count == 0 else "FAIL"
	return make_result(
		check_id=check_identifier(contract_id, column_name, "format"),
		column_name=column_name,
		check_type="format",
		status=status,
		actual_value=None if failing_count == 0 else failing_values.iloc[0],
		expected=(
			"^[0-9a-f-]{36}$" if expected_format == "uuid" else "datetime.fromisoformat() compatible"
		),
		records_failing=failing_count,
		sample_failing=failing_values.astype(str).tolist()[:5],
		severity="CRITICAL" if status == "FAIL" else "LOW",
		message=(
			f"Values match the expected {expected_format} format."
			if status == "PASS"
			else f"One or more values do not match the expected {expected_format} format."
		),
	)


def validate_range(frame: pd.DataFrame, field: dict[str, Any]) -> dict[str, Any] | None:
	column_name = str(field["name"])
	contract_id = str(field.get("_contract_id", "contract"))
	minimum = field.get("minimum")
	maximum = field.get("maximum")
	if minimum is None and maximum is None:
		return None
	if column_name not in frame.columns:
		result = missing_column_result(
			column_name,
			"range",
			{"minimum": minimum, "maximum": maximum},
		)
		result["check_id"] = check_identifier(contract_id, column_name, "range")
		return result

	series = frame[column_name]
	numeric_series = series.apply(parse_number)
	invalid_mask = pd.Series(False, index=series.index)
	if minimum is not None:
		invalid_mask |= numeric_series.apply(lambda value: value is not None and value < float(minimum))
	if maximum is not None:
		invalid_mask |= numeric_series.apply(lambda value: value is not None and value > float(maximum))

	failing_values = series[invalid_mask]
	failing_count = int(len(failing_values.index))
	status = "PASS" if failing_count == 0 else "FAIL"
	numeric_non_null = numeric_series.dropna()
	actual_summary = (
		None
		if numeric_non_null.empty
		else f"min={round(float(numeric_non_null.min()), 6)}, max={round(float(numeric_non_null.max()), 6)}, mean={round(float(numeric_non_null.mean()), 6)}"
	)
	expected_summary = f"max<={maximum}, min>={minimum}"
	message = "Numeric values are within the contract range."
	if status == "FAIL":
		if "confidence" in column_name.lower():
			message = "confidence is outside the 0.0–1.0 range. Breaking change detected."
		else:
			message = "Numeric values exceed the contract range."
	return make_result(
		check_id=check_identifier(contract_id, column_name, "range"),
		column_name=column_name,
		check_type="range",
		status=status,
		actual_value=actual_summary,
		expected=expected_summary,
		records_failing=failing_count,
		sample_failing=failing_values.index.astype(str).tolist()[:5],
		severity="CRITICAL" if status == "FAIL" else "LOW",
		message=message,
	)


def numeric_column_names(frame: pd.DataFrame) -> list[str]:
	numeric_columns: list[str] = []
	for column in frame.columns:
		numeric_series = pd.to_numeric(frame[column], errors="coerce").dropna()
		if not numeric_series.empty:
			numeric_columns.append(str(column))
	return sorted(numeric_columns)


def numeric_contract_columns(frame: pd.DataFrame, fields: list[dict[str, Any]]) -> list[str]:
	contract_numeric_columns: list[str] = []
	seen: set[str] = set()
	for field in fields:
		column_name = str(field.get("name", ""))
		if not column_name or column_name in seen:
			continue
		if str(field.get("type", "")).lower() not in NUMERIC_TYPES:
			continue
		if column_name not in frame.columns:
			continue
		numeric_series = pd.to_numeric(frame[column_name], errors="coerce").dropna()
		if numeric_series.empty:
			continue
		contract_numeric_columns.append(column_name)
		seen.add(column_name)
	return sorted(contract_numeric_columns)


def build_baselines(frame: pd.DataFrame, fields: list[dict[str, Any]], contract_id: str) -> dict[str, dict[str, Any]]:
	baselines: dict[str, dict[str, Any]] = {}
	for column_name in numeric_contract_columns(frame, fields):
		numeric_series = pd.to_numeric(frame[column_name], errors="coerce").dropna()
		if numeric_series.empty:
			continue
		baselines[column_name] = {
			"mean": round(float(numeric_series.mean()), 6),
			"stddev": round(float(numeric_series.std(ddof=0)), 6),
			"row_count": int(len(numeric_series.index)),
			"_contract_id": contract_id,
		}
	return baselines


def load_baselines(contract_id: str) -> dict[str, dict[str, Any]]:
	baseline_path = BASELINE_PATH if BASELINE_PATH.exists() else LEGACY_BASELINE_PATH
	if not baseline_path.exists():
		return {}
	payload = json.loads(baseline_path.read_text(encoding="utf-8"))
	if not isinstance(payload, dict):
		return {}

	contracts = payload.get("contracts")
	if isinstance(contracts, dict):
		contract_columns = contracts.get(contract_id, {})
		if isinstance(contract_columns, dict):
			return {
				str(key): value
				for key, value in contract_columns.items()
				if isinstance(value, dict)
			}
		return {}

	legacy_columns = payload.get("columns", {})
	if not isinstance(legacy_columns, dict):
		return {}
	return {
		str(key): value
		for key, value in legacy_columns.items()
		if isinstance(value, dict) and value.get("_contract_id") == contract_id
	}


def save_baselines(contract_id: str, baselines: dict[str, dict[str, Any]]) -> None:
	BASELINE_PATH.parent.mkdir(parents=True, exist_ok=True)
	existing_payload: dict[str, Any] = {}
	if BASELINE_PATH.exists():
		loaded = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
		if isinstance(loaded, dict):
			existing_payload = loaded
	contracts = existing_payload.get("contracts", {})
	if not isinstance(contracts, dict):
		contracts = {}
	contracts[contract_id] = baselines
	payload = {
		"contracts": contracts,
		"created_at": now_iso(),
	}
	BASELINE_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def validate_drift(frame: pd.DataFrame, column_name: str, baselines: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
	baseline_entry = baselines.get(column_name)
	if baseline_entry is None:
		return None
	contract_id = str(baseline_entry.get("_contract_id", "contract"))
	if column_name not in frame.columns:
		result = missing_column_result(column_name, "statistical_drift", baseline_entry)
		result["check_id"] = check_identifier(contract_id, column_name, "statistical_drift")
		return result

	numeric_series = pd.to_numeric(frame[column_name], errors="coerce").dropna()
	if numeric_series.empty:
		return make_result(
			check_id=check_identifier(contract_id, column_name, "statistical_drift"),
			column_name=column_name,
			check_type="statistical_drift",
			status="ERROR",
			actual_value=None,
			expected=baseline_entry,
			records_failing=0,
			sample_failing=[],
			severity="CRITICAL",
			message="Numeric values are unavailable; unable to compute drift.",
		)

	try:
		baseline_mean = float(baseline_entry.get("mean"))
		baseline_stddev = float(baseline_entry.get("stddev"))
	except (TypeError, ValueError):
		return make_result(
			check_id=check_identifier(contract_id, column_name, "statistical_drift"),
			column_name=column_name,
			check_type="statistical_drift",
			status="ERROR",
			actual_value=None,
			expected=baseline_entry,
			records_failing=0,
			sample_failing=[],
			severity="CRITICAL",
			message="Baseline statistics are invalid; unable to compute drift.",
		)

	current_mean = float(numeric_series.mean())
	if baseline_stddev == 0:
		z_score = 0.0 if current_mean == baseline_mean else float("inf")
	else:
		z_score = abs(current_mean - baseline_mean) / baseline_stddev

	if z_score > 3:
		status = "FAIL"
		severity = "HIGH"
		message = "Current mean exceeds the fail drift threshold (z-score > 3). Silent corruption risk detected."
	elif z_score > 2:
		status = "WARNING"
		severity = "WARNING"
		message = "Current mean exceeds the warning drift threshold (z-score > 2)."
	else:
		status = "PASS"
		severity = "LOW"
		message = "Current mean is within the baseline drift thresholds."
	actual_value = f"mean={round(current_mean, 6)}, z_score={round(z_score, 6) if z_score != float('inf') else 'inf'}"
	expected_value = f"baseline_mean={baseline_mean}, stddev={baseline_stddev}, warning>2, fail>3"
	if "confidence" in column_name.lower() and status == "FAIL":
		message = "confidence distribution shifted beyond 3 stddev from baseline. Silent corruption detected."

	return make_result(
		check_id=check_identifier(contract_id, column_name, "statistical_drift"),
		column_name=column_name,
		check_type="statistical_drift",
		status=status,
		actual_value=actual_value,
		expected=expected_value,
		records_failing=int(len(numeric_series.index)) if status in {"FAIL", "WARNING"} else 0,
		sample_failing=numeric_series.astype(str).tolist()[:5] if status in {"FAIL", "WARNING"} else [],
		severity=severity,
		message=message,
	)


def validate_contract(contract: dict[str, Any], frame: pd.DataFrame) -> list[dict[str, Any]]:
	results: list[dict[str, Any]] = []
	contract_identifier = contract_id_from_contract(contract, Path("contract.yaml"))
	fields = extract_fields(contract)
	for field in fields:
		field["_contract_id"] = contract_identifier
	baselines = load_baselines(contract_identifier)
	if not baselines:
		baselines = build_baselines(frame, fields, contract_identifier)
		save_baselines(contract_identifier, baselines)
	else:
		for baseline in baselines.values():
			baseline.setdefault("_contract_id", contract_identifier)

	numeric_fields_for_drift = numeric_contract_columns(frame, fields)

	for field in fields:
		if field.get("required"):
			results.append(validate_required(frame, field))

		results.append(validate_type(frame, field))

		format_result = validate_format(frame, field)
		if format_result is not None:
			results.append(format_result)

		range_result = validate_range(frame, field)
		if range_result is not None:
			results.append(range_result)

	for column_name in numeric_fields_for_drift:
		drift_result = validate_drift(frame, column_name, baselines)
		if drift_result is not None:
			results.append(drift_result)

	for column_name in sorted(set(baselines.keys()) - set(numeric_fields_for_drift)):
		drift_result = validate_drift(frame, column_name, baselines)
		if drift_result is not None:
			results.append(drift_result)

	return results


def build_report(
	contract: dict[str, Any],
	contract_path: Path,
	data_path: Path,
	results: list[dict[str, Any]],
) -> dict[str, Any]:
	failed = sum(1 for result in results if result["status"] == "FAIL")
	warned = sum(1 for result in results if result["status"] == "WARNING")
	errored = sum(1 for result in results if result["status"] == "ERROR")
	passed = sum(1 for result in results if result["status"] == "PASS")
	return {
		"report_id": str(uuid.uuid4()),
		"contract_id": contract_id_from_contract(contract, contract_path),
		"snapshot_id": data_snapshot_id(data_path),
		"run_timestamp": now_iso(),
		"total_checks": len(results),
		"passed": passed,
		"failed": failed,
		"warned": warned,
		"errored": errored,
		"results": results,
	}


def write_report(report: dict[str, Any], output_path: Path) -> None:
	output_path.parent.mkdir(parents=True, exist_ok=True)
	output_path.write_text(json.dumps(report, indent=2), encoding="utf-8")


def exit_code_for_mode(mode: str, report: dict[str, Any]) -> int:
	if mode == "ENFORCE" and report["failed"] > 0:
		return 1
	return 0


def main() -> None:
	args = parse_args()
	contract_path = Path(args.contract)
	data_path = Path(args.data)
	output_path = Path(args.output)

	contract = load_contract(contract_path)
	records = load_jsonl_records(data_path)
	frame = flatten_for_checks(records)
	results = validate_contract(contract, frame)
	report = build_report(contract, contract_path, data_path, results)
	write_report(report, output_path)

	print(
		json.dumps(
			{"output": str(output_path), "failed": report["failed"], "warned": report["warned"]},
			indent=2,
		)
	)
	raise SystemExit(exit_code_for_mode(args.mode, report))


if __name__ == "__main__":
	main()
