# Final Submission Checklist

Use this checklist before pushing the final Thursday submission.

## Required Artifacts

- [ ] `DOMAIN_NOTES.md` contains all five Phase 0 answers with evidence from your own systems.
- [ ] `DOMAIN_NOTES.md` is at least 800 words.
- [ ] `outputs/week3/extractions.jsonl` exists and contains at least 50 real records.
- [ ] `outputs/week5/events.jsonl` exists and contains at least 50 real records.
- [ ] If the source systems needed reshaping, `outputs/migrate/align_data.py` is present and the migrated JSONL outputs are committed.
- [ ] `generated_contracts/week3_extractions.yaml` exists.
- [ ] `generated_contracts/week3_extractions_dbt.yml` exists.
- [ ] `generated_contracts/week5_events.yaml` exists.
- [ ] `generated_contracts/week5_events_dbt.yml` exists.
- [ ] `validation_reports/` contains at least one real report generated from your own data.

## Evaluator Commands

Run these commands from the repository root.

```bash
python contracts/generator.py --source outputs/week3/extractions.jsonl --output generated_contracts/
python contracts/generator.py --source outputs/week5/events.jsonl --output generated_contracts/
python contracts/runner.py --contract generated_contracts/week3_extractions.yaml --data outputs/week3/extractions.jsonl
```

## Expected Outcomes

- [ ] The generator completes without errors using only `--source` and `--output`.
- [ ] The generator writes exact evaluator-friendly filenames under `generated_contracts/`.
- [ ] The runner writes a JSON report under `validation_reports/`.
- [ ] `schema_snapshots/baselines.json` is created on first validation run and reused on later runs.
- [ ] Drift checks can flag confidence scale problems with `WARNING` or `FAIL` / `CRITICAL` severity.

## Final Sanity Checks

- [ ] Commit the generated contracts, migrated outputs, baseline snapshot, and validation reports.
- [ ] Confirm no placeholder or fabricated examples are included.
- [ ] Open the generated YAML and JSON report files once to confirm they are readable and complete.
