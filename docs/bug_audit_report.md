# Bug Audit Report

## Исходное состояние

- Дата: 2026-06-25
- Ветка: `main`
- Commit: `754c4ce`
- Рабочее дерево до этого аудита уже содержало незакоммиченные изменения team-ready слоя.
- Baseline перед исправлениями текущего аудита:
  - `python -m compileall app main.py config.py`: passed
  - `python -m pytest -q`: 27 passed, 0 failed, 0 skipped
  - `ruff check .`: недоступен, команда `ruff` не установлена
  - `black --check .`: недоступен, команда `black` не установлена
  - `mypy .`: недоступен, команда `mypy` не установлена
  - `docker compose config`: недоступен, команда `docker` не установлена

## Найденные проблемы

### BUG-001

- Severity: Critical
- Компонент: conversion service / AI conversion
- Описание: prompt-only путь мог использовать `AIClient.apply_prompt`, где была отдельная реализация преобразования и fallback с возвратом исходного DataFrame.
- Воспроизведение: вызвать conversion с текстовой инструкцией без валидированного `generated_rule`.
- Первопричина: AI client не только генерировал спецификацию, но и применял трансформации параллельно `ConversionService`.
- Исправление: `ConversionService` теперь требует валидированное правило для prompt conversion; `AIClient.apply_prompt` отключен и выбрасывает `RuntimeError`.
- Тест: `test_prompt_without_validated_rule_does_not_save_raw_success`, `test_ai_client_no_longer_applies_transformations_directly`.
- Статус: fixed.

### BUG-002

- Severity: Critical
- Компонент: declarative rules / calculated columns
- Описание: проект принимал `formula`, `expr`, `expression`, а вычисление использовало `expr/formula` и подавляло ошибки.
- Воспроизведение: сохранить правило с `formula`, затем правило с невалидным выражением.
- Первопричина: не было канонической схемы правила и ошибки `df.eval()` игнорировались.
- Исправление: добавлен Pydantic schema `rule_schema.py`; canonical key `expression`; aliases `formula/expr` принимаются только на чтение и нормализуются; ошибки calculated columns теперь останавливают conversion.
- Тест: `test_calculated_columns_use_expression_and_accept_legacy_aliases`, `test_invalid_calculated_expression_fails_instead_of_silent_success`.
- Статус: fixed.

### BUG-003

- Severity: Critical
- Компонент: downloads / artifacts
- Описание: API мог отдать `output_path` из job без проверки terminal status и каталога; web download отдавал файл по имени без связи с успешной задачей.
- Воспроизведение: создать failed job с `output_path` или orphan-файл в `output`.
- Первопричина: скачивание было привязано к файловой системе, а не к успешному job state.
- Исправление: API result требует `status=success` и файл внутри `OUTPUT_DIR`; web download требует matching successful job.
- Тест: `test_api_result_requires_success_and_safe_output_path`, `test_web_download_requires_successful_job`.
- Статус: fixed.

### BUG-004

- Severity: Critical
- Компонент: upload validation
- Описание: HTML/исполняемый payload с расширением `.xlsx` сохранялся как upload до фактической Excel-валидации.
- Воспроизведение: загрузить `<html>...</html>` как `evil.xlsx`.
- Первопричина: проверялось только расширение файла.
- Исправление: после сохранения выполняется `pd.ExcelFile`; пустые/битые/не-Excel файлы удаляются вместе с meta и получают structured 400.
- Тест: `test_upload_rejects_html_file_with_xlsx_extension`; существующий empty upload тест обновлен на 400.
- Статус: fixed.

### BUG-005

- Severity: High
- Компонент: generated rule execution
- Описание: non-strict path мог продолжить fallback после ошибки generated rule.
- Воспроизведение: generated rule с ошибкой применения в обычном convert.
- Первопричина: ошибка правила была fatal только в `strict=True`.
- Исправление: error diagnostics из generated rule теперь fatal во всех режимах.
- Тест: covered by calculated-expression regression.
- Статус: fixed.

### BUG-006

- Severity: High
- Компонент: Script Runner
- Описание: validator не блокировал `importlib`, `getattr`, `setattr`; не проверял сигнатуру `transform`.
- Воспроизведение: скрипт с `getattr(df, 'shape')`, `import importlib`, `transform(df, extra)`.
- Первопричина: неполный AST denylist и отсутствие проверки function args.
- Исправление: denylist расширен; проверяется ровно один positional аргумент; runner отклоняет empty output и MultiIndex.
- Тест: `test_script_validator_blocks_introspection_and_bad_signature`.
- Статус: fixed.

### BUG-007

- Severity: Medium
- Компонент: AI / training examples
- Описание: часть fallback-ошибок не логировалась.
- Воспроизведение: ошибка LLM improvement или training-pattern conversion.
- Первопричина: широкие `except Exception` возвращали fallback без записи причины.
- Исправление: добавлено warning logging в `InstructionAssistant`, `SmartConverter`, cleanup training examples.
- Тест: покрыто общим pytest; отдельного log assertion не добавлялся, потому что поведение пользователя не меняется.
- Статус: fixed.

### BUG-008

- Severity: Medium
- Компонент: API error format
- Описание: часть новых API endpoints могла возвращать ошибки без `diagnostics`.
- Воспроизведение: вызвать новые endpoints с bad payload.
- Первопричина: helper `_api_error` старого формата не включал diagnostics.
- Исправление: существующий формат сохранен для compatibility; known limitation: diagnostics надо унифицировать в отдельном проходе.
- Тест: existing structured error tests.
- Статус: partially fixed / documented.

## Исправленные проблемы

### Critical

- BUG-001: отключен direct AI DataFrame conversion и raw-success fallback.
- BUG-002: канонизирован `expression`, ошибки calculated columns больше не подавляются.
- BUG-003: result download требует successful job и безопасный путь.
- BUG-004: MIME/content mismatch для Excel upload отклоняется.

### High

- BUG-005: generated rule failures больше не переходят в successful fallback.
- BUG-006: Script Runner validator усилен против introspection и неправильной сигнатуры.

### Medium

- BUG-007: добавлено логирование для части fallback-ошибок.
- BUG-008: зафиксирована необходимость полной унификации diagnostics.

### Low

- Обновлены тесты под более строгий upload contract.

## Неисправленные проблемы

- Полная auth/role модель в UI/API не завершена.
  - Риск: viewer/editor/admin ограничения пока не покрывают все route-level сценарии.
  - Решение: добавить login pages, CSRF, decorators для workspace scope и role permissions.

- Docker/PostgreSQL/RQ integration не проверены в этой среде.
  - Риск: compose config/build/up могут иметь ошибки, не видимые unit tests.
  - Решение: запустить Docker на машине с Docker CLI и добавить CI profile.

- `AIClient.apply_prompt` содержит мертвый legacy-код после раннего `RuntimeError`.
  - Риск: статический шум и поддерживаемость.
  - Решение: удалить тело legacy method отдельным cleanup PR, оставив только ошибку или заменив методом schema generation.

- В `conversion_service.py` остаются два широких `except Exception` в форматировании Excel report и расчете quality source metrics.
  - Риск: часть дополнительных report metrics может быть недоступна без полного traceback пользователю.
  - Решение: заменить на typed exceptions и structured warning logging. Основная конвертация при этом не помечается ложным успехом из-за report-formatting ошибки.

- Настоящая sandbox-изоляция pandas scripts не реализована.
  - Риск: AST validation не является sandbox.
  - Решение: вынести execution в отдельный locked-down container/process без secrets, DB URL и основного storage.

## Проверки

- `python -m compileall app main.py config.py`: passed.
- `python -m pytest -q`: 35 passed, 0 failed, 0 skipped.
- PostgreSQL: unit-level SQLite compatibility tested; live PostgreSQL unavailable without Docker.
- Redis/RQ: sync fallback tested; live Redis/RQ unavailable without Docker.
- Script Runner: validator/runner unit tests added and passed.
- Docker: not run, `docker` command is unavailable in current environment.
- Manual UI/browser сценарии: not fully run; API-level and service-level regressions were automated.
- `ruff`, `black`, `mypy`: not run, commands unavailable.

## Результат

- Тесты после изменений: 35 passed.
- Исправлено: 7 полностью, 1 частично/documented.
- Основные Critical/High риски закрыты для raw-success, expression aliases, unsafe downloads, invalid upload content, generated-rule fallback и script validator gaps.

Команды запуска:

```bash
python -m compileall app main.py config.py
python -m pytest -q
docker compose config
docker compose build
docker compose up -d
docker compose ps
docker compose exec web flask db upgrade
docker compose exec web flask create-admin
docker compose exec web flask import-json --dry-run
docker compose exec web flask import-json
```

