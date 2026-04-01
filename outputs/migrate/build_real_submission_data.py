from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
WEEK3_LEDGER_PATH = PROJECT_ROOT / "repos" / "week3" / ".refinery" / "extraction_ledger.jsonl"
WEEK3_EXTRACTED_DIR = PROJECT_ROOT / "repos" / "week3" / ".refinery" / "extracted"
WEEK3_PROFILES_DIR = PROJECT_ROOT / "repos" / "week3" / ".refinery" / "profiles"
WEEK5_SOURCE_PATH = PROJECT_ROOT / "repos" / "week5" / "data" / "seed_events.jsonl"
WEEK3_OUTPUT_PATH = PROJECT_ROOT / "outputs" / "week3" / "extractions.jsonl"
WEEK5_OUTPUT_PATH = PROJECT_ROOT / "outputs" / "week5" / "events.jsonl"
WEEK4_OUTPUT_PATH = PROJECT_ROOT / "outputs" / "week4" / "lineage_snapshots.jsonl"


TIMESTAMP_KEYS = [
    "occurred_at",
    "submitted_at",
    "uploaded_at",
    "created_at",
    "added_at",
    "approved_at",
    "rejected_at",
    "deadline",
]


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            payload = json.loads(stripped)
            if not isinstance(payload, dict):
                raise ValueError(f"Expected object at {path}:{line_number}")
            records.append(payload)
    return records


def read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Expected object in {path}")
    return payload


def write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def isoformat_utc(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def latest_ledger_by_doc() -> dict[str, dict[str, Any]]:
    ledger_rows = read_jsonl(WEEK3_LEDGER_PATH)
    latest: dict[str, dict[str, Any]] = {}
    for row in ledger_rows:
        doc_id = str(row.get("doc_id", "")).strip()
        if not doc_id:
            continue
        candidate_time = parse_dt(row.get("timestamp")) or datetime.min.replace(tzinfo=timezone.utc)
        current = latest.get(doc_id)
        current_time = parse_dt(current.get("timestamp")) if current else None
        if current is None or candidate_time >= (current_time or datetime.min.replace(tzinfo=timezone.utc)):
            latest[doc_id] = row
    return latest


def build_week3_records() -> list[dict[str, Any]]:
    latest_ledger = latest_ledger_by_doc()
    records: list[dict[str, Any]] = []

    for extracted_path in sorted(WEEK3_EXTRACTED_DIR.glob("*.json")):
        source_doc_id = extracted_path.stem
        extracted_payload = read_json(extracted_path)
        profile_path = WEEK3_PROFILES_DIR / extracted_path.name
        profile_payload = read_json(profile_path) if profile_path.exists() else {}
        ledger_entry = latest_ledger.get(source_doc_id, {})
        confidence = float(ledger_entry.get("confidence_score", profile_payload.get("language_confidence", 0.0)) or 0.0)
        status = str(ledger_entry.get("status", "success"))
        strategy_used = str(ledger_entry.get("strategy_used", profile_payload.get("estimated_extraction_cost", "unknown")))
        processed_at = ledger_entry.get("timestamp")

        text_blocks = extracted_payload.get("text_blocks", [])
        for block_index, block in enumerate(text_blocks):
            if not isinstance(block, dict):
                continue
            text = str(block.get("text", "")).strip()
            if not text:
                continue
            page_number = block.get("page_number")
            record = {
                "doc_id": source_doc_id,
                "source_doc_id": source_doc_id,
                "block_id": block.get("id", f"block_{block_index + 1}"),
                "page_number": page_number,
                "block_type": block.get("block_type", "text"),
                "language": profile_payload.get("language"),
                "language_confidence": profile_payload.get("language_confidence"),
                "layout_complexity": profile_payload.get("layout_complexity"),
                "domain_hint": profile_payload.get("domain_hint"),
                "origin_type": profile_payload.get("origin_type"),
                "strategy_used": strategy_used,
                "status": status,
                "processed_at": processed_at,
                "processing_time_seconds": ledger_entry.get("processing_time_seconds"),
                "text_preview": text[:280],
                "text_length": len(text),
                "extracted_facts": [
                    {
                        "fact_type": "text_block",
                        "value": text[:280],
                        "confidence": confidence,
                    }
                ],
            }
            records.append(record)
    return records


def extract_occurrence_time(payload: dict[str, Any], recorded_at: str | None) -> str | None:
    for key in TIMESTAMP_KEYS:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return recorded_at


def build_week5_records() -> list[dict[str, Any]]:
    source_rows = read_jsonl(WEEK5_SOURCE_PATH)
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in source_rows:
        stream_id = str(row.get("stream_id", "")).strip()
        if not stream_id:
            continue
        grouped[stream_id].append(row)

    output_rows: list[dict[str, Any]] = []
    for stream_id, rows in grouped.items():
        ordered_rows = sorted(
            rows,
            key=lambda row: (
                parse_dt(extract_occurrence_time(row.get("payload", {}), row.get("recorded_at")))
                or datetime.min.replace(tzinfo=timezone.utc),
                parse_dt(row.get("recorded_at")) or datetime.min.replace(tzinfo=timezone.utc),
            ),
        )
        for sequence_number, row in enumerate(ordered_rows, start=1):
            payload = row.get("payload", {})
            if not isinstance(payload, dict):
                payload = {}
            recorded_at = row.get("recorded_at")
            occurred_at = extract_occurrence_time(payload, recorded_at)
            amount = payload.get("requested_amount_usd")
            try:
                amount = float(amount) if amount is not None else None
            except (TypeError, ValueError):
                amount = None
            output_rows.append(
                {
                    "event_id": f"{stream_id}-{sequence_number:04d}",
                    "aggregate_id": stream_id,
                    "aggregate_type": stream_id.split("-", 1)[0],
                    "sequence_number": sequence_number,
                    "event_type": row.get("event_type"),
                    "event_version": row.get("event_version"),
                    "occurred_at": occurred_at,
                    "recorded_at": recorded_at,
                    "source_system": "week5_events",
                    "application_id": payload.get("application_id"),
                    "package_id": payload.get("package_id"),
                    "applicant_id": payload.get("applicant_id"),
                    "document_id": payload.get("document_id"),
                    "document_type": payload.get("document_type"),
                    "document_format": payload.get("document_format"),
                    "loan_purpose": payload.get("loan_purpose"),
                    "submission_channel": payload.get("submission_channel"),
                    "requested_amount_usd": amount,
                }
            )
    return output_rows


def build_lineage_snapshot() -> list[dict[str, Any]]:
    snapshot = {
        "captured_at": isoformat_utc(datetime.now(timezone.utc)),
        "edges": [
            {"source": "week3_extractions", "target": "analytics_invoice_quality"},
            {"source": "week3_extractions", "target": "dbt_week3_extractions_model"},
            {"source": "week5_events", "target": "operations_event_dashboard"},
            {"source": "week5_events", "target": "dbt_week5_events_model"},
        ],
    }
    return [snapshot]


def main() -> None:
    week3_records = build_week3_records()
    week5_records = build_week5_records()
    lineage_records = build_lineage_snapshot()

    write_jsonl(WEEK3_OUTPUT_PATH, week3_records)
    write_jsonl(WEEK5_OUTPUT_PATH, week5_records)
    write_jsonl(WEEK4_OUTPUT_PATH, lineage_records)

    print(
        json.dumps(
            {
                "week3_records": len(week3_records),
                "week3_output": str(WEEK3_OUTPUT_PATH),
                "week5_records": len(week5_records),
                "week5_output": str(WEEK5_OUTPUT_PATH),
                "week4_snapshots": len(lineage_records),
                "week4_output": str(WEEK4_OUTPUT_PATH),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
