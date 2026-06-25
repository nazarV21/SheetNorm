from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator


class CalculatedColumnRule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    expression: str = Field(min_length=1)

    @model_validator(mode="before")
    @classmethod
    def accept_legacy_expression_aliases(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        normalized = dict(value)
        if "expression" not in normalized:
            if "expr" in normalized:
                normalized["expression"] = normalized.pop("expr")
            elif "formula" in normalized:
                normalized["expression"] = normalized.pop("formula")
        normalized.pop("expr", None)
        normalized.pop("formula", None)
        return normalized


class DeclarativeRuleSchema(BaseModel):
    model_config = ConfigDict(extra="allow")

    table_type: str | None = None
    calculated: list[CalculatedColumnRule] = Field(default_factory=list)

def normalize_declarative_rule(rule: dict[str, Any] | None) -> dict[str, Any]:
    schema = DeclarativeRuleSchema.model_validate(rule or {})
    normalized = dict(rule or {})
    normalized["calculated"] = [item.model_dump() for item in schema.calculated]
    if schema.table_type is not None:
        normalized["table_type"] = schema.table_type
    return normalized


def rule_validation_errors(rule: dict[str, Any] | None) -> list[str]:
    try:
        normalize_declarative_rule(rule)
    except ValidationError as exc:
        return [f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}" for error in exc.errors()]
    return []
