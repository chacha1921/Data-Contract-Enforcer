from __future__ import annotations

import argparse
import json
import math
import uuid
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml
from git import Repo


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_LINEAGE_PATH = PROJECT_ROOT / "outputs" / "week4" / "lineage_snapshots.jsonl"
DEFAULT_REGISTRY_PATH = PROJECT_ROOT / "contract_registry" / "subscriptions.yaml"
DEFAULT_OUTPUT_PATH = PROJECT_ROOT / "violation_log" / "violations.jsonl"
GENERATED_CONTRACTS_DIR = PROJECT_ROOT / "generated_contracts"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Attribute contract violations using registry, lineage, and git history."
    )
    parser.add_argument("--violation", required=True, help="Path to the violation JSON report.")
    parser.add_argument(
        "--lineage",
        required=False,
        default=str(DEFAULT_LINEAGE_PATH),
        help="Path to the Week 4 lineage JSONL file.",
    )
    parser.add_argument(
        "--registry",
        required=False,
        default=str(DEFAULT_REGISTRY_PATH),
        help="Path to the registry subscriptions YAML file.",
    )
    parser.add_argument(
        "--output",
        required=False,
        default=str(DEFAULT_OUTPUT_PATH),
        help="Path to the JSONL file where attributed violations will be appended.",
    )
    return parser.parse_args()


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"JSON file not found: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Expected JSON object in {path}")
    return payload


def load_registry(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        raise FileNotFoundError(f"Registry file not found: {path}")
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    subscriptions = payload.get("subscriptions", []) if isinstance(payload, dict) else []
    return [entry for entry in subscriptions if isinstance(entry, dict)]


def load_lineage_edges(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        raise FileNotFoundError(f"Lineage file not found: {path}")
    lines = [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not lines:
        return []
    payload = json.loads(lines[-1])
    edges = payload.get("edges", []) if isinstance(payload, dict) else []
    return [edge for edge in edges if isinstance(edge, dict)]


def relative_to_project(path: Path) -> str:
    try:
        return str(path.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path)


def find_contract_path(contract_id: str) -> Path | None:
    candidate = GENERATED_CONTRACTS_DIR / f"{contract_id}.yaml"
    if candidate.exists():
        return candidate
    return None


def resolve_producer_file(contract_id: str) -> str | None:
    contract_path = find_contract_path(contract_id)
    if contract_path is None:
        return None
    payload = yaml.safe_load(contract_path.read_text(encoding="utf-8")) or {}
    lineage = payload.get("lineage", {}) if isinstance(payload, dict) else {}
    source = lineage.get("source") if isinstance(lineage, dict) else None
    if isinstance(source, str) and source.strip():
        return source.strip()
    return None


def affected_registry_subscribers(
    subscriptions: list[dict[str, Any]], contract_id: str, column_name: str
) -> list[dict[str, Any]]:
    affected: list[dict[str, Any]] = []
    for subscription in subscriptions:
        if str(subscription.get("contract_id", "")).strip() != contract_id:
            continue

        consumed_fields = [str(item) for item in subscription.get("fields_consumed", []) if item]
        breaking_fields = {
            str(item.get("field")): str(item.get("reason", ""))
            for item in subscription.get("breaking_fields", [])
            if isinstance(item, dict) and item.get("field")
        }
        if column_name not in consumed_fields and column_name not in breaking_fields:
            continue

        affected.append(
            {
                "subscriber_id": subscription.get("subscriber_id"),
                "validation_mode": subscription.get("validation_mode"),
                "fields_consumed": consumed_fields,
                "breaking_reason": breaking_fields.get(column_name),
                "contact": subscription.get("contact"),
            }
        )
    return affected


def lineage_graph(edges: list[dict[str, Any]]) -> dict[str, list[str]]:
    graph: dict[str, list[str]] = defaultdict(list)
    for edge in edges:
        source = edge.get("source") or edge.get("from")
        target = edge.get("target") or edge.get("to")
        if isinstance(source, str) and isinstance(target, str):
            graph[source].append(target)
    return graph


def lineage_enrichment(edges: list[dict[str, Any]], contract_id: str) -> dict[str, Any]:
    graph = lineage_graph(edges)
    visited: set[str] = {contract_id}
    queue: deque[tuple[str, int]] = deque([(contract_id, 0)])
    downstream_nodes: list[dict[str, Any]] = []
    max_depth = 0

    while queue:
        node, depth = queue.popleft()
        for child in graph.get(node, []):
            next_depth = depth + 1
            downstream_nodes.append({"node_id": child, "depth": next_depth})
            max_depth = max(max_depth, next_depth)
            if child not in visited:
                visited.add(child)
                queue.append((child, next_depth))

    return {
        "transitive_depth": max_depth,
        "downstream_nodes": downstream_nodes,
    }


def parse_git_log_row(row: str, lineage_hops: int) -> dict[str, Any] | None:
    parts = row.split("|", 3)
    if len(parts) != 4:
        return None
    commit_hash, committed_epoch, author, message = parts
    try:
        committed_at = datetime.fromtimestamp(int(committed_epoch), tz=timezone.utc)
    except ValueError:
        return None

    days_since_commit = (now_utc() - committed_at).total_seconds() / 86400.0
    confidence_score = 1.0 - (days_since_commit * 0.1) - (lineage_hops * 0.2)
    confidence_score = max(0.0, min(1.0, confidence_score))
    return {
        "commit": commit_hash,
        "author": author,
        "message": message,
        "committed_at": committed_at.isoformat(),
        "days_since_commit": round(days_since_commit, 3),
        "lineage_hops": lineage_hops,
        "confidence_score": round(confidence_score, 4),
    }


def recent_commits_for_file(producer_file: str | None, lineage_hops: int) -> list[dict[str, Any]]:
    if not producer_file:
        return []

    producer_path = PROJECT_ROOT / producer_file
    if not producer_path.exists():
        return []

    repo = Repo(PROJECT_ROOT)
    try:
        raw_log = repo.git.log(
            "--follow",
            f"--since={(now_utc() - timedelta(days=14)).isoformat()}",
            "--format=%H|%ct|%an|%s",
            "--",
            relative_to_project(producer_path),
        )
    except Exception:
        return []

    entries: list[dict[str, Any]] = []
    for row in raw_log.splitlines():
        parsed = parse_git_log_row(row, lineage_hops)
        if parsed is not None:
            entries.append(parsed)
    return entries


def violation_results(report: dict[str, Any]) -> list[dict[str, Any]]:
    results = report.get("results", []) if isinstance(report, dict) else []
    actionable_statuses = {"FAIL", "ERROR"}
    actionable: list[dict[str, Any]] = []
    for result in results:
        if not isinstance(result, dict):
            continue
        if str(result.get("status", "")).upper() not in actionable_statuses:
            continue
        column_name = result.get("column_name")
        if not isinstance(column_name, str) or not column_name.strip():
            continue
        actionable.append(result)
    return actionable


def build_violation_record(
    report: dict[str, Any],
    result: dict[str, Any],
    subscriptions: list[dict[str, Any]],
    edges: list[dict[str, Any]],
) -> dict[str, Any]:
    contract_id = str(report.get("contract_id", "unknown_contract"))
    column_name = str(result.get("column_name", "unknown_column"))
    lineage_info = lineage_enrichment(edges, contract_id)
    blast_radius = affected_registry_subscribers(subscriptions, contract_id, column_name)
    producer_file = resolve_producer_file(contract_id)
    blame_chain = recent_commits_for_file(producer_file, lineage_info["transitive_depth"])

    return {
        "violation_id": str(uuid.uuid4()),
        "report_id": report.get("report_id"),
        "contract_id": contract_id,
        "snapshot_id": report.get("snapshot_id"),
        "run_timestamp": report.get("run_timestamp"),
        "column_name": column_name,
        "check_type": result.get("check_type"),
        "status": result.get("status"),
        "severity": result.get("severity"),
        "actual_value": result.get("actual_value"),
        "expected": result.get("expected"),
        "producer_file": producer_file,
        "registry_sourced_blast_radius": blast_radius,
        "lineage_enrichment": lineage_info,
        "blame_chain": blame_chain,
        "attributed_at": now_utc().isoformat(),
    }


def append_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def main() -> None:
    args = parse_args()
    try:
        report = load_json(Path(args.violation))
        subscriptions = load_registry(Path(args.registry))
        edges = load_lineage_edges(Path(args.lineage))

        records = [
            build_violation_record(report, result, subscriptions, edges)
            for result in violation_results(report)
        ]
        append_jsonl(Path(args.output), records)

        print(
            json.dumps(
                {
                    "output": str(Path(args.output)),
                    "records_appended": len(records),
                },
                indent=2,
            )
        )
    except Exception as exc:
        print(json.dumps({"status": "ERROR", "message": str(exc)}, indent=2))
        raise SystemExit(1)


if __name__ == "__main__":
    main()
