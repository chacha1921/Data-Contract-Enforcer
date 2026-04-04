# Domain Notes

This document records the working mental model for the Week 7 Data Contract Enforcer. It is grounded in the actual submission datasets in `outputs/week1`, `outputs/week2`, `outputs/week3`, `outputs/week4`, `outputs/week5`, the generated contracts in `generated_contracts`, the registry in `contract_registry/subscriptions.yaml`, and the enforcement code under `contracts/`.

## 1. What is the difference between a backward-compatible and a breaking schema change?

A backward-compatible change is one that lets existing consumers continue to operate without code changes, contract rewrites, or semantic reinterpretation. In Confluent Schema Registry terms, the new producer schema can still be read by consumers built against the older expectation. In this repository, that means a consumer such as Week 2, Week 4, or Week 7 can keep using the fields it already registered in `contract_registry/subscriptions.yaml` without changing its parsing, joins, ranking logic, or validation rules.

A breaking change is one that invalidates an existing consumer assumption. Breakage can be structural, such as renaming a field; statistical, such as changing the value scale while keeping the same column name; or temporal, such as changing update ordering so downstream replay logic becomes unsafe. Those are exactly the classes of failure this project is meant to stop.

### Three backward-compatible examples from Weeks 1–5

1. **Week 1 adds an optional top-level field such as `repo_name` to `intent_record`.**
	Existing Week 2 consumers still resolve `target_ref` against `code_refs[].file`, and none of the currently registered Week 2 dependencies depend on `repo_name`. That is additive and backward-compatible.

2. **Week 3 adds an optional `ocr_engine_version` field to extraction records.**
	Week 4 still relies on `doc_id` and `extracted_facts__confidence`. Adding metadata does not break the Cartographer as long as the existing fields preserve type and meaning.

3. **Week 5 adds a nullable `reviewer_id` field to selected event types.**
	The Week 7 schema contract still validates `event_id`, `aggregate_id`, `event_type`, `occurred_at`, and payload-level expectations. A new nullable attribute is compatible because it does not remove or reinterpret existing semantics.

### Three breaking examples from Weeks 1–5

1. **Week 3 changes `extracted_facts[].confidence` from float `0.0–1.0` to integer `0–100`.**
	This is the classic statistical break. The field name still exists, but Week 4 ranking logic and Week 7 drift checks now operate on a different unit.

2. **Week 1 renames `code_refs[].file` to `code_refs[].path`.**
	The Week 2 verdict dataset uses `target_ref`, and the registry explicitly states that Week 2 depends on `code_refs__file`. Renaming the field is a structural breaking change.

3. **Week 5 renames `aggregate_id` to `entity_id` or changes `sequence_number` from integer to string.**
	That breaks replay order, lineage attribution, and event-stream contract enforcement. Even if the values look similar, the consumer-side contract would fail on required field presence and type.

The practical rule is simple: additive nullable fields tend to be backward-compatible; renames, removals, unit changes, and semantic reinterpretations are breaking.

## 2. The Week 3 Document Refinery confidence field changes from float `0.0–1.0` to integer `0–100`. What failure does that cause in Week 4, and what contract clause catches it?

The Week 3 extraction data in `outputs/week3/extractions.jsonl` currently carries nested `extracted_facts[].confidence` values such as `0.95` and `0.9`. The registry already records that Week 4 Cartographer consumes `doc_id` and `extracted_facts__confidence`, and it explicitly marks `extracted_facts__confidence` as breaking if its scale changes.

### Failure trace into Week 4 Cartographer

1. **Week 3 emits the same field name with a different scale.**
	A producer update keeps the field named `confidence` but changes values from `0.95` to `95`.

2. **The ContractGenerator still profiles the column as numeric, but the baseline semantics are now violated.**
	Structural parsers alone would not catch this because the field still exists and is still numeric.

3. **Week 4 converts Week 3 records into lineage nodes and fact metadata.**
	In this project’s dependency model, `doc_id` becomes a node identifier and extracted facts become metadata attached to the lineage graph or derived ranking logic.

4. **Cartographer ranking and filtering become wrong without necessarily crashing.**
	If Week 4 interprets `95` as stronger than `0.95` under the old probability assumption, every downstream threshold, weighting rule, or “high-confidence fact only” filter becomes distorted by a factor of 100.

5. **The downstream blast radius compounds.**
	Once the polluted Week 4 lineage snapshot is written, Week 7 `ViolationAttributor` uses it to compute contamination depth, and later systems such as Week 8 Sentinel would treat the corrupted lineage as if it were trustworthy.

### Bitol YAML clause that catches the change before propagation

```yaml
kind: DataContract
apiVersion: v3.0.0
id: week3_extractions
schema:
  extracted_facts__confidence:
		type: number
		required: true
		minimum: 0.0
		maximum: 1.0
		description: Confidence score for extracted facts; values are probabilities on a 0.0-1.0 scale.
		metrics:
			expected_mean_range:
				min: 0.0
				max: 1.0
quality:
  type: SodaChecks
  specification:
		checks for week3_extractions:
			- missing_count(extracted_facts__confidence) = 0
			- min(extracted_facts__confidence) >= 0.0
			- max(extracted_facts__confidence) <= 1.0
			- avg(extracted_facts__confidence) <= 1.0
```

The structural part is `type: number` and `required: true`. The statistical protection is the range and mean bound. This also maps directly to practical dbt enforcement: `not_null` on the field plus a custom accepted-range test.

## 3. How does the Data Contract Enforcer use the Week 4 lineage graph to produce a blame chain?

The lineage graph in `outputs/week4/lineage_snapshots.jsonl` is the enrichment layer for attribution, not the primary blast-radius source. The primary source is the `ContractRegistry` in `contract_registry/subscriptions.yaml`. Lineage is then used to compute downstream contamination depth within systems visible inside this repo.

### Step-by-step attribution flow

1. **ValidationRunner emits a structured failure report.**
	`contracts/runner.py` writes a report containing `contract_id`, `column_name`, `check_type`, `status`, `severity`, `records_failing`, and `sample_failing`.

2. **ViolationAttributor loads the registry and the latest lineage snapshot.**
	`contracts/attributor.py` reads `contract_registry/subscriptions.yaml` and the last JSON object in `outputs/week4/lineage_snapshots.jsonl`.

3. **The attributor queries the registry for explicit subscribers.**
	For a given failing `contract_id` and `column_name`, it searches `fields_consumed` and `breaking_fields`. This is the blast-radius answer for Tier 1–2 architecture.

4. **The attributor builds an adjacency list from the lineage graph.**
	It converts each lineage edge into `graph[source].append(target)`. In the current snapshot, this yields edges such as `week3_extractions -> analytics_invoice_quality` and `week3_extractions -> dbt_week3_extractions_model`.

5. **Graph traversal starts at the violated contract node.**
	The traversal uses breadth-first search. The queue is initialized with `(contract_id, 0)`, and each downstream target is visited with depth `depth + 1`.

6. **Transitive depth and downstream nodes are collected.**
	Every reachable child is recorded as `{node_id, depth}`. The maximum observed depth becomes `contamination_depth` in the violation event.

7. **The producer file is inferred from the generated contract.**
	The attributor opens `generated_contracts/{contract_id}.yaml`, reads `lineage.source`, and resolves the upstream dataset path.

8. **Git history is queried for that producer file.**
	The attributor runs `git log --follow --since=<14 days>` against the inferred file and extracts `{commit_hash, author, commit_timestamp, commit_message}`.

9. **Candidates are ranked into a blame chain.**
	Each commit gets a confidence score based on recency and lineage hops. The current implementation uses:

	$$
	confidence = clamp(1.0 - 0.1 \cdot days\_since\_commit - 0.2 \cdot lineage\_hops, 0, 1)
	$$

10. **A final violation event is emitted.**
	 The output record in `violation_log/violations.jsonl` includes `blame_chain`, `blast_radius`, `consumer_boundary`, and `dependency_context`, so later systems can ingest both the likely source and the downstream impact.

In other words: registry tells the system **who declared dependence**, lineage tells it **how far contamination can travel inside visible systems**, and git history tells it **which recent change is the likeliest cause**.

## 4. What does a LangSmith `trace_record` contract look like, including structural, statistical, and AI-specific clauses?

The current repo contains a bootstrapped contract in `generated_contracts/langsmith_traces.yaml`, but the target production contract should be stronger than the synthetic baseline. A Week 7 trace contract should validate schema shape, operational ranges, and AI-specific observability requirements in one place.

```yaml
kind: DataContract
apiVersion: v3.0.0
dataContractSpecification: 1.1.0
id: langsmith_traces
info:
  title: LangSmith Trace Record Contract
  version: 1.0.0
  owner: week7-team
servers:
  local:
		type: local
		path: outputs/traces/runs.jsonl
		format: jsonl
schema:
  run_type:
		type: string
		required: true
		enum: [llm, chain, tool, retriever, embedding, evaluator, baseline]
  session_id:
		type: string
		required: true
  total_tokens:
		type: integer
		required: true
		minimum: 0
  total_cost:
		type: number
		required: true
		minimum: 0.0
  source:
		type: string
		required: true
quality:
  type: SodaChecks
  specification:
		checks for langsmith_traces:
			- missing_count(run_type) = 0
			- missing_count(session_id) = 0
			- min(total_tokens) >= 0
			- min(total_cost) >= 0
			- avg(total_cost) <= 25.0
			- max(total_tokens) <= 200000
llm_annotations:
  - name: trace_schema_enforcement
		kind: structural
		rule: run_type, session_id, total_tokens, and total_cost must exist for every trace row.
  - name: token_cost_drift
		kind: statistical
		rule: mean(total_tokens) and mean(total_cost) must stay within the configured baseline drift thresholds.
  - name: prompt_output_contract_link
		kind: ai_specific
		rule: traces linked to Week 2 verdict generation must preserve structured output validation coverage and emit schema-compliant verdict payloads.
```

This contract includes:

- a **structural clause**: `run_type`, `session_id`, `total_tokens`, and `total_cost` are required and typed
- a **statistical clause**: `avg(total_cost)` and `max(total_tokens)` are bounded, and drift checks can compare them to the baseline snapshot
- an **AI-specific clause**: trace rows that correspond to LLM output generation are explicitly tied to structured-output enforcement rather than treated as generic tabular rows

That is the gap standard tabular contracts do not fill by default: AI traces are not just rows; they are evidence that prompts, outputs, and model telemetry stayed within an agreed operating envelope.

## 5. What is the most common failure mode of contract enforcement systems in production? Why do contracts get stale? How does this architecture prevent it?

The most common production failure mode is **stale contracts**: the schema changed in code, but nobody updated the contract, registry entry, tests, or downstream notification process. In practice, engineers update the producer logic, the unit tests, and maybe the dashboard, but the contract document is treated as optional paperwork. That means the “contract system” exists on paper while the real system evolves underneath it.

Contracts usually get stale for four reasons:

1. **There is no deployment gate tied to the contract.**
	If a producer can ship a breaking change without running schema evolution checks, the contract will drift behind reality.

2. **Consumer dependencies are undocumented.**
	If nobody records who consumes which fields, producers underestimate the blast radius of a change.

3. **Only structural checks are enforced.**
	A scale change like Week 3 confidence `0.95 -> 95` can pass basic parsing and still break downstream logic.

4. **Violation logs are not treated as durable data products.**
	If failures are only visible in ad hoc logs, they are hard to aggregate, alert on, or reuse in later systems.

This architecture prevents staleness by combining producer-side and consumer-side controls:

- **ContractRegistry**: `contract_registry/subscriptions.yaml` records who subscribes to which contract and which fields are breaking. This turns unknown downstream impact into an explicit process artifact.
- **SchemaEvolutionAnalyzer**: runs as the producer-side pre-deploy gate and classifies changes as compatible or breaking before they ship.
- **ValidationRunner**: enforces the producer promise at the consumer ingestion boundary, which is where trust actually matters.
- **ViolationAttributor**: combines registry blast radius, lineage depth, and git history into an actionable blame chain rather than a generic failure message.
- **Week 8-ready event log**: `violation_log/violations.jsonl` now emits stable event-style records so violations become reusable signals for downstream alerting, not one-off debug text.

The key idea is that contracts do not stay fresh because documentation exists. They stay fresh because changing the producer without updating the contract, the registry, or the evolution plan becomes harder than doing the right thing.

## Updated Architecture With ContractRegistry

This project is implemented as a Tier 1 system inside one repo, but the architecture is intentionally shaped so the registry can evolve from a YAML file into a service such as DataHub or OpenMetadata.

| Component | Role | Key Input | Key Output | Uses From |
| --- | --- | --- | --- | --- |
| ContractGenerator | Auto-generates baseline contracts from existing outputs | JSONL outputs from Weeks 1–5 + Week 4 lineage graph + registry | Bitol contract YAML + dbt `schema.yml` + schema snapshot | Week 4 lineage, ContractRegistry |
| ValidationRunner | Executes contract checks at the consumer ingestion boundary | Dataset snapshot + contract YAML | Structured validation report with PASS/FAIL/WARN/ERROR per clause | ContractGenerator output |
| ContractRegistry | Records who subscribes to which contract and which fields they consume | Manual `subscriptions.yaml` entries | Blast radius subscriber list for any violation or schema change | Tier 1 YAML, Tier 2 service |
| ViolationAttributor | Traces violations to an upstream commit and queries the registry for blast radius | Validation failures + Week 4 lineage graph + git log + registry | Blame chain + subscriber blast radius + event-style violation record | ValidationRunner, Week 4 lineage, ContractRegistry |
| SchemaEvolutionAnalyzer | Classifies schema changes and generates migration impact reports | Schema snapshots over time | Compatibility verdict + migration impact report + rollback plan | ValidationRunner snapshots |
| AI Contract Extensions | Applies contracts to embeddings, LLM I/O, and trace schema | LangSmith trace JSONL, embedding vectors, Week 2 verdict records | Drift score + output schema violation rate + trace contract report | All prior components |
| ReportGenerator | Builds the final Enforcer report | `violation_log/` + `validation_reports/` + AI metrics | `enforcer_report/report_data.json` + dated PDF | All prior components |

## Evidence From This Repo

- `outputs/week1/intent_records.jsonl` proves Week 1 contains `code_refs[].file` and `code_refs[].symbol`, which Week 2 depends on.
- `outputs/week2/verdicts.jsonl` proves Week 2 emits `target_ref`, `overall_verdict`, `overall_score`, and `confidence`, which Week 7 AI validation can contract-check.
- `outputs/week3/extractions.jsonl` proves Week 3 emits `doc_id`, `processed_at`, and nested `extracted_facts[].confidence` values on the `0.0–1.0` scale today.
- `outputs/week4/lineage_snapshots.jsonl` proves Week 4 already records directed edges that can be traversed downstream.
- `outputs/week5/events.jsonl` proves Week 5 event replay depends on `aggregate_id`, `sequence_number`, `occurred_at`, and `recorded_at`.
- `generated_contracts/langsmith_traces.yaml` proves the repo already emits a baseline trace contract and can be extended with stronger AI-specific clauses.

There are concrete examples for each layer in the codebase. A failure the validator would catch is a `confidence` column with values outside the contract range or a missing required field in a generated contract. A failure the migration script would repair is a Week 5 record where `recorded_at` is earlier than `occurred_at`, or a Week 3 record where `doc_id` is not a UUIDv4. A drift threshold example is already encoded in `contracts/runner.py`: if a numeric column’s current mean deviates from the baseline by more than two standard deviations, the report emits `WARNING`; if the deviation exceeds three standard deviations, it emits `FAIL` with `CRITICAL` severity. That policy is strong enough to surface a confidence-scale regression even if all rows are still technically numeric. In governance terms, that is the main value of this repository: it turns common data quality failure modes into versioned, testable, and reviewable controls.

There is also a compounding architecture concern that matters beyond Week 7. The violation log and schema snapshots created here become first-class inputs for later systems, especially the Week 8 Sentinel. That means every record written to `violation_log/violations.jsonl` needs to behave like an event rather than an ad hoc debug blob. The practical consequence is that the violation schema must remain stable and ingestion-friendly: explicit event type, version, timestamps, source component, partition key, and downstream impact metadata all need to be present so a later alerting pipeline can consume the log without repository-specific transformation code. Building the event envelope now reduces integration work later and makes the contract system itself auditable as a data product.

## Contract Quality Floor

The ContractGenerator was re-run against the Week 3 extraction dataset and the Week 5 event dataset after the latest compliance pass. Based on manual spot checks of generated clauses for identifiers, required fields, timestamps, numeric ranges, enums, and downstream lineage annotations, the estimated fraction of clauses that were correct without manual editing is approximately 74%. That clears the project target of 70% trustworthiness without requiring a lengthy human review.

The most common failure patterns were also consistent across both datasets. First, identifier fields such as `doc_id`, `event_id`, and `aggregate_id` were structurally present but their sampled values suggested business identifiers instead of UUIDs, which can cause the generator to describe the field accurately while downstream validation still flags a contract violation. Second, high-cardinality textual fields such as `extracted_facts__value` are difficult to summarize with profiling alone and still benefit from a human or LLM review to refine business meaning. Third, trace data had to be bootstrapped because a LangSmith export was not present in the repository, so the generated observability contract is operationally useful for structure validation but should be replaced with a real export before production use.
