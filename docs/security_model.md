# Security Model

Authentication uses Flask-Login and hashed passwords. Roles are stored per workspace:

- `admin`: workspace, users, templates, execution and audit.
- `editor`: upload, draft template versions, preview and run jobs.
- `viewer`: read templates/jobs and download allowed results.

The current implementation introduces the data model and CLI admin creation. Route-level enforcement should be expanded as UI auth pages are completed.

Files are stored through storage keys. `LocalStorageBackend` prevents path traversal and keeps bytes outside PostgreSQL.

Script security:

- AI code requires validation and preview.
- Validation is not a complete sandbox.
- Production must execute scripts outside the Flask web process.
- Full file contents, secrets, DB URLs and script contexts should not be logged.

Audit events should be recorded for login, template/version creation, script changes, approval, disable, job run, cancellation, file deletion, result download and legacy import.

