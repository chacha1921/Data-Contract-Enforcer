# Domain Notes

Use this document as the final write-up for Phase 0. Replace the bracketed prompts with evidence from your own systems, logs, traces, schemas, screenshots, and sample records. The target is **800+ words minimum** across the five sections below.

## 1. What business or operational problem does this data product solve?

Describe the concrete problem the system solves in your environment.

Fill in:

- **System name:** [Example: Week 3 extraction pipeline / Week 5 event stream]
- **Primary users:** [Analysts, downstream services, ML pipeline, operations team, etc.]
- **Decision or workflow supported:** [What people or systems do with the data]
- **Business risk if the data is wrong or delayed:** [Missed SLA, wrong decisions, broken automation, customer-facing impact]

Write 1-2 paragraphs that answer:

- What is the real-world workflow behind this dataset?
- Why does this dataset exist?
- What breaks if it is incomplete, stale, or malformed?

Evidence to include from your repo or systems:

- A concrete example record from `outputs/week3/extractions.jsonl`.
- A concrete example record from `outputs/week5/events.jsonl`.
- A short explanation of how those records are consumed downstream.

## 2. What are the key entities, attributes, and identifiers in the domain?

Describe the domain objects represented in the data and why the identifiers matter.

Cover at least:

- **Week 3 extraction entity:** [Document, extraction result, extracted fact, source artifact]
- **Week 5 event entity:** [Aggregate, event, command result, lifecycle transition]
- **Primary identifiers:** [Examples: `doc_id`, `aggregate_id`, event IDs, UUID fields]
- **High-value attributes:** [Examples: confidence, timestamps, sequence numbers, status, event type]

Write 1-2 paragraphs that answer:

- What is the core unit of data in each dataset?
- Which fields identify records uniquely?
- Which fields are descriptive versus operational versus analytical?

Evidence to include:

- A field-by-field example from one Week 3 record.
- A field-by-field example from one Week 5 record.
- A note about why identifier quality matters for joins, lineage, or replay.

## 3. What data quality rules are critical in this domain?

List the data quality expectations that must hold for the datasets to be trusted.

You should discuss rules such as:

- Required fields must be present.
- UUIDs must be valid where expected.
- `confidence` must remain in the `0.0-1.0` range.
- `recorded_at >= occurred_at` for events.
- `sequence_number` must begin at `1` and increase per `aggregate_id`.
- Numeric drift in key metrics must be caught before downstream breakage.

Write 1-2 paragraphs that answer:

- Which rules are structural, semantic, and statistical?
- Which rules are the highest priority and why?
- Which rules would catch a scale error like `95` instead of `0.95`?

Evidence to include:

- One example of a rule encoded in `contracts/generator.py`.
- One example of a rule enforced in `contracts/runner.py`.
- One example of a migration correction performed by `outputs/migrate/align_data.py`.

## 4. Who are the producers and consumers of this data, and how does lineage matter?

Explain where the data comes from and where it goes next.

Cover:

- **Producers:** [Original application, extractor, event producer, upstream pipeline]
- **Consumers:** [Validation runner, analytics consumers, dbt models, dashboards, ML features, audit workflows]
- **Lineage relevance:** [Why downstream awareness matters for contracts and change management]

Write 1-2 paragraphs that answer:

- What system creates the data?
- What systems or people rely on it later?
- Why is downstream lineage included in the generated Bitol contract?

Evidence to include:

- A short explanation of how `outputs/week4/lineage_snapshots.jsonl` informs downstream contract metadata.
- A concrete list of likely downstream consumers for Week 3 and Week 5 data.
- A note on how dbt schema tests support downstream trust.

## 5. What are the main failure modes, changes, and governance concerns in this domain?

Describe the real risks you expect over time and how contracts help manage them.

Discuss areas such as:

- Schema drift from upstream changes.
- Confidence scale drift (`0-1` vs `0-100`).
- Missing IDs or malformed UUIDs.
- Event ordering and replay errors.
- Incomplete migrations or null inflation.
- Silent changes to enums or accepted values.

Write 2-3 paragraphs that answer:

- Which changes are most likely over time?
- Which failures are easiest to miss without contracts?
- How do contract generation, migration, validation, and drift checks work together as governance controls?

Evidence to include:

- A concrete example of a failure your validator would catch.
- A concrete example of a failure your migration script would repair before generation.
- A concrete example of a drift threshold that would trigger `WARNING` or `CRITICAL` severity.

## Final Evidence Checklist

Before submitting, confirm this document includes:

- At least **800 words** total.
- Evidence from your own files and outputs, not hypothetical examples only.
- Explicit references to Week 3 extractions and Week 5 events.
- At least one mention each of contract generation, validation, migration, and drift detection.
- Concrete examples of identifiers, constraints, and downstream consumers.
