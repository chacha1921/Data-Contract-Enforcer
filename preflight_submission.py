from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import yaml


PROJECT_ROOT = Path(__file__).resolve().parent
REQUIRED_CONTRACTS = [
    PROJECT_ROOT / "generated_contracts" / "week3_extractions.yaml",
    PROJECT_ROOT / "generated_contracts" / "week3_extractions_dbt.yml",
    PROJECT_ROOT / "generated_contracts" / "week5_events.yaml",
    PROJECT_ROOT / "generated_contracts" / "week5_events_dbt.yml",
]
REQUIRED_OUTPUTS = {
    PROJECT_ROOT / "outputs" / "week3" / "extractions.jsonl": 50,
    PROJECT_ROOT / "outputs" / "week5" / "events.jsonl": 50,
}
REQUIRED_RUNNABLES = [
    PROJECT_ROOT / "contracts" / "generator.py",
    PROJECT_ROOT / "contracts" / "runner.py",
]
OPTIONAL_MIGRATION = PROJECT_ROOT / "outputs" / "migrate" / "align_data.py"
DOMAIN_NOTES_PATH = PROJECT_ROOT / "DOMAIN_NOTES.md"
VALIDATION_REPORTS_DIR = PROJECT_ROOT / "validation_reports"
BASELINE_PATH = PROJECT_ROOT / "schema_snapshots" / "baselines.json"
PLACEHOLDER_MARKERS = [
    "[Example:",
    "Fill in:",
    "[Analysts, downstream services",
    "[What people or systems do with the data]",
    "[Missed SLA, wrong decisions",
]


@dataclass
class CheckResult:
    name: str
    status: str
    message: str
    details: dict[str, Any] | None = None


def make_result(name: str, status: str, message: str, details: dict[str, Any] | None = None) -> CheckResult:
    return CheckResult(name=name, status=status, message=message, details=details)


def count_words(text: str) -> int:
    return len([token for token in text.split() if token.strip()])


def count_jsonl_records(path: Path) -> int:
    count = 0
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            payload = json.loads(stripped)
            if not isinstance(payload, dict):
                raise ValueError(f"Expected JSON object at {path}:{line_number}.")
            count += 1
    return count


def check_domain_notes() -> list[CheckResult]:
    results: list[CheckResult] = []
    if not DOMAIN_NOTES_PATH.exists():
        return [make_result("domain_notes.exists", "FAIL", "DOMAIN_NOTES.md is missing.")]

    text = DOMAIN_NOTES_PATH.read_text(encoding="utf-8")
    word_count = count_words(text)
    required_sections = [
        "## 1.",
        "## 2.",
        "## 3.",
        "## 4.",
        "## 5.",
    ]
    section_hits = sum(1 for section in required_sections if section in text)

    results.append(
        make_result(
            "domain_notes.word_count",
            "PASS" if word_count >= 800 else "FAIL",
            "DOMAIN_NOTES.md meets the minimum word count." if word_count >= 800 else "DOMAIN_NOTES.md is below the 800-word minimum.",
            {"word_count": word_count},
        )
    )
    results.append(
        make_result(
            "domain_notes.sections",
            "PASS" if section_hits == len(required_sections) else "WARNING",
            "DOMAIN_NOTES.md contains all five Phase 0 sections." if section_hits == len(required_sections) else "DOMAIN_NOTES.md is missing one or more Phase 0 section headings.",
            {"section_hits": section_hits, "expected": len(required_sections)},
        )
    )
    placeholder_hits = [marker for marker in PLACEHOLDER_MARKERS if marker in text]
    results.append(
        make_result(
            "domain_notes.placeholder_text",
            "FAIL" if placeholder_hits else "PASS",
            "DOMAIN_NOTES.md still contains template placeholder text." if placeholder_hits else "DOMAIN_NOTES.md does not contain the known template placeholders.",
            {"placeholder_hits": placeholder_hits},
        )
    )
    return results


def parse_yaml_file(path: Path) -> Any:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def check_contract_files() -> list[CheckResult]:
    results: list[CheckResult] = []
    for path in REQUIRED_CONTRACTS:
        if not path.exists():
            results.append(make_result(f"contract.exists::{path.name}", "FAIL", f"Missing required contract file: {path.name}"))
            continue
        try:
            payload = parse_yaml_file(path)
            if not isinstance(payload, dict):
                raise ValueError("Top-level YAML is not a mapping.")
            results.append(make_result(f"contract.exists::{path.name}", "PASS", f"Found readable YAML file: {path.name}"))
            if path.name.endswith("_dbt.yml"):
                continue
            clause_count = count_contract_fields(payload)
            results.append(
                make_result(
                    f"contract.clauses::{path.name}",
                    "PASS" if clause_count >= 8 else "FAIL",
                    "Bitol contract contains at least 8 field clauses." if clause_count >= 8 else "Bitol contract contains fewer than 8 field clauses.",
                    {"field_count": clause_count},
                )
            )
        except Exception as exc:
            results.append(
                make_result(
                    f"contract.exists::{path.name}",
                    "FAIL",
                    f"Contract file exists but is not valid YAML: {path.name}",
                    {"error": str(exc)},
                )
            )
    return results


def count_contract_fields(payload: dict[str, Any]) -> int:
    models = payload.get("models", [])
    if not isinstance(models, list):
        return 0
    field_count = 0
    for model in models:
        if not isinstance(model, dict):
            continue
        fields = model.get("fields", [])
        if isinstance(fields, list):
            field_count += sum(1 for field in fields if isinstance(field, dict))
    return field_count


def check_outputs() -> list[CheckResult]:
    results: list[CheckResult] = []
    for path, minimum_records in REQUIRED_OUTPUTS.items():
        if not path.exists():
            results.append(make_result(f"output.exists::{path.name}", "FAIL", f"Missing required dataset: {path}"))
            continue
        try:
            record_count = count_jsonl_records(path)
            status = "PASS" if record_count >= minimum_records else "FAIL"
            message = (
                f"Dataset contains at least {minimum_records} records."
                if status == "PASS"
                else f"Dataset has fewer than {minimum_records} records."
            )
            results.append(
                make_result(
                    f"output.count::{path.name}",
                    status,
                    message,
                    {"record_count": record_count, "minimum_records": minimum_records},
                )
            )
        except Exception as exc:
            results.append(
                make_result(
                    f"output.count::{path.name}",
                    "FAIL",
                    f"Could not validate dataset: {path}",
                    {"error": str(exc)},
                )
            )
    return results


def check_validation_reports() -> list[CheckResult]:
    if not VALIDATION_REPORTS_DIR.exists():
        return [make_result("validation_reports.exists", "FAIL", "validation_reports/ is missing.")]

    report_files = sorted(VALIDATION_REPORTS_DIR.glob("*.json"))
    if not report_files:
        return [make_result("validation_reports.exists", "FAIL", "No validation report JSON files found.")]

    latest_report = report_files[-1]
    try:
        payload = json.loads(latest_report.read_text(encoding="utf-8"))
    except Exception as exc:
        return [
            make_result(
                "validation_reports.readable",
                "FAIL",
                "Latest validation report is not valid JSON.",
                {"path": str(latest_report), "error": str(exc)},
            )
        ]

    required_keys = {"report_id", "timestamp", "total_checks", "results"}
    missing_keys = sorted(required_keys - set(payload.keys())) if isinstance(payload, dict) else sorted(required_keys)
    status = "PASS" if not missing_keys else "FAIL"
    message = "Validation report JSON is present and has the expected top-level keys." if status == "PASS" else "Validation report JSON is missing expected top-level keys."
    return [
        make_result(
            "validation_reports.schema",
            status,
            message,
            {"path": str(latest_report), "missing_keys": missing_keys},
        )
    ]


def check_runnables() -> list[CheckResult]:
    results: list[CheckResult] = []
    for path in REQUIRED_RUNNABLES:
        results.append(
            make_result(
                f"runnable.exists::{path.name}",
                "PASS" if path.exists() else "FAIL",
                f"Found runnable script: {path.name}" if path.exists() else f"Missing runnable script: {path.name}",
            )
        )
    return results


def check_migration_script() -> list[CheckResult]:
    status = "PASS" if OPTIONAL_MIGRATION.exists() else "WARNING"
    message = (
        "Migration script is present."
        if OPTIONAL_MIGRATION.exists()
        else "Migration script is missing; this is only acceptable if your original systems already emitted the required output format."
    )
    return [make_result("migration_script.exists", status, message)]


def check_baseline_snapshot() -> list[CheckResult]:
    status = "PASS" if BASELINE_PATH.exists() else "WARNING"
    message = (
        "Baseline snapshot exists."
        if BASELINE_PATH.exists()
        else "Baseline snapshot is missing; it will be created on the first real ValidationRunner execution."
    )
    return [make_result("baseline.exists", status, message)]


def run_preflight() -> list[CheckResult]:
    results: list[CheckResult] = []
    results.extend(check_domain_notes())
    results.extend(check_runnables())
    results.extend(check_contract_files())
    results.extend(check_outputs())
    results.extend(check_validation_reports())
    results.extend(check_migration_script())
    results.extend(check_baseline_snapshot())
    return results


def summarize(results: list[CheckResult]) -> dict[str, Any]:
    counts = {"PASS": 0, "WARNING": 0, "FAIL": 0}
    for result in results:
        counts[result.status] = counts.get(result.status, 0) + 1
    ok_to_submit = counts.get("FAIL", 0) == 0
    return {
        "ok_to_submit": ok_to_submit,
        "summary": counts,
        "results": [asdict(result) for result in results],
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check Thursday submission readiness.")
    parser.add_argument("--json", action="store_true", help="Print results as JSON.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    results = run_preflight()
    payload = summarize(results)

    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print(f"Submission ready: {'YES' if payload['ok_to_submit'] else 'NO'}")
        print(f"PASS={payload['summary'].get('PASS', 0)} WARNING={payload['summary'].get('WARNING', 0)} FAIL={payload['summary'].get('FAIL', 0)}")
        for result in payload["results"]:
            print(f"[{result['status']}] {result['name']}: {result['message']}")
            if result.get("details"):
                print(f"  details: {json.dumps(result['details'], ensure_ascii=False)}")

    return 0 if payload["ok_to_submit"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
