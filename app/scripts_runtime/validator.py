from __future__ import annotations

import ast
from dataclasses import dataclass


FORBIDDEN_IMPORTS = {
    "os",
    "sys",
    "subprocess",
    "socket",
    "pathlib",
    "shutil",
    "requests",
    "urllib",
    "builtins",
    "importlib",
}
FORBIDDEN_CALLS = {
    "open",
    "eval",
    "exec",
    "compile",
    "__import__",
    "input",
    "globals",
    "locals",
    "vars",
    "getattr",
    "setattr",
    "delattr",
}


@dataclass(frozen=True)
class ScriptValidationResult:
    valid: bool
    errors: list[str]
    warnings: list[str]

    def as_dict(self) -> dict:
        return {"valid": self.valid, "errors": self.errors, "warnings": self.warnings}


def validate_pandas_script(code: str, *, max_code_length: int = 30000) -> ScriptValidationResult:
    errors: list[str] = []
    warnings: list[str] = []
    if len(code) > max_code_length:
        errors.append("Script exceeds configured maximum length.")
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        return ScriptValidationResult(False, [f"Syntax error at line {exc.lineno}: {exc.msg}"], warnings)

    transform_defs = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "transform"]
    if not transform_defs:
        errors.append("Script must define transform(df).")
    else:
        transform = transform_defs[0]
        if len(transform.args.args) != 1 or transform.args.vararg or transform.args.kwarg:
            errors.append("transform must accept exactly one positional argument: df.")

    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [alias.name.split(".")[0] for alias in getattr(node, "names", [])]
            if isinstance(node, ast.ImportFrom) and node.module:
                names.append(node.module.split(".")[0])
            blocked = sorted(set(names) & FORBIDDEN_IMPORTS)
            if blocked:
                errors.append(f"Forbidden import: {', '.join(blocked)}.")
        if isinstance(node, ast.Call):
            call_name = _call_name(node.func)
            if call_name in FORBIDDEN_CALLS:
                errors.append(f"Forbidden call: {call_name}().")
        if isinstance(node, ast.Attribute) and node.attr.startswith("__"):
            errors.append("Dunder attribute access is forbidden.")
        if isinstance(node, ast.Name) and node.id.startswith("__"):
            errors.append("Dunder name access is forbidden.")
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and "__" in node.value:
            errors.append("Dunder string access is forbidden.")

    return ScriptValidationResult(not errors, sorted(set(errors)), warnings)


def _call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""
