# Migration From JSON

Legacy files remain supported as import sources:

- `rules.json`
- `jobs.json`
- `training_examples.json`

Run migrations and import:

```bash
flask db upgrade
flask import-json --dry-run
flask import-json
```

The importer creates a default workspace when needed, converts legacy rules into `TransformationTemplate` plus approved `TemplateVersion`, copies jobs into `ProcessingJob`, and stores training records as `TrainingExample` metadata.

The import is idempotent for rule and job public ids where possible. Existing rows are skipped instead of duplicated.

JSON files should be kept as backup inputs during the transition. After import, production should use `DATA_STORE_BACKEND=database`.

