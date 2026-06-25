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


## Local background mode without Redis

For a local Windows/Linux pilot, use:

```env
ASYNC_MODE=thread
LOCAL_WORKER_THREADS=1
```

The browser request returns immediately and the conversion continues in a background thread, independently of the open browser tab. The task can be stopped from `/jobs`. One worker is recommended on machines with limited RAM. Thread mode does not survive a server restart; use Redis/RQ for durable production execution.

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



## Рабочие сессии AI-помощника

Загрузка файла в AI-помощнике сразу создаёт черновик задачи. SheetNorm автоматически сохраняет:

- исходный файл и выбранный лист;
- текущую пользовательскую инструкцию;
- результаты анализа структуры;
- уточнённую инструкцию;
- сформированное правило;
- исходный и итоговый preview;
- текущий шаг.

Сессию можно открыть через `/jobs` и продолжить после перехода на другую страницу. В локальном thread-режиме работа не зависит от вкладки браузера, но активная операция прервётся при остановке Flask-процесса. Сохранённый черновик останется доступным и может быть запущен повторно.
