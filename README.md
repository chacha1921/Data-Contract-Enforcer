# Data Contract Enforcer

This repository is submission-ready around six evaluator-facing entrypoints:

1. `contracts/generator.py`
2. `contracts/runner.py`
3. `contracts/attributor.py`
4. `contracts/schema_analyzer.py`
5. `contracts/ai_extensions.py`
6. `contracts/report_generator.py`

## Fresh clone setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .
```

The repository already contains real source-backed outputs under `outputs/`, generated contracts under `generated_contracts/`, timestamped snapshots under `schema_snapshots/`, validation JSON under `validation_reports/`, and the machine-generated report under `enforcer_report/`.

## Quick verification guide

### 1. Generate contracts

```bash
python contracts/generator.py --source outputs/week3/extractions.jsonl --output generated_contracts/
python contracts/generator.py --source outputs/week5/events.jsonl --output generated_contracts/
```

Expected outputs:

- `generated_contracts/week3_extractions.yaml`
- `generated_contracts/week3_extractions_dbt.yml`
- `generated_contracts/week5_events.yaml`
- `generated_contracts/week5_events_dbt.yml`
- fresh timestamped snapshots in `schema_snapshots/week3-document-refinery-extractions/` and `schema_snapshots/week5-ledger-events/`

### 2. Run validation

```bash
python contracts/runner.py --contract generated_contracts/week3_extractions.yaml --data outputs/week3/extractions.jsonl
python contracts/runner.py --contract generated_contracts/week5_events.yaml --data outputs/week5/events.jsonl
```

Expected outputs:

- runner JSON in `validation_reports/`
- validation reports may include real `FAIL` rows from the provided datasets; these are used later by the attributor

### 3. Attribute violations

```bash
python contracts/attributor.py --violation validation_reports/week3_extractions_runner_report.json --output violation_log/violations.jsonl
python contracts/attributor.py --violation validation_reports/week5_events_runner_report.json --output violation_log/violations.jsonl
```

Expected output:

- appended JSONL records in `violation_log/violations.jsonl`
- each record contains `violation_id`, `check_id`, `blame_chain`, and `blast_radius`

The repository already includes multiple real attributed violations plus one documented injected record for evaluator inspection.

### 4. Analyze schema evolution

```bash
python contracts/schema_analyzer.py --contract-id week7-breaking-demo --output validation_reports/schema_evolution_week7_breaking_demo.json
```

Expected outputs:

- `validation_reports/schema_evolution_week7_breaking_demo.json`
- summary compatibility verdict of `BREAKING`
- at least one generated migration impact report in `validation_reports/`

This demo fixture is intentional so the evaluator can verify breaking-change classification without modifying the live contracts.

### 5. Run AI contract extensions

```bash
python contracts/ai_extensions.py run-all --week3-input outputs/week3/extractions.jsonl --week2-input outputs/week2/verdicts.jsonl --output-dir validation_reports
```

Expected outputs:

- `validation_reports/embedding_drift.json`
- `validation_reports/week3_prompt_validation.json`
- `validation_reports/week2_verdict_violation_rate.json`
- `validation_reports/ai_extensions.json`

### 6. Generate the enforcer report

```bash
python contracts/report_generator.py --output enforcer_report/report_$(date +%Y%m%d).pdf
```

Expected outputs:

- `enforcer_report/report_data.json`
- a PDF report in `enforcer_report/`
- `report_data.json` includes `data_health_score.score` between `0` and `100`

## Important repository artifacts

- `schema_snapshots/` contains at least two timestamped snapshots per contract directory used by the analyzer
- `violation_log/violations.jsonl` contains real attributed violations and a documented injected record
- `enforcer_report/report_data.json` is machine-generated
- `DOMAIN_NOTES.md` documents the Week 7 contract domain and architecture decisions

## Notes for evaluators

- The Week 3 and Week 5 datasets intentionally retain some real quality issues so the validation and attribution flow produces meaningful evidence.
- The breaking schema demo under `schema_snapshots/week7-breaking-demo/` exists only to prove that `contracts/schema_analyzer.py` classifies a breaking change and emits a migration impact report.
- If OpenAI credentials are available, the AI extension commands refresh live embedding and output-schema checks; otherwise the existing checked-in validation artifacts remain available for inspection.
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
