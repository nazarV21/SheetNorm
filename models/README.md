# Models

Папка для локальных GGUF-моделей.

По умолчанию приложение ищет модель по пути:

```text
models/qwen2.5-coder-7b-instruct-q4_k_m.gguf
```

На Windows модель можно скачать одной командой из корня проекта:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/download_model.ps1
```

Без модели приложение продолжает работать в fallback-режиме на встроенных правилах и обучающих примерах.
