from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml


PROJECT_ROOT = Path(__file__).resolve().parent.parent
VALIDATION_REPORTS_DIR = PROJECT_ROOT / "validation_reports"
REGISTRY_PATH = PROJECT_ROOT / "contract_registry" / "subscriptions.yaml"
DEFAULT_OUTPUT_PATH = PROJECT_ROOT / "enforcer_report" / "report_data.json"
SEVERITY_DEDUCTIONS = {
	"CRITICAL": 20,
	"HIGH": 10,
	"MEDIUM": 5,
	"LOW": 1,
}
SEVERITY_ORDER = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3, "INFO": 4}
RUNNER_FAIL_SEVERITY = {"FAIL": "CRITICAL", "ERROR": "HIGH", "WARN": "MEDIUM"}


def parse_args() -> argparse.Namespace:
	parser = argparse.ArgumentParser(
		description="Aggregate validation metrics into a stakeholder-ready health report."
	)
	parser.add_argument(
		"--validation-dir",
		default=str(VALIDATION_REPORTS_DIR),
		help="Directory containing validation JSON reports.",
	)
	parser.add_argument(
		"--registry",
		default=str(REGISTRY_PATH),
		help="Path to the contract registry subscriptions file.",
	)
	parser.add_argument(
		"--output",
		default=str(DEFAULT_OUTPUT_PATH),
		help="Path to write the final stakeholder report JSON.",
	)
	return parser.parse_args()


def now_iso() -> str:
	return datetime.now(timezone.utc).isoformat()


def write_json(path: Path, payload: dict[str, Any]) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def load_json(path: Path) -> dict[str, Any]:
	if not path.exists():
		return {}
	try:
		payload = json.loads(path.read_text(encoding="utf-8"))
	except json.JSONDecodeError:
		return {}
	return payload if isinstance(payload, dict) else {}


def load_registry(path: Path) -> list[dict[str, Any]]:
	if not path.exists():
		return []
	payload = yaml.safe_load(path.read_text(encoding="utf-8"))
	if not isinstance(payload, dict):
		return []
	subscriptions = payload.get("subscriptions", [])
	return subscriptions if isinstance(subscriptions, list) else []


def relative_path(path: Path) -> str:
	try:
		return str(path.relative_to(PROJECT_ROOT))
	except ValueError:
		return str(path)


def contract_display_name(contract_id: str) -> str:
	normalized = re.sub(r"([A-Za-z]+)(\d+)", r"\1 \2", str(contract_id))
	parts = [part for part in normalized.split("_") if part]
	return " ".join(part.capitalize() for part in parts) if parts else "Unknown Contract"


def contract_id_from_path(raw_path: Any) -> str | None:
	if not isinstance(raw_path, str) or not raw_path.strip():
		return None
	return Path(raw_path).stem or None


def infer_data_path(contract_id: str) -> str | None:
	mapping = {
		"week2_verdicts": PROJECT_ROOT / "outputs" / "week2" / "verdicts.jsonl",
		"week3_extractions": PROJECT_ROOT / "outputs" / "week3" / "extractions.jsonl",
		"week4_lineage_snapshots": PROJECT_ROOT / "outputs" / "week4" / "lineage_snapshots.jsonl",
		"week5_events": PROJECT_ROOT / "outputs" / "week5" / "events.jsonl",
	}
	path = mapping.get(contract_id)
	return relative_path(path) if path else None


def infer_contract_path(contract_id: str) -> str | None:
	path = PROJECT_ROOT / "generated_contracts" / f"{contract_id}.yaml"
	return relative_path(path) if path.exists() else None


def normalize_field_name(field_name: str | None) -> str:
	if not field_name:
		return ""
	return str(field_name).replace(".", "__")


def leaf_field_name(field_name: str | None) -> str:
	normalized = normalize_field_name(field_name)
	if not normalized:
		return "record"
	return normalized.split("__")[-1]


def find_impacts(
	registry: list[dict[str, Any]], contract_id: str, field_name: str | None
) -> list[dict[str, str]]:
	normalized_field = normalize_field_name(field_name)
	impacts: list[dict[str, str]] = []
	for subscription in registry:
		if subscription.get("contract_id") != contract_id:
			continue

		consumed = {
			normalize_field_name(item)
			for item in subscription.get("fields_consumed", [])
			if isinstance(item, str)
		}
		breaking_reasons = {}
		for item in subscription.get("breaking_fields", []):
			if not isinstance(item, dict):
				continue
			field = normalize_field_name(item.get("field"))
			if field:
				breaking_reasons[field] = str(item.get("reason", "")).strip()

		if normalized_field and normalized_field not in consumed and normalized_field not in breaking_reasons:
			continue

		impact = {
			"subscriber_id": str(subscription.get("subscriber_id", "unknown_subscriber")),
			"validation_mode": str(subscription.get("validation_mode", "UNKNOWN")),
			"reason": breaking_reasons.get(normalized_field, "").strip(),
		}
		impacts.append(impact)
	return impacts


def severity_from_result(result: dict[str, Any]) -> str:
	severity = str(result.get("severity", "")).upper()
	if severity in SEVERITY_DEDUCTIONS:
		return severity
	if severity == "ERROR":
		return "HIGH"
	status = str(result.get("status", "")).upper()
	return RUNNER_FAIL_SEVERITY.get(status, "LOW")


def normalize_check_type(raw_check: Any) -> str:
	check = str(raw_check or "validation")
	mapping = {
		"structural.required": "required",
		"type.match": "type_conformance",
		"range.numeric": "range_enforcement",
		"drift.baseline_initialized": "drift_baseline_initialized",
	}
	if check in mapping:
		return mapping[check]
	return check.replace(".", "_")


def range_phrase(expected: Any, actual: Any) -> str:
	if isinstance(expected, dict):
		minimum = expected.get("minimum")
		maximum = expected.get("maximum")
		if minimum is not None and maximum is not None:
			return f" Expected {minimum}-{maximum} but found {actual}."
	return ""


def brief_value(value: Any, limit: int = 120) -> Any:
	if value is None:
		return value
	text = str(value)
	if len(text) <= limit:
		return text
	return f"{text[:limit].rstrip()}..."


def business_translation(violation: dict[str, Any]) -> str:
	contract_name = contract_display_name(violation.get("contract_id", "unknown_contract"))
	field_name = leaf_field_name(violation.get("field"))
	check_type = str(violation.get("check_type", "issue"))
	expected = violation.get("expected")
	actual = brief_value(violation.get("actual"))
	impacts = violation.get("impacts", [])
	record_count = violation.get("affected_records")

	if check_type == "range_enforcement":
		sentence = (
			f"The '{field_name}' field in {contract_name} failed its range check."
			f"{range_phrase(expected, actual)}"
		)
	elif check_type.startswith("format_"):
		format_name = check_type.split("format_", maxsplit=1)[-1].replace("-", " ")
		sentence = (
			f"The '{field_name}' field in {contract_name} failed its {format_name} format check."
			f" Expected {expected} but found {actual}."
		)
	elif check_type == "type_conformance":
		sentence = f"The '{field_name}' field in {contract_name} contains values that do not match the agreed type."
		if expected is not None and actual is not None:
			sentence += f" Expected {expected} but found {actual}."
		elif expected is not None:
			sentence += f" Expected {expected}."
	elif check_type == "required":
		sentence = (
			f"The '{field_name}' field in {contract_name} is missing required values."
			f" The report observed {actual} missing values."
		)
	elif check_type.startswith("change_"):
		change_name = check_type.replace("_", " ")
		sentence = f"{contract_name} introduced a {change_name} on '{field_name}', which is classified as a breaking schema change."
	elif check_type == "prompt_input_validation":
		sentence = (
			f"Prompt input validation for {contract_name} found records that do not match the Draft-07 schema."
			f" {actual} records were quarantined."
		)
	elif check_type == "llm_output_violation_rate":
		sentence = (
			f"The LLM verdict output for {contract_name} exceeded the allowed violation rate."
			f" Observed {actual}% against baseline {expected}%."
		)
	elif check_type == "embedding_drift":
		sentence = (
			f"The embedding centroid for {contract_name} drifted beyond the acceptable threshold."
			f" Observed distance {actual} against threshold {expected}."
		)
	else:
		sentence = (
			f"{contract_name} reported a {check_type.replace('_', ' ')} issue on '{field_name}'."
			f" Expected {expected} but found {actual}."
		)

	if isinstance(record_count, int) and record_count > 0:
		sentence += f" This affects {record_count} records."

	if impacts:
		impact_text = ", ".join(
			impact.get("reason") or impact.get("subscriber_id", "unknown_subscriber")
			for impact in impacts[:2]
		)
		sentence += f" This impacts {impact_text}."

	return " ".join(sentence.split())


def flatten_runner_report(
	payload: dict[str, Any], report_path: Path, registry: list[dict[str, Any]]
) -> list[dict[str, Any]]:
	contract_id = str(
		payload.get("contract_id")
		or contract_id_from_path(payload.get("contract_path"))
		or report_path.stem
	)
	violations: list[dict[str, Any]] = []
	for result in payload.get("results", []):
		if not isinstance(result, dict):
			continue
		status = str(result.get("status", "")).upper()
		if status not in {"FAIL", "ERROR", "WARN"}:
			continue
		field_name = str(result.get("column_name") or result.get("field") or "")
		metadata = result.get("metadata", {}) if isinstance(result.get("metadata"), dict) else {}
		check_type = normalize_check_type(result.get("check_type") or result.get("check"))
		expected = result.get("expected")
		if expected is None:
			if check_type == "type_conformance":
				expected = metadata.get("expected_type")
			elif check_type == "range_enforcement":
				expected = {
					"minimum": metadata.get("minimum"),
					"maximum": metadata.get("maximum"),
				}
		affected_records = result.get("failing_record_count")
		actual = result.get("actual_value")
		if actual is None and isinstance(affected_records, int) and affected_records > 0:
			actual = affected_records
		violation = {
			"source_report": relative_path(report_path),
			"report_kind": "runner",
			"contract_id": contract_id,
			"severity": severity_from_result(result),
			"field": field_name,
			"check_type": check_type,
			"status": status,
			"expected": expected,
			"actual": actual,
			"message": str(result.get("message", "")).strip(),
			"run_timestamp": payload.get("run_timestamp") or payload.get("timestamp"),
			"impacts": find_impacts(registry, contract_id, field_name),
			"affected_records": affected_records if isinstance(affected_records, int) else None,
			"contract_path": infer_contract_path(contract_id),
			"data_path": infer_data_path(contract_id),
		}
		violation["business_summary"] = business_translation(violation)
		violations.append(violation)
	return violations


def flatten_schema_report(
	payload: dict[str, Any], report_path: Path, registry: list[dict[str, Any]]
) -> list[dict[str, Any]]:
	contract_id = str(payload.get("contract_id", report_path.stem))
	migration_impact = payload.get("migration_impact", {})
	if not isinstance(migration_impact, dict):
		return []

	violations: list[dict[str, Any]] = []
	for change in migration_impact.get("diff", []):
		if not isinstance(change, dict):
			continue
		compatibility = str(change.get("compatibility", "")).upper()
		severity = "HIGH" if compatibility == "BREAKING" else "LOW"
		field_name = str(change.get("field", "schema"))
		details = change.get("details", {})
		violation = {
			"source_report": relative_path(report_path),
			"report_kind": "schema_migration",
			"contract_id": contract_id,
			"severity": severity,
			"field": field_name,
			"check_type": str(change.get("change_type", "schema_change")),
			"status": compatibility or "INFO",
			"expected": details.get("from") if isinstance(details, dict) else None,
			"actual": details.get("to") if isinstance(details, dict) else None,
			"message": f"Schema change classified as {compatibility or 'UNKNOWN'}.",
			"run_timestamp": migration_impact.get("analyzed_at"),
			"impacts": find_impacts(registry, contract_id, field_name),
			"contract_path": infer_contract_path(contract_id),
			"data_path": infer_data_path(contract_id),
		}
		violation["business_summary"] = business_translation(violation)
		violations.append(violation)
	return violations


def flatten_ai_report(
	payload: dict[str, Any], report_path: Path, registry: list[dict[str, Any]]
) -> list[dict[str, Any]]:
	check = str(payload.get("check", report_path.stem))
	status = str(payload.get("status", "")).upper()
	if status in {"PASS", "BASELINE_CREATED", ""}:
		return []

	contract_id = "week3_extractions" if check == "prompt_input_validation" else "week2_verdicts"
	severity = "HIGH" if status in {"FAIL", "ERROR"} else "MEDIUM"
	actual: Any = None
	expected: Any = None
	field = "system"
	affected_records: int | None = None

	if check == "prompt_input_validation":
		field = "prompt_input"
		actual = payload.get("invalid_records")
		expected = 0
		affected_records = int(actual) if isinstance(actual, int) else None
	elif check == "llm_output_violation_rate":
		field = "overall_verdict"
		actual = payload.get("violation_rate")
		expected = payload.get("baseline_rate", 2.0)
	elif check == "embedding_drift":
		field = "embedding_centroid"
		actual = payload.get("cosine_distance")
		expected = payload.get("threshold")

	violation = {
		"source_report": relative_path(report_path),
		"report_kind": "ai_extension",
		"contract_id": contract_id,
		"severity": severity,
		"field": field,
		"check_type": check,
		"status": status,
		"expected": expected,
		"actual": actual,
		"message": str(payload.get("message", "")).strip(),
		"run_timestamp": payload.get("run_timestamp"),
		"impacts": find_impacts(registry, contract_id, field),
		"affected_records": affected_records,
		"contract_path": infer_contract_path(contract_id),
		"data_path": infer_data_path(contract_id),
	}
	violation["business_summary"] = business_translation(violation)
	return [violation]


def collect_violations(
	validation_dir: Path, registry: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
	violations: list[dict[str, Any]] = []
	processed_reports: list[dict[str, Any]] = []
	for report_path in sorted(validation_dir.glob("*.json")):
		payload = load_json(report_path)
		processed_reports.append(
			{
				"path": relative_path(report_path),
				"loaded": bool(payload),
				"report_kind": detect_report_kind(payload, report_path),
			}
		)
		if not payload:
			continue

		report_kind = detect_report_kind(payload, report_path)
		if report_kind == "runner":
			violations.extend(flatten_runner_report(payload, report_path, registry))
		elif report_kind == "schema_migration":
			violations.extend(flatten_schema_report(payload, report_path, registry))
		elif report_kind == "ai_extension":
			violations.extend(flatten_ai_report(payload, report_path, registry))

	return violations, processed_reports


def detect_report_kind(payload: dict[str, Any], report_path: Path) -> str:
	if isinstance(payload.get("results"), list):
		return "runner"
	if isinstance(payload.get("migration_impact"), dict):
		return "schema_migration"
	if payload.get("check"):
		return "ai_extension"
	return report_path.stem


def calculate_health_score(violations: list[dict[str, Any]]) -> tuple[int, dict[str, int]]:
	score = 100
	counts = {severity: 0 for severity in ["CRITICAL", "HIGH", "MEDIUM", "LOW"]}
	for violation in violations:
		severity = str(violation.get("severity", "LOW")).upper()
		if severity not in SEVERITY_DEDUCTIONS:
			continue
		counts[severity] += 1
		score -= SEVERITY_DEDUCTIONS[severity]
	return max(score, 0), counts


def extract_period(violations: list[dict[str, Any]]) -> dict[str, str | None]:
	timestamps = []
	for violation in violations:
		run_timestamp = violation.get("run_timestamp")
		if isinstance(run_timestamp, str) and run_timestamp.strip():
			timestamps.append(run_timestamp)
	if not timestamps:
		now = now_iso()
		return {"start": now, "end": now}
	return {"start": min(timestamps), "end": max(timestamps)}


def health_narrative(score: int, counts: dict[str, int], violations: list[dict[str, Any]]) -> str:
	if not violations:
		return "All loaded validation reports are clean. No severity-bearing violations were found in the current reporting window."

	most_severe = next(
		(
			severity
			for severity in ["CRITICAL", "HIGH", "MEDIUM", "LOW"]
			if counts.get(severity, 0) > 0
		),
		"LOW",
	)
	total = sum(counts.values())
	return (
		f"Data health score is {score}/100 after processing {total} severity-bearing findings across the validation reports. "
		f"The current risk posture is driven primarily by {counts.get('CRITICAL', 0)} critical and {counts.get('HIGH', 0)} high-severity issues, "
		f"with {most_severe.lower()} severity currently setting the stakeholder priority level."
	)


def build_ai_risk(validation_dir: Path) -> dict[str, Any]:
	aggregate_path = validation_dir / "ai_extensions.json"
	aggregate_payload = load_json(aggregate_path)
	if aggregate_payload:
		embedding_payload = aggregate_payload.get("embedding_drift", {})
		llm_payload = aggregate_payload.get("llm_output_violation_rate", {})
		return {
			"status": str(aggregate_payload.get("status", "AVAILABLE")),
			"source": relative_path(aggregate_path),
			"embedding_drift_score": embedding_payload.get("cosine_distance"),
			"embedding_status": embedding_payload.get("status"),
			"llm_output_violation_rate": llm_payload.get("violation_rate"),
			"llm_status": llm_payload.get("status"),
			"message": str(aggregate_payload.get("message", "AI Extensions summary loaded.")),
		}

	embedding_candidates = sorted(validation_dir.glob("*embedding*.json"))
	embedding_payload = load_json(embedding_candidates[0]) if embedding_candidates else {}
	llm_payload = load_json(validation_dir / "week2_verdict_violation_rate.json")

	if not embedding_payload:
		return {
			"status": "SKIPPED",
			"source": relative_path(aggregate_path),
			"embedding_drift_score": None,
			"embedding_status": "SKIPPED",
			"llm_output_violation_rate": llm_payload.get("violation_rate"),
			"llm_status": llm_payload.get("status"),
			"message": "AI Extensions: Execution skipped (Prerequisites missing).",
		}

	return {
		"status": "PARTIAL",
		"source": relative_path(embedding_candidates[0]) if embedding_candidates else relative_path(aggregate_path),
		"embedding_drift_score": embedding_payload.get("cosine_distance"),
		"embedding_status": embedding_payload.get("status"),
		"llm_output_violation_rate": llm_payload.get("violation_rate"),
		"llm_status": llm_payload.get("status"),
		"message": "AI Extensions metrics were assembled from individual report files.",
	}


def build_recommendations(violations: list[dict[str, Any]]) -> list[dict[str, Any]]:
	recommendations: list[dict[str, Any]] = []
	seen_actions: set[tuple[str, str]] = set()
	for violation in sorted_violations(violations):
		contract_id = str(violation.get("contract_id", "unknown_contract"))
		field = leaf_field_name(violation.get("field"))
		contract_path = violation.get("contract_path") or "generated_contracts"
		data_path = violation.get("data_path") or "outputs"
		key = (contract_id, field)
		if key in seen_actions:
			continue
		seen_actions.add(key)

		recommendations.append(
			{
				"priority": len(recommendations) + 1,
				"contract_id": contract_id,
				"action": (
					f"Review contract {contract_id} for field '{field}' in {contract_path}, align the producer data in {data_path}, "
					f"and rerun the failing checks captured in {violation.get('source_report')}"
				),
				"reason": violation.get("business_summary"),
			}
		)
		if len(recommendations) == 3:
			break

	if len(recommendations) < 3:
		fallbacks = [
			{
				"priority": len(recommendations) + 1,
				"contract_id": "week3_extractions",
				"action": "Re-run contracts/runner.py for generated_contracts/week3_extractions.yaml against outputs/week3/extractions.jsonl to confirm the current failures are resolved.",
				"reason": "Week 3 extraction quality is a dependency for downstream lineage and enforcement checks.",
			},
			{
				"priority": len(recommendations) + 2,
				"contract_id": "week5_events",
				"action": "Review generated_contracts/week5_events.yaml and correct producer field formatting in outputs/week5/events.jsonl before the next enforcement run.",
				"reason": "Week 5 event identifiers are currently the largest source of critical contract failures.",
			},
			{
				"priority": len(recommendations) + 3,
				"contract_id": "week2_verdicts",
				"action": "Run contracts/ai_extensions.py verdict-violation-rate against outputs/week2/verdicts.jsonl after any model changes to keep the baseline current.",
				"reason": "The stakeholder report should keep AI quality baselines fresh even when no new violations are present.",
			},
		]
		for item in fallbacks:
			if len(recommendations) == 3:
				break
			recommendations.append(item)

	for index, recommendation in enumerate(recommendations, start=1):
		recommendation["priority"] = index
	return recommendations[:3]


def sorted_violations(violations: list[dict[str, Any]]) -> list[dict[str, Any]]:
	return sorted(
		violations,
		key=lambda item: (
			SEVERITY_ORDER.get(str(item.get("severity", "INFO")).upper(), 99),
			str(item.get("contract_id", "")),
			str(item.get("field", "")),
		),
	)


def select_top_violations(violations: list[dict[str, Any]], limit: int = 5) -> list[dict[str, Any]]:
	top_items = []
	for violation in sorted_violations(violations)[:limit]:
		top_items.append(
			{
				"contract_id": violation.get("contract_id"),
				"severity": violation.get("severity"),
				"field": violation.get("field"),
				"check_type": violation.get("check_type"),
				"source_report": violation.get("source_report"),
				"business_summary": violation.get("business_summary"),
				"contract_path": violation.get("contract_path"),
				"data_path": violation.get("data_path"),
			}
		)
	return top_items


def print_console_summary(report_payload: dict[str, Any]) -> None:
	print(f"Data Health Score: {report_payload['data_health_score']}/100")
	print(report_payload["health_narrative"])
	for violation in report_payload.get("top_violations", [])[:3]:
		print(
			f"- {violation.get('severity')}: {violation.get('contract_id')} / {violation.get('field')} -> {violation.get('check_type')}"
		)


def main() -> int:
	args = parse_args()
	validation_dir = Path(args.validation_dir)
	registry_path = Path(args.registry)
	output_path = Path(args.output)

	registry = load_registry(registry_path)
	violations, processed_reports = collect_violations(validation_dir, registry)
	score, severity_counts = calculate_health_score(violations)
	period = extract_period(violations)
	report_payload = {
		"generated_at": now_iso(),
		"period": period,
		"data_health_score": score,
		"health_narrative": health_narrative(score, severity_counts, violations),
		"top_violations": select_top_violations(violations),
		"ai_risk": build_ai_risk(validation_dir),
		"recommendations": build_recommendations(violations),
	}
	write_json(output_path, report_payload)
	print_console_summary(report_payload)
	return 0


if __name__ == "__main__":
	raise SystemExit(main())