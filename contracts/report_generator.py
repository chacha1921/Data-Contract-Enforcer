from __future__ import annotations

import argparse
import json
import textwrap
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml


PROJECT_ROOT = Path(__file__).resolve().parent.parent
VALIDATION_REPORTS_DIR = PROJECT_ROOT / "validation_reports"
VIOLATION_LOG_PATH = PROJECT_ROOT / "violation_log" / "violations.jsonl"
AI_WARNING_LOG_PATH = PROJECT_ROOT / "violation_log" / "ai_warnings.jsonl"
REGISTRY_PATH = PROJECT_ROOT / "contract_registry" / "subscriptions.yaml"
ENFORCER_REPORT_DIR = PROJECT_ROOT / "enforcer_report"
SEVERITY_ORDER = {"CRITICAL": 0, "HIGH": 1, "WARNING": 2, "LOW": 3, "INFO": 4}
RUNNER_STATUS_SEVERITY = {"FAIL": "CRITICAL", "ERROR": "HIGH", "WARNING": "WARNING"}
AI_STATUS_SEVERITY = {
    "embedding_drift": {"FAIL": "CRITICAL", "ERROR": "CRITICAL", "WARN": "HIGH"},
    "prompt_input_validation": {"FAIL": "CRITICAL", "ERROR": "CRITICAL"},
    "llm_output_schema_violation_rate": {"WARN": "HIGH", "FAIL": "CRITICAL", "ERROR": "CRITICAL"},
}


def default_pdf_path() -> Path:
    return ENFORCER_REPORT_DIR / f"report_{datetime.now().strftime('%Y%m%d')}.pdf"


def default_json_path() -> Path:
    return ENFORCER_REPORT_DIR / "report_data.json"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate the Week 7 Enforcer Report from live validation artifacts.")
    parser.add_argument("--validation-dir", default=str(VALIDATION_REPORTS_DIR), help="Directory containing validation and AI report JSON files.")
    parser.add_argument("--registry", default=str(REGISTRY_PATH), help="Path to the contract registry subscriptions YAML.")
    parser.add_argument("--output", default=str(default_pdf_path()), help="Path to write the enforcer PDF report.")
    parser.add_argument("--json-output", required=False, default=str(default_json_path()), help="Path for the structured JSON companion report.")
    return parser.parse_args()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def report_date_label() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def seven_days_ago() -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=7)


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


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            raw = line.strip()
            if not raw or raw.startswith("#"):
                continue
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                records.append(payload)
    return records


def load_registry(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        return []
    subscriptions = payload.get("subscriptions", [])
    return [item for item in subscriptions if isinstance(item, dict)]


def relative_path(path: Path) -> str:
    try:
        return str(path.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path)


def normalize_identifier(value: str) -> str:
    return "".join(char for char in str(value).lower() if char.isalnum())


def normalize_field_name(value: str) -> str:
    return str(value).replace("[*]", "").replace("__", ".").strip(".")


def contract_display_name(contract_id: str) -> str:
    return str(contract_id).replace("-", " ").replace("_", " ").title()


def producer_path_for_contract(contract_id: str) -> str:
    mapping = {
        "week3-document-refinery-extractions": "repos/week3/src",
        "week5-ledger-events": "repos/week5/src",
        "week1-intent-records": "outputs/week1/intent_records.jsonl",
        "week2-verdict-records": "outputs/week2/verdicts.jsonl",
        "langsmith-traces": "outputs/traces/runs.jsonl",
        "week4-cartographer-lineage": "outputs/week4/lineage_snapshots.jsonl",
    }
    return mapping.get(contract_id, "outputs")


def contract_file_for_contract(contract_id: str) -> str:
    mapping = {
        "week1-intent-records": "generated_contracts/week1_intent_records.yaml",
        "week2-verdict-records": "generated_contracts/week2_verdicts.yaml",
        "week3-document-refinery-extractions": "generated_contracts/week3_extractions.yaml",
        "week4-cartographer-lineage": "generated_contracts/week4_lineage.yaml",
        "week5-ledger-events": "generated_contracts/week5_events.yaml",
        "langsmith-traces": "generated_contracts/langsmith_traces.yaml",
        "week7-breaking-demo": "validation_reports/schema_evolution_week7_breaking_demo.json",
    }
    return mapping.get(contract_id, "generated_contracts")


def registry_impacts(registry: list[dict[str, Any]], contract_id: str, field_name: str | None) -> list[dict[str, Any]]:
    impacts: list[dict[str, Any]] = []
    field = normalize_field_name(field_name or "")
    field_root = field.split(".", 1)[0] if field else ""
    contract_key = normalize_identifier(contract_id)
    for subscription in registry:
        if normalize_identifier(str(subscription.get("contract_id", ""))) != contract_key:
            continue
        consumed = {
            normalize_field_name(item)
            for item in subscription.get("fields_consumed", [])
            if isinstance(item, str)
        }
        breaking = {
            normalize_field_name(item.get("field")): str(item.get("reason", ""))
            for item in subscription.get("breaking_fields", [])
            if isinstance(item, dict) and item.get("field")
        }
        if field and field not in consumed and field not in breaking and field_root not in consumed and field_root not in breaking:
            continue
        impacts.append(
            {
                "subscriber_id": subscription.get("subscriber_id"),
                "subscriber_team": subscription.get("subscriber_team"),
                "validation_mode": subscription.get("validation_mode"),
                "reason": breaking.get(field) or breaking.get(field_root) or "Downstream consumer depends on this field.",
                "contact": subscription.get("contact"),
            }
        )
    return impacts


def runner_report_paths(validation_dir: Path) -> list[Path]:
    return sorted(validation_dir.glob("*_runner_compliance.json"))


def schema_evolution_paths(validation_dir: Path) -> list[Path]:
    return sorted(validation_dir.glob("schema_evolution_*.json"))


def build_check_summary(validation_dir: Path) -> dict[str, Any]:
    total_checks = 0
    checks_passed = 0
    for path in runner_report_paths(validation_dir):
        payload = load_json(path)
        total_checks += int(payload.get("total_checks", 0) or 0)
        checks_passed += int(payload.get("passed", 0) or 0)

    ai_reports = [
        load_json(validation_dir / "embedding_drift.json"),
        load_json(validation_dir / "week3_prompt_validation.json"),
        load_json(validation_dir / "week2_verdict_violation_rate.json"),
    ]
    for payload in ai_reports:
        if not payload:
            continue
        total_checks += 1
        if str(payload.get("status", "")).upper() in {"PASS", "BASELINE_SET"}:
            checks_passed += 1

    raw_score = 0.0 if total_checks == 0 else (checks_passed / total_checks) * 100.0
    return {
        "total_checks": total_checks,
        "checks_passed": checks_passed,
        "raw_score": round(raw_score, 2),
    }


def collect_runner_violations(validation_dir: Path, registry: list[dict[str, Any]]) -> list[dict[str, Any]]:
    violations: list[dict[str, Any]] = []
    for path in runner_report_paths(validation_dir):
        payload = load_json(path)
        contract_id = str(payload.get("contract_id", path.stem))
        for result in payload.get("results", []):
            if not isinstance(result, dict):
                continue
            status = str(result.get("status", "")).upper()
            if status not in {"FAIL", "ERROR", "WARNING"}:
                continue
            field_name = str(result.get("column_name") or result.get("field") or "unknown_field")
            impacts = registry_impacts(registry, contract_id, field_name)
            violations.append(
                {
                    "source": "runner",
                    "report_path": relative_path(path),
                    "system": contract_display_name(contract_id),
                    "contract_id": contract_id,
                    "field": field_name,
                    "severity": str(result.get("severity") or RUNNER_STATUS_SEVERITY.get(status, "LOW")).upper(),
                    "status": status,
                    "check_type": result.get("check_type"),
                    "impacts": impacts,
                    "message": result.get("message"),
                }
            )
    return violations


def collect_ai_violations(validation_dir: Path, registry: list[dict[str, Any]]) -> list[dict[str, Any]]:
    files = [
        validation_dir / "embedding_drift.json",
        validation_dir / "week3_prompt_validation.json",
        validation_dir / "week2_verdict_violation_rate.json",
    ]
    violations: list[dict[str, Any]] = []
    for path in files:
        payload = load_json(path)
        if not payload:
            continue
        check_name = str(payload.get("check", path.stem))
        status = str(payload.get("status", "")).upper()
        if status in {"PASS", "BASELINE_SET", ""}:
            continue
        contract_id = "week3-document-refinery-extractions" if check_name in {"embedding_drift", "prompt_input_validation"} else "week2-verdict-records"
        field_name = "extracted_facts[*].text" if check_name == "embedding_drift" else "week3_extraction_prompt_metadata" if check_name == "prompt_input_validation" else "overall_verdict"
        impacts = registry_impacts(registry, contract_id, field_name)
        severity = AI_STATUS_SEVERITY.get(check_name, {}).get(status, "HIGH")
        violations.append(
            {
                "source": "ai_extension",
                "report_path": relative_path(path),
                "system": "Week 3 Extraction AI" if contract_id.startswith("week3") else "Week 2 Verdict AI",
                "contract_id": contract_id,
                "field": field_name,
                "severity": severity,
                "status": status,
                "check_type": check_name,
                "impacts": impacts,
                "message": payload.get("message"),
            }
        )
    return violations


def collect_ai_logged_violations(registry: list[dict[str, Any]]) -> list[dict[str, Any]]:
    violations: list[dict[str, Any]] = []
    for record in load_jsonl(AI_WARNING_LOG_PATH):
        contract_id = str(record.get("contract_id", "week2-verdict-records"))
        field_name = str(record.get("field", "overall_verdict"))
        impacts = registry_impacts(registry, contract_id, field_name)
        violations.append(
            {
                "source": "ai_warning_log",
                "report_path": str(record.get("report_path") or relative_path(AI_WARNING_LOG_PATH)),
                "system": contract_display_name(contract_id),
                "contract_id": contract_id,
                "field": field_name,
                "severity": str(record.get("severity", "WARNING")).upper(),
                "status": str(record.get("status", "WARN")).upper(),
                "check_type": record.get("check_type") or "llm_output_schema_violation_rate",
                "impacts": impacts,
                "message": record.get("message"),
            }
        )
    return violations


def collect_schema_changes(validation_dir: Path) -> list[dict[str, Any]]:
    changes: list[dict[str, Any]] = []
    cutoff = seven_days_ago()
    for path in schema_evolution_paths(validation_dir):
        payload = load_json(path)
        contract_id = str(payload.get("contract_id", path.stem))
        for comparison in payload.get("comparisons", []):
            if not isinstance(comparison, dict):
                continue
            current_ts = comparison.get("current_captured_at")
            if not isinstance(current_ts, str):
                continue
            try:
                parsed = datetime.fromisoformat(current_ts.replace("Z", "+00:00"))
            except ValueError:
                continue
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            if parsed.astimezone(timezone.utc) < cutoff:
                continue
            for change in comparison.get("changes", []):
                if not isinstance(change, dict):
                    continue
                changes.append(
                    {
                        "contract_id": contract_id,
                        "change_type": change.get("change_type"),
                        "summary": change.get("summary") or change.get("human_diff"),
                        "compatibility_verdict": comparison.get("compatibility_verdict"),
                        "required_action": change.get("required_action"),
                        "field": change.get("field") or change.get("from"),
                        "report_path": relative_path(path),
                    }
                )
    return changes


def collect_schema_breaking_violations(validation_dir: Path, registry: list[dict[str, Any]]) -> list[dict[str, Any]]:
    violations: list[dict[str, Any]] = []
    for change in collect_schema_changes(validation_dir):
        if str(change.get("compatibility_verdict", "")).upper() != "BREAKING":
            continue
        contract_id = str(change.get("contract_id"))
        field_name = str(change.get("field") or "schema")
        impacts = registry_impacts(registry, contract_id, field_name)
        violations.append(
            {
                "source": "schema_evolution",
                "report_path": change.get("report_path"),
                "system": contract_display_name(contract_id),
                "contract_id": contract_id,
                "field": field_name,
                "severity": "HIGH",
                "status": "BREAKING",
                "check_type": "schema_change",
                "change_type": change.get("change_type"),
                "impacts": impacts,
                "message": change.get("summary"),
            }
        )
    return violations


def sorted_violations(violations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        violations,
        key=lambda item: (
            SEVERITY_ORDER.get(str(item.get("severity", "INFO")).upper(), 99),
            str(item.get("system", "")),
            str(item.get("field", "")),
        ),
    )


def downstream_impact_text(impacts: list[dict[str, Any]]) -> str:
    if not impacts:
        return "no registered downstream consumers were identified"
    subscribers = ", ".join(str(item.get("subscriber_id")) for item in impacts[:3] if item.get("subscriber_id"))
    return f"downstream consumers affected include {subscribers}"


def plain_language_violation(violation: dict[str, Any]) -> str:
    system = str(violation.get("system", "Unknown System"))
    field_name = str(violation.get("field", "unknown_field"))
    message = str(violation.get("message", "A contract issue was detected.")).rstrip(".")
    impact = downstream_impact_text(violation.get("impacts", []))
    return f"{system} failed on field {field_name}: {message}; {impact}."


def severity_counts(violations: list[dict[str, Any]]) -> dict[str, int]:
    counts = Counter(str(item.get("severity", "INFO")).upper() for item in violations)
    return {severity: counts.get(severity, 0) for severity in ["CRITICAL", "HIGH", "WARNING", "LOW"]}


def build_data_health(violations: list[dict[str, Any]], check_summary: dict[str, Any]) -> dict[str, Any]:
    critical_count = sum(1 for violation in violations if str(violation.get("severity", "")).upper() == "CRITICAL")
    raw_score = float(check_summary.get("raw_score", 0.0) or 0.0)
    adjusted_score = max(0, min(100, round(raw_score - (20 * critical_count))))
    total_checks = int(check_summary.get("total_checks", 0) or 0)
    checks_passed = int(check_summary.get("checks_passed", 0) or 0)
    if not violations:
        narrative = f"Data health is {adjusted_score}/100; all monitored checks passed and no critical violations reduced the score."
    else:
        narrative = (
            f"Data health is {adjusted_score}/100 after {checks_passed} of {total_checks} checks passed, "
            f"with {critical_count} critical violations lowering trust in downstream data use."
        )
    return {
        "score": adjusted_score,
        "narrative": narrative,
        "checks_passed": checks_passed,
        "total_checks": total_checks,
        "critical_violations": critical_count,
        "raw_score": raw_score,
    }


def build_ai_risk_assessment(validation_dir: Path) -> dict[str, Any]:
    aggregate = load_json(validation_dir / "ai_extensions.json")
    embedding = load_json(validation_dir / "embedding_drift.json")
    prompt = load_json(validation_dir / "week3_prompt_validation.json")
    verdict = load_json(validation_dir / "week2_verdict_violation_rate.json")
    status = str(aggregate.get("status", "UNKNOWN")) if aggregate else "UNKNOWN"
    reliable = bool(aggregate.get("ai_system_reliable")) if aggregate else False
    embedding_ok = embedding.get("status") in {"PASS", "BASELINE_SET"}
    verdict_stable = str(verdict.get("trend", "unknown")) in {"stable", "unknown"} and str(verdict.get("status", "")) in {"PASS", "BASELINE_SET"}
    narrative = (
        aggregate.get("risk_assessment")
        if aggregate.get("risk_assessment")
        else (
            f"AI systems are {'reliable' if reliable else 'at risk'}; embedding drift is {'within' if embedding_ok else 'outside'} threshold "
            f"and output schema violations are {'stable' if verdict_stable else 'rising'}."
        )
    )
    return {
        "status": status,
        "narrative": narrative,
        "ai_system_reliable": reliable,
        "embedding_drift_within_threshold": embedding_ok,
        "embedding_drift_score": embedding.get("drift_score"),
        "embedding_threshold": embedding.get("threshold"),
        "prompt_input_validation_status": prompt.get("status"),
        "prompt_invalid_records": prompt.get("invalid_records"),
        "output_schema_violation_rate": verdict.get("violation_rate"),
        "output_schema_violation_trend": verdict.get("trend"),
        "per_prompt_version": verdict.get("per_prompt_version", []),
    }


def recommendation_from_violation(violation: dict[str, Any]) -> str:
    contract_id = str(violation.get("contract_id", "unknown_contract"))
    field_name = normalize_field_name(str(violation.get("field", "unknown_field")))
    contract_file = contract_file_for_contract(contract_id)
    check_type = str(violation.get("check_type", "type"))
    clause = contract_clause_for_violation(contract_id, field_name, check_type, str(violation.get("change_type") or ""))
    if contract_id == "week2-verdict-records" and check_type == "llm_output_schema_violation_rate":
        return f"Update {contract_file} clause {clause} so overall_verdict stays within PASS/FAIL/WARN for the prompt versions called out in the AI warning log."
    return f"Update {contract_file} clause {clause} so field {field_name} satisfies the failing contract check captured in the latest input logs."


def contract_clause_for_violation(contract_id: str, field_name: str, check_type: str, change_type: str = "") -> str:
    dotted_field = normalize_field_name(field_name)
    normalized_check = check_type.lower()
    if normalized_check in {"required", "type", "format", "range"}:
        return f"schema.{dotted_field}.{normalized_check}"
    if normalized_check == "statistical_drift":
        return f"schema.{dotted_field}.statistical_drift"
    if normalized_check == "schema_change":
        if change_type == "add_required_field":
            return f"schema.{dotted_field}.required"
        if change_type in {"narrow_constraints", "confidence_scale_change"}:
            return f"schema.{dotted_field}.range"
        if change_type in {"narrow_type", "change_type"}:
            return f"schema.{dotted_field}.type"
        return f"schema.{dotted_field}.compatibility"
    if normalized_check == "llm_output_schema_violation_rate":
        return "schema.overall_verdict.enum"
    if normalized_check == "prompt_input_validation":
        return "quality.ai.prompt_input_schema.required_fields"
    if normalized_check == "embedding_drift":
        return "quality.ai.embedding_drift.threshold"
    return f"schema.{dotted_field}.type"


def build_recommendations(violations: list[dict[str, Any]], ai_risk: dict[str, Any]) -> list[str]:
    recommendations: list[str] = []
    seen: set[str] = set()
    for violation in sorted_violations(violations):
        action = recommendation_from_violation(violation)
        if action in seen:
            continue
        seen.add(action)
        recommendations.append(action)
        if len(recommendations) == 3:
            return recommendations

    fallback_actions = [
        "Update generated_contracts/week2_verdicts.yaml clause schema.prompt_version.required so AI risk tracking can key on prompt_version instead of falling back to rubric_version.",
        "Update generated_contracts/week3_extractions.yaml clause schema.source_path.required so prompt input validation reads a canonical source path from the contract-governed payload.",
        "Update generated_contracts/week3_extractions.yaml clause schema.extracted_facts.confidence.statistical_drift so live analyzer and runner outputs continue to drive the report narrative from contract evidence.",
    ]
    for action in fallback_actions:
        if action in seen:
            continue
        recommendations.append(action)
        if len(recommendations) == 3:
            break
    return recommendations


def build_json_payload(validation_dir: Path, registry: list[dict[str, Any]]) -> dict[str, Any]:
    runner_violations = collect_runner_violations(validation_dir, registry)
    ai_violations = collect_ai_violations(validation_dir, registry)
    ai_logged_violations = collect_ai_logged_violations(registry)
    schema_violations = collect_schema_breaking_violations(validation_dir, registry)
    violations = sorted_violations(runner_violations + ai_violations + ai_logged_violations + schema_violations)
    check_summary = build_check_summary(validation_dir)
    data_health = build_data_health(violations, check_summary)
    schema_changes = collect_schema_changes(validation_dir)
    ai_risk = build_ai_risk_assessment(validation_dir)
    top_violations = [
        {
            "severity": violation.get("severity"),
            "system": violation.get("system"),
            "field": violation.get("field"),
            "description": plain_language_violation(violation),
            "report_path": violation.get("report_path"),
        }
        for violation in violations[:3]
    ]
    return {
        "generated_at": now_iso(),
        "report_date": report_date_label(),
        "data_health_score": data_health,
        "violations_this_week": {
            "count_by_severity": severity_counts(violations),
            "most_significant": top_violations,
        },
        "schema_changes_detected": schema_changes,
        "ai_system_risk_assessment": ai_risk,
        "recommended_actions": build_recommendations(violations, ai_risk),
        "source_artifacts": {
            "validation_reports": relative_path(validation_dir),
            "violation_log": relative_path(VIOLATION_LOG_PATH),
            "ai_warning_log": relative_path(AI_WARNING_LOG_PATH),
        },
    }


def section_lines(payload: dict[str, Any]) -> list[str]:
    lines = [
        "Week 7 Data Contract Enforcer Report",
        f"Generated: {payload.get('generated_at')}",
        "",
        "Data Health Score",
        f"Score: {payload['data_health_score']['score']}/100",
        str(payload['data_health_score']['narrative']),
        "",
        "Violations this Week",
    ]
    counts = payload['violations_this_week']['count_by_severity']
    lines.append(
        f"Counts by severity: CRITICAL={counts.get('CRITICAL', 0)}, HIGH={counts.get('HIGH', 0)}, WARNING={counts.get('WARNING', 0)}, LOW={counts.get('LOW', 0)}"
    )
    significant = payload['violations_this_week'].get('most_significant', [])
    if significant:
        for item in significant:
            lines.append(f"- {item.get('description')}")
    else:
        lines.append("- No severity-bearing violations were detected in the current reporting window.")

    lines.extend(["", "Schema Changes Detected"])
    schema_changes = payload.get('schema_changes_detected', [])
    if schema_changes:
        for change in schema_changes:
            lines.append(
                f"- {change.get('contract_id')}: {change.get('summary')} Verdict={change.get('compatibility_verdict')}. Action: {change.get('required_action')}"
            )
    else:
        lines.append("- No schema changes were observed in the past 7 days.")

    lines.extend(["", "AI System Risk Assessment"])
    ai = payload['ai_system_risk_assessment']
    lines.append(str(ai.get('narrative', 'AI risk assessment unavailable.')))
    lines.append(
        f"- Embedding drift: score={ai.get('embedding_drift_score')} threshold_ok={ai.get('embedding_drift_within_threshold')}"
    )
    lines.append(
        f"- Output schema violation rate: {ai.get('output_schema_violation_rate')} trend={ai.get('output_schema_violation_trend')}"
    )
    lines.append(
        f"- Prompt input validation: status={ai.get('prompt_input_validation_status')} invalid_records={ai.get('prompt_invalid_records')}"
    )

    lines.extend(["", "Recommended Actions"])
    for action in payload.get('recommended_actions', []):
        lines.append(f"- {action}")
    return lines


def wrap_lines(lines: list[str], width: int = 96) -> list[str]:
    wrapped: list[str] = []
    for line in lines:
        if not line:
            wrapped.append("")
            continue
        wrapped.extend(textwrap.wrap(line, width=width) or [""])
    return wrapped


def escape_pdf_text(text: str) -> str:
    return text.replace('\\', '\\\\').replace('(', '\\(').replace(')', '\\)')


def write_simple_pdf(path: Path, payload: dict[str, Any]) -> None:
    lines = wrap_lines(section_lines(payload))
    pages: list[list[str]] = []
    page_size = 46
    for index in range(0, len(lines), page_size):
        pages.append(lines[index : index + page_size])

    objects: list[bytes] = []
    font_object_id = 3
    page_object_ids: list[int] = []
    content_object_ids: list[int] = []
    next_object_id = 4
    for _ in pages:
        page_object_ids.append(next_object_id)
        next_object_id += 1
        content_object_ids.append(next_object_id)
        next_object_id += 1

    kids = ' '.join(f"{page_id} 0 R" for page_id in page_object_ids)
    objects.append(b"1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n")
    objects.append(f"2 0 obj << /Type /Pages /Kids [{kids}] /Count {len(page_object_ids)} >> endobj\n".encode('utf-8'))
    objects.append(b"3 0 obj << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> endobj\n")

    for page_id, content_id, page_lines in zip(page_object_ids, content_object_ids, pages):
        content_lines = ["BT", "/F1 10 Tf", "50 780 Td", "14 TL"]
        for line_index, line in enumerate(page_lines):
            if line_index > 0:
                content_lines.append("T*")
            content_lines.append(f"({escape_pdf_text(line)}) Tj")
        content_lines.append("ET")
        stream = '\n'.join(content_lines).encode('utf-8')
        objects.append(
            f"{page_id} 0 obj << /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 {font_object_id} 0 R >> >> /Contents {content_id} 0 R >> endobj\n".encode('utf-8')
        )
        objects.append(
            f"{content_id} 0 obj << /Length {len(stream)} >> stream\n".encode('utf-8') + stream + b"\nendstream endobj\n"
        )

    pdf = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for obj in objects:
        offsets.append(len(pdf))
        pdf.extend(obj)
    xref_start = len(pdf)
    pdf.extend(f"xref\n0 {len(objects) + 1}\n".encode('utf-8'))
    pdf.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        pdf.extend(f"{offset:010d} 00000 n \n".encode('utf-8'))
    pdf.extend(f"trailer << /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_start}\n%%EOF\n".encode('utf-8'))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(pdf)


def print_console_summary(payload: dict[str, Any]) -> None:
    data_health = payload['data_health_score']
    print(f"Data Health Score: {data_health['score']}/100")
    print(data_health['narrative'])
    counts = payload['violations_this_week']['count_by_severity']
    print(f"Violations this week: {counts}")
    ai = payload['ai_system_risk_assessment']
    print(
        f"AI Risk: {ai.get('status')} | Reliable={ai.get('ai_system_reliable')} | "
        f"Embedding={ai.get('embedding_drift_score')} | Output Rate={ai.get('output_schema_violation_rate')}"
    )


def main() -> int:
    args = parse_args()
    validation_dir = Path(args.validation_dir)
    registry = load_registry(Path(args.registry))
    pdf_path = Path(args.output)
    json_path = Path(args.json_output)

    payload = build_json_payload(validation_dir, registry)
    write_json(json_path, payload)
    write_simple_pdf(pdf_path, payload)
    print_console_summary(payload)
    print(f"JSON Report: {json_path}")
    print(f"PDF Report: {pdf_path}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
