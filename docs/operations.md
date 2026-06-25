# Operations

## Local Docker

```bash
docker compose config
docker compose build
docker compose up -d
docker compose ps
docker compose exec web flask db upgrade
docker compose exec web flask create-admin
```

Services:

- `web`: Flask/Jinja/API application.
- `worker`: RQ worker for background conversion.
- `script-runner`: separate process boundary for future hardened script execution.
- `postgres`: primary data store.
- `redis`: RQ broker.

## Health

- `/health`: legacy liveness response.
- `/health/live`: process liveness.
- `/health/ready`: database, storage and Redis readiness when `ASYNC_MODE=rq`.

## Backup

Use PostgreSQL native tooling:

```bash
pg_dump "$DATABASE_URL" > sheetnorm.sql
psql "$DATABASE_URL" < sheetnorm.sql
```

Also back up:

- storage volume;
- `.env` or secret manager configuration;
- model files if local GGUF inference is used.

Do not replace `pg_dump` with application-level JSON export for production backup.

