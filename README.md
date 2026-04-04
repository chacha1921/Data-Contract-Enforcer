# Data-Contract-Enforcer

Data-Contract-Enforcer is a small data quality project for generating, validating, and preflighting Bitol-compatible data contracts from JSONL datasets.

The repository focuses on four submission datasets:

- Week 1 intent records in `outputs/week1/intent_records.jsonl`
- Week 2 verdict records in `outputs/week2/verdicts.jsonl`
- Week 3 document extractions in `outputs/week3/extractions.jsonl`
- Week 5 event records in `outputs/week5/events.jsonl`

It includes:

- a contract generator that profiles source data and emits Bitol + dbt YAML
- a validation runner that checks structure, types, ranges, UUIDs, and drift
- a migration script for rebuilding Week 1-5 submission data from repo-backed sources
- a preflight checker that audits the repository against the Thursday submission requirements

## Current Data Sources

The current submission outputs are sourced from the real repository data stored under `repos/`:

- Week 1 outputs are built from `repos/week1/.orchestration/active_intents.yaml` and `repos/week1/.orchestration/agent_trace.jsonl`
- Week 2 outputs are built from `repos/week2/rubric.json`, `repos/week2/src/state.py`, and related Week 2 audit source files
- Week 3 outputs are built from `repos/week3/.refinery/extraction_ledger.jsonl`, `repos/week3/.refinery/extracted/`, and `repos/week3/.refinery/profiles/`
- Week 5 outputs are built from `repos/week5/data/seed_events.jsonl`

The repo-backed submission outputs were rebuilt into:

- `outputs/week1/intent_records.jsonl`
- `outputs/week2/verdicts.jsonl`
- `outputs/week3/extractions.jsonl`
- `outputs/week5/events.jsonl`

using `outputs/migrate/build_real_submission_data.py` and then normalized with `outputs/migrate/align_data.py` before contract generation and validation.

## Repository Layout

```text
contracts/
	generator.py          Generate Bitol and dbt contracts from JSONL
	runner.py             Validate JSONL against generated contracts
outputs/
	migrate/
		align_data.py       Normalize Week 3 and Week 5 source files
	week1/
		intent_records.jsonl Week 1 intent dataset
	week2/
		verdicts.jsonl      Week 2 verdict dataset
	week3/
		extractions.jsonl   Week 3 extraction dataset
	week5/
		events.jsonl        Week 5 event dataset
generated_contracts/    Generated Bitol and dbt contract files
schema_snapshots/       Baseline statistics for drift checks
validation_reports/     JSON output from ValidationRunner
DOMAIN_NOTES.md         Phase 0 domain write-up
SUBMISSION_CHECKLIST.md Manual submission checklist
preflight_submission.py Automated submission readiness checker
```

## What The Project Does

### Contract Generation

`contracts/generator.py` reads JSON or JSONL input, flattens nested records, profiles columns, and writes:

- a Bitol-compatible YAML contract
- a dbt schema YAML companion

Key features:

- optional `--contract-id` and `--lineage` arguments
- evaluator-friendly defaults from the source path
- Week 3 flattening support for `extracted_facts`
- numeric statistics including `min`, `max`, `mean`, `stddev`, `p25`, `p50`, `p75`, and `p95`
- downstream lineage injection from `outputs/week4/lineage_snapshots.jsonl`

Default filename mapping:

- `outputs/week3/extractions.jsonl` → `generated_contracts/week3_extractions.yaml`
- `outputs/week5/events.jsonl` → `generated_contracts/week5_events.yaml`

dbt companions are written as:

- `generated_contracts/week3_extractions_dbt.yml`
- `generated_contracts/week5_events_dbt.yml`

### Validation

`contracts/runner.py` validates JSONL records against a Bitol contract.

Implemented checks include:

- required-field checks
- type checks
- UUID format checks
- numeric range checks
- statistical drift checks against `schema_snapshots/baselines.json`

Drift behavior:

- first run initializes the baseline file from current numeric columns
- later runs emit `WARNING` if mean drift exceeds `2 * stddev`
- later runs emit `FAIL` with `CRITICAL` severity if mean drift exceeds `3 * stddev`

Validation reports are written to `validation_reports/` as JSON.

### Migration

`outputs/migrate/align_data.py` prepares the submission datasets for contract generation.

Week 3 alignment:

- ensures each record has a UUIDv4 `doc_id`
- ensures each `extracted_facts[].confidence` is a float between `0.0` and `1.0`
- rescales confidence values above `1.0` by dividing by `100`

Week 5 alignment:

- ensures `recorded_at >= occurred_at`
- normalizes timestamps to ISO 8601 UTC form
- resets `sequence_number` to start at `1` and increase per `aggregate_id`

### Preflight Submission Check

`preflight_submission.py` verifies repository readiness before submission.

It checks:

- `DOMAIN_NOTES.md` word count and section presence
- placeholder text still left in the domain notes template
- expected generated contracts and dbt files
- minimum JSONL record counts for Week 3 and Week 5
- presence and shape of at least one validation report
- presence of runnable scripts and the migration script

## Setup

This repo uses Python 3.13 and a local virtual environment.

If you are setting up from scratch:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .
```

If you are already using the repo venv:

```bash
source .venv/bin/activate
```

## Typical Workflow

### 1. Align the raw outputs

```bash
python outputs/migrate/align_data.py
```

This rewrites:

- `outputs/week3/extractions.jsonl`
- `outputs/week5/events.jsonl`

### 2. Generate contracts

Evaluator-compatible commands:

```bash
python contracts/generator.py --source outputs/week3/extractions.jsonl --output generated_contracts/
python contracts/generator.py --source outputs/week5/events.jsonl --output generated_contracts/
```

Optional explicit form:

```bash
python contracts/generator.py \
	--source outputs/week3/extractions.jsonl \
	--contract-id week3_extractions \
	--lineage outputs/week4/lineage_snapshots.jsonl \
	--output generated_contracts/
```

### 3. Run validation

```bash
python contracts/runner.py --contract generated_contracts/week3_extractions.yaml --data outputs/week3/extractions.jsonl
python contracts/runner.py --contract generated_contracts/week5_events.yaml --data outputs/week5/events.jsonl
```

Expected effects:

- writes a JSON report in `validation_reports/`
- creates `schema_snapshots/baselines.json` on the first real run

### 4. Run preflight

```bash
python preflight_submission.py
```

JSON output mode:

```bash
python preflight_submission.py --json
```

## Generated Artifacts

After a complete successful run, the repository should contain at least:

- `generated_contracts/week3_extractions.yaml`
- `generated_contracts/week3_extractions_dbt.yml`
- `generated_contracts/week5_events.yaml`
- `generated_contracts/week5_events_dbt.yml`
- `validation_reports/*.json`
- `schema_snapshots/baselines.json`

## Submission Requirements Summary

For the Thursday submission, make sure the repo includes:

- `DOMAIN_NOTES.md` with all five Phase 0 answers and real evidence
- at least 50 records in both Week 3 and Week 5 output files
- generated Bitol + dbt contract pairs for Week 3 and Week 5
- at least one real validation report generated from your own data
- committed migrated output if original upstream files did not match the target format

See `SUBMISSION_CHECKLIST.md` for the manual checklist and `preflight_submission.py` for the automated check.

## Current Status Reminder

The automation in this repository is ready, but the final submission still depends on real data files and generated artifacts being present in the repo.

If `preflight_submission.py` reports failures, fix those artifacts before submitting.
