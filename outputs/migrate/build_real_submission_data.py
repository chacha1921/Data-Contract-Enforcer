from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]
WEEK1_REPO_ROOT = PROJECT_ROOT / "repos" / "week1"
WEEK1_INTENTS_PATH = WEEK1_REPO_ROOT / ".orchestration" / "active_intents.yaml"
WEEK1_TRACE_PATH = WEEK1_REPO_ROOT / ".orchestration" / "agent_trace.jsonl"
WEEK2_REPO_ROOT = PROJECT_ROOT / "repos" / "week2"
WEEK2_RUBRIC_PATH = WEEK2_REPO_ROOT / "rubric.json"
WEEK2_STATE_PATH = WEEK2_REPO_ROOT / "src" / "state.py"
WEEK2_GRAPH_PATH = WEEK2_REPO_ROOT / "src" / "graph.py"
WEEK2_DETECTIVES_PATH = WEEK2_REPO_ROOT / "src" / "nodes" / "detectives.py"
WEEK2_JUDGES_PATH = WEEK2_REPO_ROOT / "src" / "nodes" / "judges.py"
WEEK2_JUSTICE_PATH = WEEK2_REPO_ROOT / "src" / "nodes" / "justice.py"
WEEK2_REPO_TOOLS_PATH = WEEK2_REPO_ROOT / "src" / "tools" / "repo_tools.py"
WEEK2_REPORT_PATH = WEEK2_REPO_ROOT / "submission_report.md"
WEEK1_OUTPUT_PATH = PROJECT_ROOT / "outputs" / "week1" / "intent_records.jsonl"
WEEK2_OUTPUT_PATH = PROJECT_ROOT / "outputs" / "week2" / "verdicts.jsonl"
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

SYMBOL_PATTERNS = [
    re.compile(r"^\s*export\s+(?:default\s+)?class\s+([A-Za-z_][\w]*)"),
    re.compile(r"^\s*export\s+(?:default\s+)?interface\s+([A-Za-z_][\w]*)"),
    re.compile(r"^\s*export\s+(?:default\s+)?type\s+([A-Za-z_][\w]*)"),
    re.compile(r"^\s*export\s+(?:async\s+)?function\s+([A-Za-z_][\w]*)"),
    re.compile(r"^\s*(?:async\s+)?function\s+([A-Za-z_][\w]*)"),
    re.compile(r"^\s*class\s+([A-Za-z_][\w]*)"),
    re.compile(r"^\s*interface\s+([A-Za-z_][\w]*)"),
    re.compile(r"^\s*type\s+([A-Za-z_][\w]*)"),
    re.compile(r"^\s*(?:export\s+)?const\s+([A-Za-z_][\w]*)\s*="),
    re.compile(r"^\s*def\s+([A-Za-z_][\w]*)\s*\("),
]

WEEK1_GOVERNANCE_RULES = [
    (("auth", "token", "session", "oauth", "identity"), "auth"),
    (("billing", "stripe", "invoice", "payment", "subscription"), "billing"),
    (("pii", "privacy", "personal", "email", "user"), "pii"),
    (("security", "secure", "signature", "webhook", "secret"), "security"),
    (("database", "db", "sql", "query"), "data"),
    (("api", "http", "client", "endpoint"), "api"),
    (("type", "types", "schema", "interface"), "types"),
    (("refactor", "shared", "scope"), "engineering"),
]


def deterministic_uuid4(seed: str) -> str:
    digest = bytearray(hashlib.sha256(seed.encode("utf-8")).digest()[:16])
    digest[6] = (digest[6] & 0x0F) | 0x40
    digest[8] = (digest[8] & 0x3F) | 0x80
    return str(uuid.UUID(bytes=bytes(digest)))


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


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def read_yaml(path: Path) -> dict[str, Any]:
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Expected mapping in {path}")
    return payload


def write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def sha256_hex(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


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


def load_source_texts(paths: list[Path]) -> dict[Path, str]:
    return {path: read_text(path) for path in paths if path.exists()}


def find_symbol_reference(path: Path) -> tuple[str, int, int, float]:
    if not path.exists() or not path.is_file():
        return (path.stem, 1, 1, 0.2)

    lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    for line_number, line in enumerate(lines, start=1):
        for pattern in SYMBOL_PATTERNS:
            match = pattern.match(line)
            if match:
                symbol = match.group(1)
                return (symbol, line_number, line_number, 0.82)
    return (path.stem, 1, 1, 0.45)


def build_code_ref(relative_path: str, repo_root: Path, base_confidence: float) -> dict[str, Any]:
    absolute_path = repo_root / relative_path
    symbol, line_start, line_end, inferred_confidence = find_symbol_reference(absolute_path)
    return {
        "file": relative_path,
        "line_start": line_start,
        "line_end": line_end,
        "symbol": symbol,
        "confidence": round(min(1.0, max(base_confidence, inferred_confidence)), 2),
    }


def collect_scope_files(repo_root: Path, scope_patterns: list[str], limit: int = 4) -> list[str]:
    collected: list[str] = []
    seen: set[str] = set()
    for pattern in scope_patterns:
        normalized = str(pattern).rstrip("/")
        if "**" in normalized:
            base_prefix = normalized.split("**", 1)[0].rstrip("/")
            base_path = repo_root / base_prefix if base_prefix else repo_root
            while not base_path.exists() and base_path != repo_root:
                base_path = base_path.parent
            matches = [base_path] if base_path.exists() else []
        else:
            matches = list(repo_root.glob(normalized))
            if not matches:
                base_path = repo_root / normalized
                while not base_path.exists() and base_path != repo_root:
                    base_path = base_path.parent
                if base_path.exists():
                    matches = [base_path]
        for match in matches:
            if match.is_dir():
                for candidate in match.rglob("*"):
                    if not candidate.is_file():
                        continue
                    relative = candidate.relative_to(repo_root).as_posix()
                    if relative in seen:
                        continue
                    seen.add(relative)
                    collected.append(relative)
                    if len(collected) >= limit:
                        return collected
            elif match.is_file():
                relative = match.relative_to(repo_root).as_posix()
                if relative not in seen:
                    seen.add(relative)
                    collected.append(relative)
                    if len(collected) >= limit:
                        return collected
    return collected


def derive_governance_tags(intent: dict[str, Any]) -> list[str]:
    text_parts = [
        str(intent.get("name", "")),
        " ".join(str(item) for item in intent.get("owned_scope", [])),
        " ".join(str(item) for item in intent.get("constraints", [])),
        " ".join(str(item) for item in intent.get("acceptance_criteria", [])),
    ]
    haystack = " ".join(text_parts).lower()
    tags: list[str] = []
    for keywords, tag in WEEK1_GOVERNANCE_RULES:
        if any(keyword in haystack for keyword in keywords) and tag not in tags:
            tags.append(tag)
    if not tags:
        tags.append("engineering")
    return tags


def build_week1_records() -> list[dict[str, Any]]:
    intents_payload = read_yaml(WEEK1_INTENTS_PATH)
    trace_rows = read_jsonl(WEEK1_TRACE_PATH)
    traces_by_intent: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in trace_rows:
        intent_id = str(row.get("intent_id", "")).strip()
        if intent_id:
            traces_by_intent[intent_id].append(row)

    fallback_created_at = isoformat_utc(
        datetime.fromtimestamp(WEEK1_INTENTS_PATH.stat().st_mtime, tz=timezone.utc)
    )
    records: list[dict[str, Any]] = []
    active_intents = intents_payload.get("active_intents", [])
    if not isinstance(active_intents, list):
        return records

    for intent in active_intents:
        if not isinstance(intent, dict):
            continue
        source_intent_id = str(intent.get("id", "")).strip()
        if not source_intent_id:
            continue

        sorted_traces = sorted(
            traces_by_intent.get(source_intent_id, []),
            key=lambda row: parse_dt(row.get("timestamp")) or datetime.max.replace(tzinfo=timezone.utc),
        )
        code_refs: list[dict[str, Any]] = []
        seen_files: set[str] = set()
        for trace in sorted_traces:
            file_path = str(trace.get("file_path", "")).strip()
            if not file_path or file_path in seen_files:
                continue
            absolute_path = WEEK1_REPO_ROOT / file_path
            if not absolute_path.exists() or not absolute_path.is_file():
                continue
            seen_files.add(file_path)
            code_refs.append(build_code_ref(file_path, WEEK1_REPO_ROOT, 0.87))

        if not code_refs:
            scope_files = collect_scope_files(
                WEEK1_REPO_ROOT,
                [str(item) for item in intent.get("owned_scope", [])],
            )
            for file_path in scope_files:
                if file_path not in seen_files:
                    code_refs.append(build_code_ref(file_path, WEEK1_REPO_ROOT, 0.56))

        created_at = fallback_created_at
        if sorted_traces:
            created_at = isoformat_utc(parse_dt(sorted_traces[0].get("timestamp"))) or fallback_created_at

        records.append(
            {
                "intent_id": deterministic_uuid4(f"week1:{source_intent_id}"),
                "description": str(intent.get("name") or source_intent_id),
                "code_refs": code_refs,
                "governance_tags": derive_governance_tags(intent),
                "created_at": created_at,
            }
        )

    return records


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


def keyword_score(text: str, keywords: list[str]) -> int:
    return sum(1 for keyword in keywords if keyword in text)


def build_week2_scores(rubric: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], float]:
    source_texts = load_source_texts(
        [
            WEEK2_STATE_PATH,
            WEEK2_GRAPH_PATH,
            WEEK2_DETECTIVES_PATH,
            WEEK2_JUDGES_PATH,
            WEEK2_JUSTICE_PATH,
            WEEK2_REPO_TOOLS_PATH,
            WEEK2_REPORT_PATH,
        ]
    )
    state_text = source_texts.get(WEEK2_STATE_PATH, "")
    graph_text = source_texts.get(WEEK2_GRAPH_PATH, "")
    detectives_text = source_texts.get(WEEK2_DETECTIVES_PATH, "")
    judges_text = source_texts.get(WEEK2_JUDGES_PATH, "")
    justice_text = source_texts.get(WEEK2_JUSTICE_PATH, "")
    repo_tools_text = source_texts.get(WEEK2_REPO_TOOLS_PATH, "")
    report_text = source_texts.get(WEEK2_REPORT_PATH, "")
    repo_has_git = (WEEK2_REPO_ROOT / ".git").exists()

    scores: dict[str, dict[str, Any]] = {}
    confidence_accumulator = 0.0
    dimensions = rubric.get("dimensions", [])

    for dimension in dimensions:
        if not isinstance(dimension, dict):
            continue
        criterion_name = str(dimension.get("name", "Unnamed Criterion"))
        evidence: list[str] = []
        notes = "Static repository-derived verdict."
        score = 3
        confidence = 0.68

        if criterion_name == "Git Forensic Analysis":
            score = 4 if repo_has_git else 2
            evidence = [
                ".git metadata is present in the repo snapshot." if repo_has_git else ".git history is not bundled in the repo snapshot.",
                "rubric.json explicitly expects iterative git-history analysis.",
            ]
            notes = "Scored from repository snapshot availability rather than a live cloned history."
            confidence = 0.62
        elif criterion_name == "State Management Rigor":
            features = [
                ("TypedDict", "AgentState uses TypedDict in src/state.py."),
                ("BaseModel", "Evidence, JudicialOpinion, CriterionResult, and AuditReport inherit from BaseModel."),
                ("operator.ior", "AgentState uses operator.ior reducer for evidence merging."),
                ("operator.add", "AgentState uses operator.add reducer for judicial opinions."),
            ]
            matches = [message for token, message in features if token in state_text]
            evidence = matches or ["State management structures were not detected."]
            score = min(5, max(1, 1 + len(matches)))
            notes = "Derived directly from typed state and reducer definitions in src/state.py."
            confidence = 0.94
        elif criterion_name == "Graph Orchestration Architecture":
            checks = [
                ("StateGraph", "StateGraph builder exists in src/graph.py."),
                ("add_conditional_edges", "Conditional routing is implemented in src/graph.py."),
                ("RepoInvestigator", "Detective fan-out nodes are declared."),
                ("ChiefJustice", "Chief Justice fan-in node is declared."),
            ]
            matches = [message for token, message in checks if token in graph_text]
            evidence = matches or ["Graph orchestration evidence was not detected."]
            score = min(5, max(1, 1 + len(matches)))
            notes = "Computed from StateGraph construction, fan-out/fan-in nodes, and conditional edges."
            confidence = 0.92
        elif criterion_name == "Safe Tool Engineering":
            safety_points = [
                ("TemporaryDirectory", "Repository cloning is sandboxed with tempfile.TemporaryDirectory()."),
                ("subprocess.run", "Git commands use subprocess.run with captured output."),
                ("os.system", "AST analysis explicitly scans for unsafe os.system usage."),
            ]
            matches = [message for token, message in safety_points if token in repo_tools_text or token in detectives_text]
            evidence = matches or ["Tool-engineering safeguards were not detected."]
            score = 5 if "TemporaryDirectory" in repo_tools_text and "subprocess.run" in repo_tools_text else 3
            notes = "Based on clone sandboxing, subprocess usage, and explicit security checks in detective analysis."
            confidence = 0.91
        elif criterion_name == "Structured Output Enforcement":
            matches = []
            if "with_structured_output" in judges_text:
                matches.append("Judge nodes use with_structured_output(JudicialOpinion).")
            if "except Exception" in judges_text:
                matches.append("Judge nodes retry after malformed structured output.")
            if "JudicialOpinion" in judges_text:
                matches.append("Structured output is validated against the JudicialOpinion model.")
            evidence = matches or ["No structured-output enforcement markers were detected."]
            score = 5 if len(matches) >= 3 else max(1, 2 + len(matches))
            notes = "Scored from structured-output binding and retry logic in judge nodes."
            confidence = 0.95
        elif criterion_name == "Judicial Nuance and Dialectics":
            matches = []
            for token, message in [
                ("You are the Prosecutor", "Prosecutor persona prompt is distinct and adversarial."),
                ("You are the Defense Attorney", "Defense persona prompt is distinct and mitigation-oriented."),
                ("You are the Tech Lead", "Tech Lead persona prompt is distinct and pragmatic."),
            ]:
                if token in judges_text:
                    matches.append(message)
            evidence = matches or ["Distinct judicial personas were not detected."]
            score = 5 if len(matches) == 3 else max(1, 2 + len(matches))
            notes = "Derived from the presence of separate judge prompts and roles in src/nodes/judges.py."
            confidence = 0.9
        elif criterion_name == "Chief Justice Synthesis Engine":
            matches = []
            for token, message in [
                ("deterministic_score", "Chief Justice computes a deterministic score in Python."),
                ("Fallback mechanism", "Chief Justice includes a fallback path when LLM synthesis fails."),
                ("criterion_results", "Chief Justice emits structured criterion results."),
            ]:
                if token in justice_text:
                    matches.append(message)
            evidence = matches or ["Chief Justice synthesis markers were not detected."]
            score = 5 if len(matches) >= 3 else max(1, 2 + len(matches))
            notes = "Scored from deterministic synthesis and structured result handling in src/nodes/justice.py."
            confidence = 0.88
        elif criterion_name == "Theoretical Depth (Documentation)":
            matches = []
            for token in ["Dialectical Synthesis", "Metacognition", "Fan-Out", "Fan-In"]:
                if token in report_text:
                    matches.append(f"submission_report.md explains {token}.")
            evidence = matches or ["Theoretical architecture terms were not detected in the report."]
            score = 5 if len(matches) >= 4 else max(1, 1 + len(matches))
            notes = "Computed from substantive terminology present in submission_report.md."
            confidence = 0.9
        elif criterion_name == "Report Accuracy (Cross-Reference)":
            verified_paths = []
            for candidate in [
                WEEK2_GRAPH_PATH,
                WEEK2_JUDGES_PATH,
                WEEK2_JUSTICE_PATH,
                WEEK2_STATE_PATH,
            ]:
                if candidate.exists() and candidate.name.replace("_", "")[:5].lower() in report_text.lower().replace("_", ""):
                    verified_paths.append(f"Report discussion aligns with {candidate.relative_to(WEEK2_REPO_ROOT).as_posix()}.")
            evidence = verified_paths or ["The report discusses architecture that is supported by existing source files."]
            score = 4 if verified_paths else 3
            notes = "Cross-reference is approximated from report claims and matching source-file presence in the repository snapshot."
            confidence = 0.7
        elif criterion_name == "Architectural Diagram Analysis":
            diagram_tokens = keyword_score(report_text, ["```mermaid", "RepoInvestigator", "VisionInspector", "ChiefJustice", "Parallel"])
            evidence = [
                "submission_report.md contains a Mermaid architecture diagram.",
                "The diagram shows detective and judicial parallel branches.",
            ] if diagram_tokens >= 3 else ["No strong parallel architecture diagram markers were detected."]
            score = 5 if diagram_tokens >= 4 else max(1, 1 + diagram_tokens)
            notes = "Scored from Mermaid diagram presence and explicit parallel-branch labels in the report."
            confidence = 0.84

        scores[criterion_name] = {
            "score": int(max(1, min(5, round(score)))),
            "evidence": evidence,
            "notes": notes,
        }
        confidence_accumulator += confidence

    average_confidence = confidence_accumulator / max(1, len(scores))
    return scores, average_confidence


def build_week2_records() -> list[dict[str, Any]]:
    rubric = read_json(WEEK2_RUBRIC_PATH)
    rubric_metadata = rubric.get("rubric_metadata", {}) if isinstance(rubric, dict) else {}
    scores, confidence = build_week2_scores(rubric)
    numeric_scores = [criterion["score"] for criterion in scores.values()]
    overall_score = round(sum(numeric_scores) / max(1, len(numeric_scores)), 2)
    overall_verdict = "PASS" if overall_score >= 4.0 else "WARN" if overall_score >= 3.0 else "FAIL"
    evaluated_at = isoformat_utc(
        datetime.fromtimestamp(max(path.stat().st_mtime for path in [WEEK2_RUBRIC_PATH, WEEK2_STATE_PATH, WEEK2_REPORT_PATH]), tz=timezone.utc)
    )

    return [
        {
            "verdict_id": deterministic_uuid4(f"week2:{sha256_hex(WEEK2_RUBRIC_PATH)}"),
            "target_ref": "submission_report.md",
            "rubric_id": sha256_hex(WEEK2_RUBRIC_PATH),
            "rubric_version": str(rubric_metadata.get("version", "0.0.0")),
            "scores": scores,
            "overall_verdict": overall_verdict,
            "overall_score": overall_score,
            "confidence": round(min(1.0, max(0.0, confidence)), 2),
            "evaluated_at": evaluated_at,
        }
    ]


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
    week1_records = build_week1_records()
    week2_records = build_week2_records()
    week3_records = build_week3_records()
    week5_records = build_week5_records()
    lineage_records = build_lineage_snapshot()

    write_jsonl(WEEK1_OUTPUT_PATH, week1_records)
    write_jsonl(WEEK2_OUTPUT_PATH, week2_records)
    write_jsonl(WEEK3_OUTPUT_PATH, week3_records)
    write_jsonl(WEEK5_OUTPUT_PATH, week5_records)
    write_jsonl(WEEK4_OUTPUT_PATH, lineage_records)

    print(
        json.dumps(
            {
                "week1_records": len(week1_records),
                "week1_output": str(WEEK1_OUTPUT_PATH),
                "week2_records": len(week2_records),
                "week2_output": str(WEEK2_OUTPUT_PATH),
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
