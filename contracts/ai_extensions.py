from __future__ import annotations

import argparse
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
from jsonschema import Draft7Validator
from openai import OpenAI


PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = PROJECT_ROOT / ".env"
VALIDATION_REPORTS_DIR = PROJECT_ROOT / "validation_reports"
AGGREGATE_AI_REPORT = VALIDATION_REPORTS_DIR / "ai_extensions.json"
DEFAULT_WEEK3_PATH = PROJECT_ROOT / "outputs" / "week3" / "extractions.jsonl"
DEFAULT_WEEK2_PATH = PROJECT_ROOT / "outputs" / "week2" / "verdicts.jsonl"
DEFAULT_QUARANTINE_DIR = PROJECT_ROOT / "outputs" / "quarantine"
DEFAULT_EMBEDDING_BASELINE = PROJECT_ROOT / "schema_snapshots" / "embedding_baselines.npz"
DEFAULT_LLM_RATE_BASELINE = PROJECT_ROOT / "schema_snapshots" / "llm_output_violation_baseline.json"
DEFAULT_EMBEDDING_OUTPUT = VALIDATION_REPORTS_DIR / "embedding_drift.json"
DEFAULT_PROMPT_OUTPUT = VALIDATION_REPORTS_DIR / "week3_prompt_validation.json"
DEFAULT_VERDICT_OUTPUT = VALIDATION_REPORTS_DIR / "week2_verdict_violation_rate.json"
EXPECTED_VERDICTS = {"PASS", "FAIL", "WARN"}
EMBEDDING_MODEL = "text-embedding-3-small"
PROMPT_INPUT_NAMESPACE = uuid.UUID("7b0a0600-9677-4bc9-af8d-0f7d779f1dad")


PROMPT_INPUT_SCHEMA: dict[str, Any] = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "type": "object",
    "required": ["doc_id", "source_path", "content_preview"],
    "properties": {
        "doc_id": {"type": "string", "minLength": 36, "maxLength": 36},
        "source_path": {"type": "string", "minLength": 1},
        "content_preview": {"type": "string", "maxLength": 8000},
    },
    "additionalProperties": False,
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="AI-specific contract extension checks.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    embedding = subparsers.add_parser("embedding-drift", help="Check centroid drift for Week 3 embedded text.")
    embedding.add_argument("--input", default=str(DEFAULT_WEEK3_PATH), help="Path to Week 3 JSONL input.")
    embedding.add_argument("--baseline", default=str(DEFAULT_EMBEDDING_BASELINE), help="Path to embedding baseline NPZ file.")
    embedding.add_argument("--output", default=str(DEFAULT_EMBEDDING_OUTPUT), help="Path to the JSON result output.")
    embedding.add_argument("--threshold", type=float, default=0.15, help="Cosine distance alert threshold.")
    embedding.add_argument("--sample-size", type=int, default=200, help="Maximum number of text samples to embed.")

    prompt = subparsers.add_parser("prompt-validation", help="Validate Week 3 prompt inputs against the prompt metadata schema.")
    prompt.add_argument("--input", default=str(DEFAULT_WEEK3_PATH), help="Path to Week 3 JSONL input.")
    prompt.add_argument("--output", default=str(DEFAULT_PROMPT_OUTPUT), help="Path to the JSON result output.")
    prompt.add_argument("--quarantine-dir", default=str(DEFAULT_QUARANTINE_DIR), help="Directory to write quarantined records.")

    verdict = subparsers.add_parser("verdict-violation-rate", help="Check Week 2 output schema violation rate per prompt version.")
    verdict.add_argument("--input", default=str(DEFAULT_WEEK2_PATH), help="Path to Week 2 verdicts JSONL input.")
    verdict.add_argument("--baseline", default=str(DEFAULT_LLM_RATE_BASELINE), help="Path to verdict-rate baseline JSON file.")
    verdict.add_argument("--output", default=str(DEFAULT_VERDICT_OUTPUT), help="Path to the JSON result output.")
    verdict.add_argument("--warn-threshold", type=float, default=0.02, help="Warning threshold for schema violation rate.")

    run_all = subparsers.add_parser("run-all", help="Execute all AI extension checks and refresh the aggregate output.")
    run_all.add_argument("--week3-input", default=str(DEFAULT_WEEK3_PATH), help="Path to Week 3 JSONL input.")
    run_all.add_argument("--week2-input", default=str(DEFAULT_WEEK2_PATH), help="Path to Week 2 verdicts JSONL input.")
    run_all.add_argument("--output-dir", default=str(VALIDATION_REPORTS_DIR), help="Directory for AI extension result files.")
    run_all.add_argument("--quarantine-dir", default=str(DEFAULT_QUARANTINE_DIR), help="Directory to write quarantined records.")
    run_all.add_argument("--embedding-baseline", default=str(DEFAULT_EMBEDDING_BASELINE), help="Path to embedding baseline NPZ file.")
    run_all.add_argument("--verdict-baseline", default=str(DEFAULT_LLM_RATE_BASELINE), help="Path to verdict-rate baseline JSON file.")
    run_all.add_argument("--threshold", type=float, default=0.15, help="Cosine distance alert threshold.")
    run_all.add_argument("--warn-threshold", type=float, default=0.02, help="Warning threshold for schema violation rate.")
    run_all.add_argument("--sample-size", type=int, default=200, help="Maximum number of text samples to embed.")
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


def load_jsonl_records(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        raise FileNotFoundError(f"Input file not found: {path}")
    records: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            raw = line.strip()
            if not raw:
                continue
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise ValueError(f"Expected one JSON object per line in {path} at line {line_number}.")
            records.append(payload)
    return records


def load_dotenv_values(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    with path.open("r", encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", maxsplit=1)
            cleaned_key = key.strip()
            cleaned_value = value.strip().strip('"').strip("'")
            if cleaned_key:
                values[cleaned_key] = cleaned_value
    return values


def resolve_api_configuration() -> tuple[str | None, str | None]:
    dotenv_values = load_dotenv_values(ENV_PATH)
    api_key = (
        os.getenv("OPENAI_API_KEY")
        or os.getenv("API_KEY")
        or dotenv_values.get("OPENAI_API_KEY")
        or dotenv_values.get("API_KEY")
    )
    base_url = os.getenv("OPENAI_BASE_URL") or dotenv_values.get("OPENAI_BASE_URL")
    if api_key and api_key.startswith("sk-or-") and not base_url:
        base_url = "https://openrouter.ai/api/v1"
    return api_key, base_url


def stable_doc_id(record: dict[str, Any]) -> str | None:
    raw_doc_id = record.get("doc_id")
    if isinstance(raw_doc_id, str) and len(raw_doc_id) == 36:
        return raw_doc_id
    seed = record.get("source_doc_id") or record.get("doc_id") or record.get("block_id")
    if not isinstance(seed, str) or not seed.strip():
        return None
    return str(uuid.uuid5(PROMPT_INPUT_NAMESPACE, seed.strip()))


def derive_prompt_input(record: dict[str, Any]) -> dict[str, Any]:
    prompt_input: dict[str, Any] = {}
    doc_id = stable_doc_id(record)
    source_path = record.get("source_path") or record.get("source_doc_id") or record.get("doc_id")
    content_preview = record.get("content_preview") or record.get("text_preview")

    if doc_id is not None:
        prompt_input["doc_id"] = doc_id
    if isinstance(source_path, str) and source_path.strip():
        prompt_input["source_path"] = source_path.strip()
    if isinstance(content_preview, str):
        prompt_input["content_preview"] = content_preview
    return prompt_input


def quarantine_path(quarantine_dir: Path) -> Path:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    return quarantine_dir / f"prompt_input_quarantine_{timestamp}.jsonl"


def prompt_validation_errors(record: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    prompt_input = derive_prompt_input(record)
    validator = Draft7Validator(PROMPT_INPUT_SCHEMA)
    errors = [error.message for error in sorted(validator.iter_errors(prompt_input), key=lambda item: list(item.path))]
    return prompt_input, errors


def run_prompt_validation(input_path: Path, quarantine_dir: Path, output_path: Path) -> dict[str, Any]:
    records = load_jsonl_records(input_path)
    invalid_records: list[dict[str, Any]] = []
    valid_records = 0
    for index, record in enumerate(records):
        prompt_input, errors = prompt_validation_errors(record)
        if errors:
            invalid_records.append(
                {
                    "record_index": index,
                    "errors": errors,
                    "prompt_input": prompt_input,
                    "raw_record": record,
                }
            )
            continue
        valid_records += 1

    quarantine_file = quarantine_path(quarantine_dir)
    quarantine_file.parent.mkdir(parents=True, exist_ok=True)
    with quarantine_file.open("w", encoding="utf-8") as handle:
        for record in invalid_records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    payload = {
        "check": "prompt_input_validation",
        "status": "PASS" if not invalid_records else "FAIL",
        "schema": PROMPT_INPUT_SCHEMA,
        "total_records": len(records),
        "valid_records": valid_records,
        "invalid_records": len(invalid_records),
        "quarantine_file": str(quarantine_file),
        "monitored_object": "week3_extraction_prompt_metadata",
        "run_timestamp": now_iso(),
        "message": (
            "All derived Week 3 prompt metadata objects conform to the prompt input schema."
            if not invalid_records
            else "Non-conforming prompt metadata records were quarantined and must not be silently passed downstream."
        ),
    }
    write_json(output_path, payload)
    return payload


def extract_embedding_text_candidates(records: list[dict[str, Any]]) -> tuple[list[str], str]:
    texts: list[str] = []
    resolved_field = "extracted_facts[*].value"
    for record in records:
        extracted_facts = record.get("extracted_facts", [])
        if not isinstance(extracted_facts, list):
            continue
        for item in extracted_facts:
            if not isinstance(item, dict):
                continue
            text_value = item.get("text")
            if isinstance(text_value, str) and text_value.strip():
                resolved_field = "extracted_facts[*].text"
                texts.append(text_value)
                continue
            fallback_value = item.get("value")
            if isinstance(fallback_value, str) and fallback_value.strip():
                texts.append(fallback_value)
    return texts, resolved_field


def sample_texts(texts: list[str], sample_size: int) -> list[str]:
    if len(texts) <= sample_size:
        return texts
    rng = np.random.default_rng(7)
    indices = rng.choice(len(texts), size=sample_size, replace=False)
    return [texts[int(index)] for index in indices]


def embed_texts(texts: list[str]) -> np.ndarray:
    api_key, base_url = resolve_api_configuration()
    if not api_key:
        raise RuntimeError(
            "An API key is required for embedding-drift checks. Set OPENAI_API_KEY or API_KEY in the environment or .env file."
        )
    client_kwargs: dict[str, Any] = {"api_key": api_key}
    if base_url:
        client_kwargs["base_url"] = base_url
    client = OpenAI(**client_kwargs)
    vectors: list[list[float]] = []
    batch_size = 100
    for start in range(0, len(texts), batch_size):
        batch = texts[start : start + batch_size]
        response = client.embeddings.create(model=EMBEDDING_MODEL, input=batch)
        vectors.extend(item.embedding for item in response.data)
    return np.asarray(vectors, dtype=np.float64)


def centroid(vectors: np.ndarray) -> np.ndarray:
    return np.mean(vectors, axis=0)


def cosine_distance(left: np.ndarray, right: np.ndarray) -> float:
    similarity = float(np.dot(left, right) / ((np.linalg.norm(left) * np.linalg.norm(right)) + 1e-9))
    return 1.0 - similarity


def check_embedding_drift(texts: list[str], baseline_path: Path, threshold: float = 0.15, sample_size: int = 200) -> dict[str, Any]:
    sampled_texts = sample_texts(texts, sample_size)
    current_vectors = embed_texts(sampled_texts)
    current_centroid = centroid(current_vectors)
    if not baseline_path.exists():
        baseline_path.parent.mkdir(parents=True, exist_ok=True)
        np.savez(
            baseline_path,
            centroid=current_centroid,
            sample_size=len(sampled_texts),
            created_at=now_iso(),
            model=EMBEDDING_MODEL,
        )
        return {
            "status": "BASELINE_SET",
            "drift_score": 0.0,
            "threshold": threshold,
            "sample_size": len(sampled_texts),
        }
    baseline_centroid = np.load(baseline_path, allow_pickle=True)["centroid"]
    drift = cosine_distance(current_centroid, baseline_centroid)
    return {
        "status": "FAIL" if drift > threshold else "PASS",
        "drift_score": round(float(drift), 4),
        "threshold": threshold,
        "sample_size": len(sampled_texts),
    }


def run_embedding_drift(
    input_path: Path,
    baseline_path: Path,
    output_path: Path,
    *,
    threshold: float,
    sample_size: int,
) -> dict[str, Any]:
    records = load_jsonl_records(input_path)
    texts, resolved_field = extract_embedding_text_candidates(records)
    if not texts:
        payload = {
            "check": "embedding_drift",
            "status": "ERROR",
            "message": "No embedded text candidates were found in Week 3 extracted facts.",
            "monitored_field": "extracted_facts[*].text",
            "resolved_source_field": resolved_field,
            "sample_size": 0,
            "run_timestamp": now_iso(),
        }
        write_json(output_path, payload)
        return payload

    result = check_embedding_drift(texts, baseline_path, threshold=threshold, sample_size=sample_size)
    payload = {
        "check": "embedding_drift",
        "status": result["status"],
        "drift_score": result["drift_score"],
        "threshold": result["threshold"],
        "sample_size": result["sample_size"],
        "baseline": str(baseline_path),
        "embedding_model": EMBEDDING_MODEL,
        "monitored_field": "extracted_facts[*].text",
        "resolved_source_field": resolved_field,
        "run_timestamp": now_iso(),
        "message": (
            "Embedding drift baseline was created from the current sample."
            if result["status"] == "BASELINE_SET"
            else "Embedding drift exceeds the allowed cosine distance threshold."
            if result["status"] == "FAIL"
            else "Embedding drift is within the acceptable threshold."
        ),
    }
    write_json(output_path, payload)
    return payload


def prompt_version_for_record(record: dict[str, Any]) -> str:
    for key in ("prompt_version", "model_version", "rubric_version"):
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return "unknown"


def compute_violation_trend(rate: float, baseline_rate: float | None) -> str:
    if baseline_rate is None:
        return "unknown"
    if baseline_rate == 0:
        return "rising" if rate > 0 else "stable"
    return "rising" if rate > baseline_rate * 1.5 else "stable"


def run_verdict_violation_rate(
    input_path: Path,
    baseline_path: Path,
    output_path: Path,
    *,
    warn_threshold: float,
) -> dict[str, Any]:
    records = load_jsonl_records(input_path)
    grouped: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        grouped.setdefault(prompt_version_for_record(record), []).append(record)

    baseline_payload = load_json(baseline_path)
    baseline_versions = baseline_payload.get("versions", {}) if isinstance(baseline_payload.get("versions"), dict) else {}
    per_version: list[dict[str, Any]] = []
    baseline_changed = False
    existing_baseline_count = 0

    total_outputs = 0
    total_violations = 0
    for version, version_records in sorted(grouped.items()):
        total = len(version_records)
        violations = sum(
            1
            for record in version_records
            if str(record.get("overall_verdict", "")).upper() not in EXPECTED_VERDICTS
        )
        rate = violations / max(total, 1)
        total_outputs += total
        total_violations += violations
        baseline_entry = baseline_versions.get(version)
        baseline_rate = None
        if isinstance(baseline_entry, dict) and baseline_entry.get("violation_rate") is not None:
            baseline_rate = float(baseline_entry["violation_rate"])
            existing_baseline_count += 1
        else:
            baseline_versions[version] = {
                "violation_rate": round(rate, 6),
                "total_outputs": total,
                "schema_violations": violations,
                "created_at": now_iso(),
            }
            baseline_changed = True

        trend = compute_violation_trend(rate, baseline_rate)
        status = "WARN" if rate > warn_threshold else "PASS"
        per_version.append(
            {
                "prompt_version": version,
                "total_outputs": total,
                "schema_violations": violations,
                "violation_rate": round(rate, 4),
                "baseline_rate": round(baseline_rate, 4) if baseline_rate is not None else None,
                "trend": trend,
                "status": status,
            }
        )

    if baseline_changed or not baseline_path.exists():
        baseline_path.parent.mkdir(parents=True, exist_ok=True)
        write_json(
            baseline_path,
            {
                "created_at": baseline_payload.get("created_at") or now_iso(),
                "updated_at": now_iso(),
                "versions": baseline_versions,
            },
        )

    overall_rate = total_violations / max(total_outputs, 1)
    status = "WARN" if any(item["status"] == "WARN" for item in per_version) else "PASS"
    if existing_baseline_count == 0:
        status = "BASELINE_SET"
    trend = "rising" if any(item["trend"] == "rising" for item in per_version) else "stable" if per_version else "unknown"
    payload = {
        "check": "llm_output_schema_violation_rate",
        "status": status,
        "total_outputs": total_outputs,
        "schema_violations": total_violations,
        "violation_rate": round(overall_rate, 4),
        "warn_threshold": warn_threshold,
        "trend": trend,
        "per_prompt_version": per_version,
        "baseline": str(baseline_path),
        "run_timestamp": now_iso(),
        "message": (
            "Output schema violation baselines were created for the observed prompt versions."
            if status == "BASELINE_SET"
            else "LLM output schema violation rate is rising or above the warning threshold."
            if status == "WARN"
            else "LLM output schema violation rate is stable and within the accepted range."
        ),
    }
    write_json(output_path, payload)
    return payload


def aggregate_status(statuses: list[str]) -> str:
    normalized = [status.upper() for status in statuses if isinstance(status, str)]
    if any(status in {"FAIL", "ERROR"} for status in normalized):
        return "FAIL"
    if any(status == "WARN" for status in normalized):
        return "WARN"
    if any(status == "BASELINE_SET" for status in normalized):
        return "BASELINE_SET"
    return "PASS"


def build_ai_risk_narrative(
    embedding_payload: dict[str, Any],
    prompt_payload: dict[str, Any],
    verdict_payload: dict[str, Any],
    aggregate_status_value: str,
) -> str:
    reliable = aggregate_status_value in {"PASS", "BASELINE_SET"} and prompt_payload.get("status") == "PASS"
    drift_phrase = (
        "within threshold"
        if embedding_payload.get("status") in {"PASS", "BASELINE_SET"}
        else "outside threshold"
    )
    trend = verdict_payload.get("trend", "unknown")
    reliability_phrase = "currently consuming reliable data" if reliable else "not currently consuming fully reliable data"
    return (
        f"AI systems are {reliability_phrase}; embedding drift is {drift_phrase} "
        f"and the LLM output schema violation rate is {trend}."
    )


def build_ai_extensions_aggregate() -> dict[str, Any]:
    embedding_payload = load_json(DEFAULT_EMBEDDING_OUTPUT)
    prompt_payload = load_json(DEFAULT_PROMPT_OUTPUT)
    verdict_payload = load_json(DEFAULT_VERDICT_OUTPUT)
    statuses = [
        str(embedding_payload.get("status", "")),
        str(prompt_payload.get("status", "")),
        str(verdict_payload.get("status", "")),
    ]
    status = aggregate_status(statuses)
    aggregate = {
        "status": status,
        "generated_at": now_iso(),
        "embedding_drift_score": embedding_payload.get("drift_score"),
        "embedding_drift_within_threshold": embedding_payload.get("status") in {"PASS", "BASELINE_SET"},
        "prompt_input_validation_status": prompt_payload.get("status"),
        "output_schema_violation_rate": verdict_payload.get("violation_rate"),
        "output_schema_violation_trend": verdict_payload.get("trend", "unknown"),
        "ai_system_reliable": status in {"PASS", "BASELINE_SET"} and prompt_payload.get("status") == "PASS",
        "risk_assessment": build_ai_risk_narrative(embedding_payload, prompt_payload, verdict_payload, status),
        "embedding_drift": embedding_payload,
        "prompt_input_validation": prompt_payload,
        "llm_output_schema_violation_rate": verdict_payload,
    }
    write_json(AGGREGATE_AI_REPORT, aggregate)
    return aggregate


def run_all(args: argparse.Namespace) -> dict[str, Any]:
    output_dir = Path(args.output_dir)
    embedding_payload = run_embedding_drift(
        Path(args.week3_input),
        Path(args.embedding_baseline),
        output_dir / DEFAULT_EMBEDDING_OUTPUT.name,
        threshold=args.threshold,
        sample_size=args.sample_size,
    )
    prompt_payload = run_prompt_validation(
        Path(args.week3_input),
        Path(args.quarantine_dir),
        output_dir / DEFAULT_PROMPT_OUTPUT.name,
    )
    verdict_payload = run_verdict_violation_rate(
        Path(args.week2_input),
        Path(args.verdict_baseline),
        output_dir / DEFAULT_VERDICT_OUTPUT.name,
        warn_threshold=args.warn_threshold,
    )
    aggregate = build_ai_extensions_aggregate()
    return {
        "status": aggregate.get("status"),
        "outputs": {
            "embedding_drift": embedding_payload,
            "prompt_input_validation": prompt_payload,
            "llm_output_schema_violation_rate": verdict_payload,
            "aggregate": aggregate,
        },
    }


def main() -> None:
    args = parse_args()
    try:
        if args.command == "embedding-drift":
            payload = run_embedding_drift(
                Path(args.input),
                Path(args.baseline),
                Path(args.output),
                threshold=args.threshold,
                sample_size=args.sample_size,
            )
            build_ai_extensions_aggregate()
        elif args.command == "prompt-validation":
            payload = run_prompt_validation(Path(args.input), Path(args.quarantine_dir), Path(args.output))
            build_ai_extensions_aggregate()
        elif args.command == "verdict-violation-rate":
            payload = run_verdict_violation_rate(
                Path(args.input),
                Path(args.baseline),
                Path(args.output),
                warn_threshold=args.warn_threshold,
            )
            build_ai_extensions_aggregate()
        elif args.command == "run-all":
            payload = run_all(args)
        else:
            raise ValueError(f"Unsupported command: {args.command}")

        output_target = AGGREGATE_AI_REPORT if args.command == "run-all" else Path(args.output)
        print(json.dumps({"output": str(output_target), "status": payload["status"]}, indent=2))
    except Exception as exc:
        output_path = Path(getattr(args, "output", AGGREGATE_AI_REPORT))
        payload = {
            "check": args.command,
            "status": "ERROR",
            "message": str(exc),
            "run_timestamp": now_iso(),
        }
        write_json(output_path, payload)
        print(json.dumps({"output": str(output_path), "status": "ERROR", "message": str(exc)}, indent=2))
        raise SystemExit(1)


if __name__ == "__main__":
    main()
