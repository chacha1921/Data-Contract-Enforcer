from __future__ import annotations

import argparse
import json
import re
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
	sys.path.insert(0, str(PROJECT_ROOT))

from contracts.generator import flatten_for_profile, load_records


UUID_PATTERN = re.compile(r"^[0-9a-f-]{36}$")
NUMERIC_TYPES = {"number", "integer"}
BASELINE_PATH = PROJECT_ROOT / "schema_snapshots" / "baselines.json"


def parse_args() -> argparse.Namespace:
	parser = argparse.ArgumentParser(description="Validate JSONL data against a Bitol YAML contract.")
	parser.add_argument("--contract", required=True, help="Path to the Bitol YAML contract.")
	parser.add_argument("--data", required=True, help="Path to the target JSONL dataset.")
	return parser.parse_args()


def load_contract(contract_path: Path) -> dict[str, Any]:
	if not contract_path.exists():
		raise FileNotFoundError(f"Contract file not found: {contract_path}")

	payload = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
	if not isinstance(payload, dict):
		raise ValueError("Contract YAML must contain a top-level object.")
	return payload


def extract_fields(contract: dict[str, Any]) -> list[dict[str, Any]]:
	models = contract.get("models", [])
	if not isinstance(models, list):
		return []

	fields: list[dict[str, Any]] = []
	for model in models:
		if not isinstance(model, dict):
			continue
		model_fields = model.get("fields", [])
		if not isinstance(model_fields, list):
			continue
		for field in model_fields:
			if isinstance(field, dict) and field.get("name"):
				fields.append(field)
	return fields


def make_result(
	check_name: str,
	field_name: str,
	status: str,
	failing_count: int,
	message: str,
	*,
	severity: str | None = None,
	metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
	result = {
		"check": check_name,
		"field": field_name,
		"status": status,
		"severity": severity or derive_severity(status),
		"failing_record_count": int(failing_count),
		"message": message,
	}
	if metadata:
		result["metadata"] = metadata
	return result


def derive_severity(status: str) -> str:
	mapping = {
		"PASS": "INFO",
		"WARNING": "WARNING",
		"FAIL": "ERROR",
		"ERROR": "ERROR",
	}
	return mapping.get(status, "INFO")


def is_missing(value: Any) -> bool:
	return pd.isna(value)


def is_number(value: Any) -> bool:
	if isinstance(value, bool):
		return False
	try:
		parsed = float(value)
	except (TypeError, ValueError):
		return False
	return pd.notna(parsed)


def is_integer(value: Any) -> bool:
	if not is_number(value):
		return False
	return float(value).is_integer()


def matches_type(value: Any, expected_type: str) -> bool:
	if is_missing(value):
		return True

	expected = expected_type.lower()
	if expected == "string":
		return isinstance(value, str)
	if expected == "boolean":
		if isinstance(value, bool):
			return True
		if isinstance(value, str):
			return value.lower() in {"true", "false"}
		return False
	if expected == "integer":
		return is_integer(value)
	if expected == "number":
		return is_number(value)
	if expected == "timestamp":
		return pd.notna(pd.to_datetime([value], errors="coerce", utc=True))[0]
	return True


def validate_required_field(frame: pd.DataFrame, field: dict[str, Any]) -> dict[str, Any]:
	field_name = str(field["name"])
	if field_name not in frame.columns:
		return make_result(
			"structural.required",
			field_name,
			"ERROR",
			len(frame.index),
			"Required field is missing from the dataset.",
		)

	missing_count = int(frame[field_name].isna().sum())
	status = "PASS" if missing_count == 0 else "FAIL"
	message = "Required field is present for all records." if missing_count == 0 else "Required field contains null or missing values."
	return make_result("structural.required", field_name, status, missing_count, message)


def validate_field_type(frame: pd.DataFrame, field: dict[str, Any]) -> dict[str, Any]:
	field_name = str(field["name"])
	expected_type = str(field.get("type", "string"))
	if field_name not in frame.columns:
		return make_result(
			"type.match",
			field_name,
			"ERROR",
			len(frame.index),
			"Field is missing; unable to validate types.",
			metadata={"expected_type": expected_type},
		)

	series = frame[field_name]
	mask = ~series.apply(lambda value: matches_type(value, expected_type))
	failing_count = int(mask.sum())
	status = "PASS" if failing_count == 0 else "FAIL"
	message = "Observed values match the contract type." if failing_count == 0 else "Observed values do not match the contract type."
	return make_result(
		"type.match",
		field_name,
		status,
		failing_count,
		message,
		metadata={"expected_type": expected_type},
	)


def should_apply_uuid_check(field: dict[str, Any]) -> bool:
	field_name = str(field.get("name", "")).lower()
	field_type = str(field.get("type", "")).lower()
	return "uuid" in field_name or field_type == "uuid"


def validate_uuid_format(frame: pd.DataFrame, field: dict[str, Any]) -> dict[str, Any]:
	field_name = str(field["name"])
	if field_name not in frame.columns:
		return make_result(
			"format.uuid",
			field_name,
			"ERROR",
			len(frame.index),
			"Field is missing; unable to validate UUID format.",
		)

	series = frame[field_name].dropna().astype(str)
	mask = ~series.str.fullmatch(UUID_PATTERN)
	failing_count = int(mask.sum())
	status = "PASS" if failing_count == 0 else "FAIL"
	message = "All populated values match the UUID regex." if failing_count == 0 else "One or more values do not match the UUID regex."
	return make_result("format.uuid", field_name, status, failing_count, message)


def validate_numeric_range(frame: pd.DataFrame, field: dict[str, Any]) -> dict[str, Any]:
	field_name = str(field["name"])
	minimum = field.get("minimum")
	maximum = field.get("maximum")
	if field_name not in frame.columns:
		return make_result(
			"range.numeric",
			field_name,
			"ERROR",
			len(frame.index),
			"Field is missing; unable to validate numeric range.",
			metadata={"minimum": minimum, "maximum": maximum},
		)

	series = frame[field_name]
	numeric_series = pd.to_numeric(series, errors="coerce")
	out_of_range = pd.Series(False, index=series.index)
	if minimum is not None:
		out_of_range |= numeric_series < float(minimum)
	if maximum is not None:
		out_of_range |= numeric_series > float(maximum)

	valid_presence = series.notna() & numeric_series.notna()
	failing_count = int((out_of_range & valid_presence).sum())
	status = "PASS" if failing_count == 0 else "FAIL"
	message = "All numeric values are within the contract range." if failing_count == 0 else "One or more numeric values fall outside the contract range."
	return make_result(
		"range.numeric",
		field_name,
		status,
		failing_count,
		message,
		metadata={"minimum": minimum, "maximum": maximum},
	)


def load_baselines() -> dict[str, dict[str, Any]]:
	if not BASELINE_PATH.exists():
		return {}

	payload = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
	return normalize_baselines(payload)


def round_metric(value: float | None) -> float | None:
	if value is None or pd.isna(value):
		return None
	return round(float(value), 6)


def numeric_column_names(frame: pd.DataFrame) -> list[str]:
	numeric_columns: list[str] = []
	for column in frame.columns:
		series = pd.to_numeric(frame[column], errors="coerce")
		if series.notna().any():
			numeric_columns.append(str(column))
	return sorted(numeric_columns)


def build_baselines(frame: pd.DataFrame) -> dict[str, dict[str, Any]]:
	baselines: dict[str, dict[str, Any]] = {}
	for column in numeric_column_names(frame):
		numeric_series = pd.to_numeric(frame[column], errors="coerce").dropna()
		if numeric_series.empty:
			continue
		baselines[column] = {
			"mean": round_metric(float(numeric_series.mean())),
			"stddev": round_metric(float(numeric_series.std(ddof=0))),
			"row_count": int(len(numeric_series.index)),
		}
	return baselines


def save_baselines(baselines: dict[str, dict[str, Any]]) -> None:
	BASELINE_PATH.parent.mkdir(parents=True, exist_ok=True)
	payload = {"columns": baselines, "created_at": datetime.now(timezone.utc).isoformat()}
	BASELINE_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def load_or_initialize_baselines(frame: pd.DataFrame) -> tuple[dict[str, dict[str, Any]], bool]:
	baselines = load_baselines()
	if baselines:
		return baselines, False

	baselines = build_baselines(frame)
	save_baselines(baselines)
	return baselines, True


def normalize_baselines(payload: Any) -> dict[str, dict[str, Any]]:
	if not isinstance(payload, dict):
		return {}

	if "fields" in payload and isinstance(payload["fields"], dict):
		return {str(key): value for key, value in payload["fields"].items() if isinstance(value, dict)}

	if "columns" in payload and isinstance(payload["columns"], dict):
		return {str(key): value for key, value in payload["columns"].items() if isinstance(value, dict)}

	normalized: dict[str, dict[str, Any]] = {}
	for key, value in payload.items():
		if isinstance(value, dict) and ({"mean", "stddev"} & set(value.keys()) or "metrics" in value):
			normalized[str(key)] = value
	return normalized


def extract_baseline_metric(baseline_entry: dict[str, Any], metric_name: str) -> float | None:
	metric_value = baseline_entry.get(metric_name)
	if metric_value is None and isinstance(baseline_entry.get("metrics"), dict):
		metric_value = baseline_entry["metrics"].get(metric_name)
	if metric_value is None:
		return None
	try:
		return float(metric_value)
	except (TypeError, ValueError):
		return None


def validate_statistical_drift(
	frame: pd.DataFrame,
	field_name: str,
	baselines: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
	baseline_entry = baselines.get(field_name)
	if baseline_entry is None:
		return None

	if field_name not in frame.columns:
		return make_result(
			"drift.mean_vs_baseline",
			field_name,
			"ERROR",
			len(frame.index),
			"Field is missing; unable to compare against the baseline.",
		)

	baseline_mean = extract_baseline_metric(baseline_entry, "mean")
	baseline_stddev = extract_baseline_metric(baseline_entry, "stddev")
	current_series = pd.to_numeric(frame[field_name], errors="coerce").dropna()
	current_mean = None if current_series.empty else float(current_series.mean())

	if baseline_mean is None or baseline_stddev is None or current_mean is None:
		return make_result(
			"drift.mean_vs_baseline",
			field_name,
			"ERROR",
			len(frame.index),
			"Insufficient statistics to compare current mean with the baseline.",
			metadata={
				"baseline_mean": baseline_mean,
				"baseline_stddev": baseline_stddev,
				"current_mean": current_mean,
			},
		)

	drift = abs(current_mean - baseline_mean)
	warning_threshold = 2 * baseline_stddev
	critical_threshold = 3 * baseline_stddev
	if drift > critical_threshold:
		status = "FAIL"
		severity = "CRITICAL"
		message = "Current mean exceeds the critical drift threshold."
		failing_count = int(len(current_series.index))
	elif drift > warning_threshold:
		status = "WARNING"
		severity = "WARNING"
		message = "Current mean exceeds the warning drift threshold."
		failing_count = int(len(current_series.index))
	else:
		status = "PASS"
		severity = "INFO"
		message = "Current mean is within the baseline drift thresholds."
		failing_count = 0
	return make_result(
		"drift.mean_vs_baseline",
		field_name,
		status,
		failing_count,
		message,
		severity=severity,
		metadata={
			"baseline_mean": baseline_mean,
			"baseline_stddev": baseline_stddev,
			"current_mean": round(current_mean, 6),
			"drift": round(drift, 6),
			"warning_threshold": round(warning_threshold, 6),
			"critical_threshold": round(critical_threshold, 6),
		},
	)


def validate_contract(contract: dict[str, Any], frame: pd.DataFrame) -> list[dict[str, Any]]:
	results: list[dict[str, Any]] = []
	fields = extract_fields(contract)
	baselines, baselines_initialized = load_or_initialize_baselines(frame)
	contract_field_names = {str(field["name"]) for field in fields}
	numeric_fields = sorted(set(numeric_column_names(frame)) | (set(baselines.keys()) & contract_field_names) | set(baselines.keys()))

	if baselines_initialized:
		results.append(
			make_result(
				"drift.baseline_initialized",
				"*",
				"PASS",
				0,
				"Created schema_snapshots/baselines.json from current numeric columns.",
				metadata={"baseline_path": str(BASELINE_PATH), "column_count": len(baselines)},
			)
		)

	for field in fields:
		if field.get("required"):
			results.append(validate_required_field(frame, field))

		results.append(validate_field_type(frame, field))

		if should_apply_uuid_check(field):
			results.append(validate_uuid_format(frame, field))

		if field.get("type") in NUMERIC_TYPES or field.get("minimum") is not None or field.get("maximum") is not None:
			results.append(validate_numeric_range(frame, field))

	for field_name in numeric_fields:
		drift_result = validate_statistical_drift(frame, field_name, baselines)
		if drift_result is not None:
			results.append(drift_result)

	return results


def build_report(contract_path: Path, data_path: Path, results: list[dict[str, Any]]) -> dict[str, Any]:
	report_id = str(uuid.uuid4())
	return {
		"report_id": report_id,
		"timestamp": datetime.now(timezone.utc).isoformat(),
		"contract_path": str(contract_path),
		"data_path": str(data_path),
		"total_checks": len(results),
		"results": results,
	}


def write_report(report: dict[str, Any]) -> Path:
	reports_dir = PROJECT_ROOT / "validation_reports"
	reports_dir.mkdir(parents=True, exist_ok=True)
	report_path = reports_dir / f"{report['report_id']}.json"
	report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
	return report_path


def main() -> None:
	args = parse_args()
	contract_path = Path(args.contract)
	data_path = Path(args.data)

	contract = load_contract(contract_path)
	records = load_records(data_path)
	frame = flatten_for_profile(records)
	results = validate_contract(contract, frame)
	report = build_report(contract_path, data_path, results)
	report_path = write_report(report)

	print(json.dumps({"report_path": str(report_path), "total_checks": len(results)}, indent=2))


if __name__ == "__main__":
	main()
