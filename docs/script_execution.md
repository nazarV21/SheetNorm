# Script Execution

SheetNorm supports two execution modes:

- `declarative_rule`: existing JSON rule engine.
- `pandas_script`: approved Python code with `transform(df) -> pandas.DataFrame`.

AI-generated code is never trusted as-is. It must pass validation, produce a preview, and be approved as a template version before reuse.

Current local runtime:

- AST validation blocks dangerous imports and calls such as `os`, `subprocess`, `socket`, `open`, `eval`, `exec` and dunder access.
- `ScriptRunner` executes with a restricted builtins dictionary.
- Output must be a DataFrame, have unique columns, and stay inside configured row/column limits.

Important limitation: AST validation is not a full sandbox. Production should keep script execution outside the Flask web process. The Docker Compose stack includes a separate `script-runner` service placeholder so this boundary can be hardened without changing template storage.

Only approved versions are intended for repeated processing. Draft, invalid, rejected or disabled templates must not be used for batch conversion.

