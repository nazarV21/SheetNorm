from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pandas as pd
from flask import current_app

from .validator import validate_pandas_script


class ScriptExecutionError(Exception):
    pass


@dataclass
class ScriptRunner:
    max_rows: int = 1_000_000
    max_columns: int = 500

    @classmethod
    def from_app_config(cls) -> "ScriptRunner":
        return cls(
            max_rows=current_app.config.get("SCRIPT_MAX_OUTPUT_ROWS", 1_000_000),
            max_columns=current_app.config.get("SCRIPT_MAX_OUTPUT_COLUMNS", 500),
        )

    def run(self, code: str, df: pd.DataFrame) -> pd.DataFrame:
        validation = validate_pandas_script(
            code,
            max_code_length=current_app.config.get("SCRIPT_MAX_CODE_LENGTH", 30000),
        )
        if not validation.valid:
            raise ScriptExecutionError("; ".join(validation.errors))

        namespace: dict[str, Any] = {
            "__builtins__": {
                "len": len,
                "range": range,
                "min": min,
                "max": max,
                "sum": sum,
                "str": str,
                "int": int,
                "float": float,
                "bool": bool,
                "list": list,
                "dict": dict,
                "set": set,
                "tuple": tuple,
                "enumerate": enumerate,
                "zip": zip,
            },
            "pd": pd,
        }
        exec(compile(code, "<sheetnorm-script>", "exec"), namespace, namespace)
        transform = namespace.get("transform")
        if not callable(transform):
            raise ScriptExecutionError("Script must define callable transform(df).")
        result = transform(df.copy())
        if not isinstance(result, pd.DataFrame):
            raise ScriptExecutionError("transform(df) must return a pandas DataFrame.")
        if result.empty:
            raise ScriptExecutionError("Script output must not be empty.")
        if isinstance(result.index, pd.MultiIndex) or isinstance(result.columns, pd.MultiIndex):
            raise ScriptExecutionError("Script output must not use a MultiIndex.")
        if len(result) > self.max_rows:
            raise ScriptExecutionError("Script output exceeds maximum row count.")
        if len(result.columns) > self.max_columns:
            raise ScriptExecutionError("Script output exceeds maximum column count.")
        if len(set(map(str, result.columns))) != len(result.columns):
            raise ScriptExecutionError("Script output contains duplicate columns.")
        return result
