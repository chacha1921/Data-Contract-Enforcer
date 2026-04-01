from __future__ import annotations

import json
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
WEEK3_PATH = PROJECT_ROOT / "outputs" / "week3" / "extractions.jsonl"
WEEK5_PATH = PROJECT_ROOT / "outputs" / "week5" / "events.jsonl"


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        raise FileNotFoundError(f"Expected source file not found: {path}")

    records: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            raw_line = line.strip()
            if not raw_line:
                continue
            payload = json.loads(raw_line)
            if not isinstance(payload, dict):
                raise ValueError(
                    f"Expected JSON object at {path}:{line_number}, got {type(payload).__name__}."
                )
            records.append(payload)
    return records


def write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def parse_iso8601(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
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
    return parsed


def format_iso8601(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def ensure_uuid4(value: Any) -> str:
    if isinstance(value, str):
        try:
            parsed = uuid.UUID(value)
            if parsed.version == 4:
                return str(parsed)
        except ValueError:
            pass
    return str(uuid.uuid4())


def normalize_confidence(value: Any) -> float:
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        confidence = 0.0

    if confidence > 1.0:
        confidence = confidence / 100.0

    if confidence < 0.0:
        return 0.0
    if confidence > 1.0:
        return 1.0
    return confidence


def align_week3_record(record: dict[str, Any]) -> dict[str, Any]:
    aligned = dict(record)
    aligned["doc_id"] = ensure_uuid4(aligned.get("doc_id"))

    extracted_facts = aligned.get("extracted_facts")
    if isinstance(extracted_facts, list):
        normalized_facts: list[Any] = []
        for fact in extracted_facts:
            if isinstance(fact, dict):
                normalized_fact = dict(fact)
                normalized_fact["confidence"] = normalize_confidence(
                    normalized_fact.get("confidence")
                )
                normalized_facts.append(normalized_fact)
            else:
                normalized_facts.append(fact)
        aligned["extracted_facts"] = normalized_facts

    return aligned


def align_week5_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[tuple[int, dict[str, Any]]]] = defaultdict(list)
    passthrough_records: list[tuple[int, dict[str, Any]]] = []

    for index, record in enumerate(records):
        aggregate_id = record.get("aggregate_id")
        if aggregate_id is None:
            passthrough_records.append((index, dict(record)))
            continue
        grouped[str(aggregate_id)].append((index, dict(record)))

    aligned_by_index: dict[int, dict[str, Any]] = {}

    for group_records in grouped.values():
        sorted_group = sorted(
            group_records,
            key=lambda item: (
                parse_iso8601(item[1].get("occurred_at")) or datetime.min.replace(tzinfo=timezone.utc),
                parse_iso8601(item[1].get("recorded_at")) or datetime.min.replace(tzinfo=timezone.utc),
                item[0],
            ),
        )

        for sequence_number, (original_index, record) in enumerate(sorted_group, start=1):
            occurred_at = parse_iso8601(record.get("occurred_at"))
            recorded_at = parse_iso8601(record.get("recorded_at"))
            if occurred_at and (recorded_at is None or recorded_at < occurred_at):
                record["recorded_at"] = format_iso8601(occurred_at)
            elif recorded_at:
                record["recorded_at"] = format_iso8601(recorded_at)
            if occurred_at:
                record["occurred_at"] = format_iso8601(occurred_at)

            record["sequence_number"] = sequence_number
            aligned_by_index[original_index] = record

    for original_index, record in passthrough_records:
        occurred_at = parse_iso8601(record.get("occurred_at"))
        recorded_at = parse_iso8601(record.get("recorded_at"))
        if occurred_at and (recorded_at is None or recorded_at < occurred_at):
            record["recorded_at"] = format_iso8601(occurred_at)
        elif recorded_at:
            record["recorded_at"] = format_iso8601(recorded_at)
        if occurred_at:
            record["occurred_at"] = format_iso8601(occurred_at)
        if not isinstance(record.get("sequence_number"), int) or record["sequence_number"] < 1:
            record["sequence_number"] = 1
        aligned_by_index[original_index] = record

    return [aligned_by_index[index] for index in sorted(aligned_by_index)]


def main() -> None:
    week3_records = load_jsonl(WEEK3_PATH)
    week5_records = load_jsonl(WEEK5_PATH)

    aligned_week3 = [align_week3_record(record) for record in week3_records]
    aligned_week5 = align_week5_records(week5_records)

    write_jsonl(WEEK3_PATH, aligned_week3)
    write_jsonl(WEEK5_PATH, aligned_week5)

    print(
        json.dumps(
            {
                "week3_path": str(WEEK3_PATH),
                "week3_records": len(aligned_week3),
                "week5_path": str(WEEK5_PATH),
                "week5_records": len(aligned_week5),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
