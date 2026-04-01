from __future__ import annotations

import json
import uuid
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
WEEK3_PATH = PROJECT_ROOT / "outputs" / "week3" / "extractions.jsonl"
WEEK4_PATH = PROJECT_ROOT / "outputs" / "week4" / "lineage_snapshots.jsonl"
WEEK5_PATH = PROJECT_ROOT / "outputs" / "week5" / "events.jsonl"


def write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def isoformat_utc(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def build_week3_records(record_count: int = 60) -> list[dict]:
    records: list[dict] = []
    base_time = datetime(2026, 3, 1, 9, 0, tzinfo=UTC)
    fact_types = ["invoice_total", "invoice_status", "vendor_name"]
    vendor_names = ["Acme Corp", "Globex", "Initech", "Umbrella", "Stark Industries"]

    for index in range(record_count):
        doc_id = str(uuid.uuid4())
        fact_confidence = round(0.72 + ((index % 20) * 0.01), 2)
        total_value = round(120.0 + (index * 7.5), 2)
        status_value = "approved" if index % 3 else "pending"
        vendor_value = vendor_names[index % len(vendor_names)]
        record = {
            "doc_id": doc_id,
            "document_name": f"invoice_{index + 1:03d}.pdf",
            "document_type": "invoice",
            "source_system": "week3_extractions",
            "batch_id": f"batch-{(index // 10) + 1:02d}",
            "processed_at": isoformat_utc(base_time + timedelta(minutes=index * 3)),
            "language": "en",
            "extraction_version": "v1.0",
            "extracted_facts": [
                {
                    "fact_type": fact_types[0],
                    "value": total_value,
                    "confidence": fact_confidence,
                },
                {
                    "fact_type": fact_types[1],
                    "value": status_value,
                    "confidence": round(min(fact_confidence + 0.05, 0.99), 2),
                },
                {
                    "fact_type": fact_types[2],
                    "value": vendor_value,
                    "confidence": round(min(fact_confidence + 0.03, 0.99), 2),
                },
            ],
            "quality_status": "ready" if index % 5 else "review",
        }
        records.append(record)
    return records


def build_week5_records(aggregate_count: int = 10, events_per_aggregate: int = 6) -> list[dict]:
    records: list[dict] = []
    base_time = datetime(2026, 3, 5, 10, 0, tzinfo=UTC)
    event_types = [
        "order_created",
        "order_validated",
        "payment_authorized",
        "shipment_requested",
        "shipment_confirmed",
        "order_closed",
    ]

    for aggregate_index in range(aggregate_count):
        aggregate_id = str(uuid.uuid4())
        account_id = f"acct-{aggregate_index + 1:03d}"
        for event_index in range(events_per_aggregate):
            occurred_at = base_time + timedelta(hours=aggregate_index, minutes=event_index * 5)
            recorded_at = occurred_at + timedelta(minutes=1)
            amount = round(45 + (aggregate_index * 13) + (event_index * 4.25), 2)
            record = {
                "event_id": str(uuid.uuid4()),
                "aggregate_id": aggregate_id,
                "aggregate_type": "order",
                "sequence_number": event_index + 1,
                "event_type": event_types[event_index % len(event_types)],
                "occurred_at": isoformat_utc(occurred_at),
                "recorded_at": isoformat_utc(recorded_at),
                "source_system": "week5_events",
                "account_id": account_id,
                "status": "completed" if event_index == events_per_aggregate - 1 else "in_progress",
                "amount": amount,
            }
            records.append(record)
    return records


def build_lineage_snapshot() -> list[dict]:
    snapshot = {
        "snapshot_id": str(uuid.uuid4()),
        "captured_at": isoformat_utc(datetime(2026, 3, 10, 12, 0, tzinfo=UTC)),
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
    lineage_snapshot = build_lineage_snapshot()

    write_jsonl(WEEK3_PATH, week3_records)
    write_jsonl(WEEK5_PATH, week5_records)
    write_jsonl(WEEK4_PATH, lineage_snapshot)

    print(
        json.dumps(
            {
                "week3_path": str(WEEK3_PATH),
                "week3_records": len(week3_records),
                "week4_path": str(WEEK4_PATH),
                "week4_snapshots": len(lineage_snapshot),
                "week5_path": str(WEEK5_PATH),
                "week5_records": len(week5_records),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
