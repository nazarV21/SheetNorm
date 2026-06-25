# Models

Поместите сюда локальные GGUF-модели. Файлы могут лежать во вложенных каталогах.

```text
models/
  qwen2.5-coder-3b-instruct-q4_k_m.gguf
  qwen2.5-coder-7b-instruct-q4_k_m.gguf
```

После запуска откройте `/settings`, обновите список, выберите профиль, протестируйте и активируйте модель.

Можно добавить sidecar JSON с тем же именем, например `qwen2.5-coder-7b-instruct-q4_k_m.json`:

```json
{
  "display_name": "Qwen 2.5 Coder 7B Q4_K_M",
  "family": "Qwen 2.5 Coder",
  "parameters_b": 7,
  "quantization": "Q4_K_M",
  "minimum_ram_gb": 8,
  "recommended_ram_gb": 12,
  "recommended_context": 4096
}
```

GGUF-файлы исключены из Git и Docker image. Без модели SheetNorm работает в fallback-режиме.
