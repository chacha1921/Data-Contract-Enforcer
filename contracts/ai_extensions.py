from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from jsonschema import Draft7Validator
from openai import OpenAI


PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = PROJECT_ROOT / ".env"
DEFAULT_WEEK3_PATH = PROJECT_ROOT / "outputs" / "week3" / "extractions.jsonl"
DEFAULT_WEEK2_PATH = PROJECT_ROOT / "outputs" / "week2" / "verdicts.jsonl"
DEFAULT_QUARANTINE_DIR = PROJECT_ROOT / "outputs" / "quarantine"
DEFAULT_EMBEDDING_BASELINE = PROJECT_ROOT / "schema_snapshots" / "embedding_baselines.npz"
DEFAULT_LLM_RATE_BASELINE = PROJECT_ROOT / "schema_snapshots" / "llm_output_violation_baseline.json"
EXPECTED_VERDICTS = {"PASS", "FAIL", "WARN"}
EMBEDDING_MODEL = "text-embedding-3-small"


PROMPT_INPUT_SCHEMA: dict[str, Any] = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "type": "object",
    "required": [
        "doc_id",
        "source_doc_id",
        "block_id",
        "page_number",
        "block_type",
        "language",
        "processed_at",
        "text_preview",
        "extracted_facts",
    ],
    "properties": {
        "doc_id": {"type": "string"},
        "source_doc_id": {"type": "string"},
        "block_id": {"type": "string"},
        "page_number": {"type": "integer"},
        "block_type": {"type": "string"},
        "language": {"type": "string"},
        "processed_at": {"type": "string"},
        "text_preview": {"type": "string"},
        "extracted_facts": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["fact_type", "value", "confidence"],
                "properties": {
                    "fact_type": {"type": "string"},
                    "value": {"type": "string"},
                    "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                },
            },
        },
    },
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="AI-specific Data Contract Enforcer checks.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    embedding = subparsers.add_parser("embedding-drift", help="Check centroid drift for Week 3 text embeddings.")
    embedding.add_argument("--input", default=str(DEFAULT_WEEK3_PATH), help="Path to Week 3 JSONL input.")
    embedding.add_argument("--baseline", default=str(DEFAULT_EMBEDDING_BASELINE), help="Path to embedding baseline NPZ file.")
    embedding.add_argument("--output", required=True, help="Path to the JSON result output.")

    prompt = subparsers.add_parser("prompt-validation", help="Validate Week 3 prompt inputs against Draft-07 schema.")
    prompt.add_argument("--input", default=str(DEFAULT_WEEK3_PATH), help="Path to Week 3 JSONL input.")
    prompt.add_argument("--output", required=True, help="Path to the JSON result output.")
    prompt.add_argument("--quarantine-dir", default=str(DEFAULT_QUARANTINE_DIR), help="Directory to write quarantined records.")

    verdict = subparsers.add_parser("verdict-violation-rate", help="Check Week 2 verdict enum violation rate.")
    verdict.add_argument("--input", default=str(DEFAULT_WEEK2_PATH), help="Path to Week 2 verdicts JSONL input.")
    verdict.add_argument("--baseline", default=str(DEFAULT_LLM_RATE_BASELINE), help="Path to verdict-rate baseline JSON file.")
    verdict.add_argument("--output", required=True, help="Path to the JSON result output.")

    return parser.parse_args()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def load_jsonl_records(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        raise FileNotFoundError(f"Input file not found: {path}")
    records: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            raw = line.strip()
            if not raw:
                continue
            payload = json.loads(raw)
            if isinstance(payload, dict):
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

    base_url = (
        os.getenv("OPENAI_BASE_URL")
        or dotenv_values.get("OPENAI_BASE_URL")
    )

    if api_key and api_key.startswith("sk-or-") and not base_url:
        base_url = "https://openrouter.ai/api/v1"

    return api_key, base_url


def sample_week3_texts(records: list[dict[str, Any]], limit: int = 200) -> list[str]:
    texts: list[str] = []
    seen: set[str] = set()
    for record in records:
        extracted_facts = record.get("extracted_facts", [])
        if isinstance(extracted_facts, list):
            for item in extracted_facts:
                if not isinstance(item, dict):
                    continue
                value = item.get("value")
                if isinstance(value, str) and value.strip() and value not in seen:
                    seen.add(value)
                    texts.append(value)
                    if len(texts) >= limit:
                        return texts
        text_preview = record.get("text_preview")
        if isinstance(text_preview, str) and text_preview.strip() and text_preview not in seen:
            seen.add(text_preview)
            texts.append(text_preview)
            if len(texts) >= limit:
                return texts
    return texts[:limit]


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
    return vectors.mean(axis=0)


def cosine_distance(left: np.ndarray, right: np.ndarray) -> float:
    left_norm = np.linalg.norm(left)
    right_norm = np.linalg.norm(right)
    if left_norm == 0 or right_norm == 0:
        return 1.0
    cosine_similarity = float(np.dot(left, right) / (left_norm * right_norm))
    return 1.0 - cosine_similarity


def run_embedding_drift(input_path: Path, baseline_path: Path, output_path: Path) -> dict[str, Any]:
    records = load_jsonl_records(input_path)
    texts = sample_week3_texts(records)
    if not texts:
        payload = {
            "check": "embedding_drift",
            "status": "ERROR",
            "message": "No text samples found for embedding drift analysis.",
            "sample_size": 0,
            "run_timestamp": now_iso(),
        }
        write_json(output_path, payload)
        return payload

    vectors = embed_texts(texts)
    current_centroid = centroid(vectors)

    if not baseline_path.exists():
        baseline_path.parent.mkdir(parents=True, exist_ok=True)
        np.savez(baseline_path, centroid=current_centroid, sample_size=len(texts), created_at=now_iso())
        payload = {
            "check": "embedding_drift",
            "status": "BASELINE_CREATED",
            "message": "Embedding baseline did not exist and was created from the current sample.",
            "sample_size": len(texts),
            "baseline": str(baseline_path),
            "run_timestamp": now_iso(),
        }
        write_json(output_path, payload)
        return payload

    baseline = np.load(baseline_path, allow_pickle=True)
    baseline_centroid = baseline["centroid"]
    drift = cosine_distance(current_centroid, baseline_centroid)
    status = "FAIL" if drift > 0.15 else "PASS"
    payload = {
        "check": "embedding_drift",
        "status": status,
        "threshold": 0.15,
        "cosine_distance": round(float(drift), 6),
        "sample_size": len(texts),
        "baseline": str(baseline_path),
        "run_timestamp": now_iso(),
        "message": (
            "Embedding centroid drift exceeds the allowed threshold."
            if status == "FAIL"
            else "Embedding centroid drift is within the allowed threshold."
        ),
    }
    write_json(output_path, payload)
    return payload


def quarantine_path(quarantine_dir: Path) -> Path:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    return quarantine_dir / f"prompt_input_quarantine_{timestamp}.jsonl"


def run_prompt_validation(input_path: Path, quarantine_dir: Path, output_path: Path) -> dict[str, Any]:
    records = load_jsonl_records(input_path)
    validator = Draft7Validator(PROMPT_INPUT_SCHEMA)
    invalid_records: list[dict[str, Any]] = []

    for index, record in enumerate(records):
        errors = sorted(validator.iter_errors(record), key=lambda error: error.path)
        if not errors:
            continue
        invalid_records.append(
            {
                "record_index": index,
                "errors": [error.message for error in errors],
                "record": record,
            }
        )

    quarantine_file = quarantine_path(quarantine_dir)
    quarantine_file.parent.mkdir(parents=True, exist_ok=True)
    with quarantine_file.open("w", encoding="utf-8") as handle:
        for record in invalid_records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    payload = {
        "check": "prompt_input_validation",
        "status": "PASS" if not invalid_records else "FAIL",
        "total_records": len(records),
        "invalid_records": len(invalid_records),
        "quarantine_file": str(quarantine_file),
        "run_timestamp": now_iso(),
        "message": (
            "All prompt input records conform to the Draft-07 schema."
            if not invalid_records
            else "Non-conforming prompt input records were quarantined."
        ),
    }
    write_json(output_path, payload)
    return payload


def load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload if isinstance(payload, dict) else {}


def run_verdict_violation_rate(input_path: Path, baseline_path: Path, output_path: Path) -> dict[str, Any]:
    records = load_jsonl_records(input_path)
    total = len(records)
    invalid = sum(
        1
        for record in records
        if str(record.get("overall_verdict", "")).upper() not in EXPECTED_VERDICTS
    )
    rate = 0.0 if total == 0 else (invalid / total) * 100.0

    baseline_payload = load_json(baseline_path)
    baseline_rate = baseline_payload.get("violation_rate")
    if baseline_rate is None:
        baseline = {
            "violation_rate": round(rate, 6),
            "record_count": total,
            "created_at": now_iso(),
        }
        baseline_path.parent.mkdir(parents=True, exist_ok=True)
        baseline_path.write_text(json.dumps(baseline, indent=2), encoding="utf-8")
        payload = {
            "check": "llm_output_violation_rate",
            "status": "BASELINE_CREATED",
            "violation_rate": round(rate, 6),
            "record_count": total,
            "baseline": str(baseline_path),
            "run_timestamp": now_iso(),
            "message": "Violation-rate baseline did not exist and was created from the current data.",
        }
        write_json(output_path, payload)
        return payload

    baseline_rate_value = float(baseline_rate)
    threshold_triggered = rate > 2.0 or (baseline_rate_value > 0 and rate > baseline_rate_value * 1.5)
    status = "WARN" if threshold_triggered else "PASS"
    payload = {
        "check": "llm_output_violation_rate",
        "status": status,
        "violation_rate": round(rate, 6),
        "baseline_rate": round(baseline_rate_value, 6),
        "record_count": total,
        "invalid_records": invalid,
        "run_timestamp": now_iso(),
        "message": (
            "Verdict violation rate exceeded the warning threshold."
            if status == "WARN"
            else "Verdict violation rate is within the acceptable range."
        ),
    }
    write_json(output_path, payload)
    return payload


def main() -> None:
    args = parse_args()
    output_path = Path(args.output)
    try:
        if args.command == "embedding-drift":
            payload = run_embedding_drift(Path(args.input), Path(args.baseline), output_path)
        elif args.command == "prompt-validation":
            payload = run_prompt_validation(Path(args.input), Path(args.quarantine_dir), output_path)
        elif args.command == "verdict-violation-rate":
            payload = run_verdict_violation_rate(Path(args.input), Path(args.baseline), output_path)
        else:
            raise ValueError(f"Unsupported command: {args.command}")

        print(json.dumps({"output": str(output_path), "status": payload["status"]}, indent=2))
    except Exception as exc:
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
