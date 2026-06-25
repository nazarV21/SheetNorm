# Database Schema

SheetNorm now has a PostgreSQL-ready ORM layer under `app/db/models`.

Primary entities:

- `User`: email, display name, password hash, active/superuser flags, login timestamps.
- `Workspace`: tenant boundary with a unique slug and JSON settings.
- `WorkspaceMember`: workspace role assignment: `admin`, `editor`, `viewer`.
- `TransformationTemplate`: stable template identity for declarative rules and pandas scripts.
- `TemplateVersion`: immutable version payload with `rule_json` or `script_code`, validation state, approval fields and checksum.
- `ProcessingJob`: job lifecycle, progress, queue id, source/result artifact links and failure details.
- `JobEvent`: append-only job status and progress timeline.
- `Artifact`: file metadata and storage key. File bytes stay in storage, not in PostgreSQL.
- `QualityReport`: normalized metrics plus flexible metrics JSON.
- `InstructionFeedback`: accepted/rejected instruction improvements.
- `TrainingExample`: source/target artifact pair metadata for future tuning.
- `AuditLog`: security and product action audit trail.

Indexes cover job workspace/status/created/template lookups, template version status, artifacts by workspace and sha256, audit workspace/time, user email and workspace slug.

For unit tests, the same models run on SQLite. PostgreSQL remains the production target and should be validated with the Docker Compose profile.

