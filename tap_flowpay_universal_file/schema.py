"""Record schemas: the built-in GoKasa schema, and checks driven by any configured JSON Schema.

The configured schema is both the stream schema and the contract each file is checked
against. Downstream consumers interpret the fields; the tap does not.
"""

from __future__ import annotations

import copy
import datetime
import json
import math
import re
import typing as t

from jsonschema import Draft7Validator, FormatChecker, validators
from jsonschema.exceptions import SchemaError
from singer_sdk import typing as th

PAYMENT = th.ObjectType(
    th.Property("paymentMethod", th.StringType, description="CASH, CREDIT_CARD or BANK_TRANSFER"),
    th.Property("amount", th.IntegerType, required=True, description="Haléř; negative for a refund"),
    th.Property("currency", th.StringType, required=True),
    th.Property("acceptedAmount", th.IntegerType),
    th.Property("acceptedCurrency", th.StringType),
)

# One GoKasa transaction, exactly as GoKasa sends it.
GOKASA_SCHEMA = th.PropertiesList(
    th.Property("transactionId", th.StringType, required=True, description="Merge key"),
    th.Property("payments", th.ArrayType(PAYMENT), required=True, description="More than one for a split bill"),
    th.Property("date", th.DateType, required=True),
    th.Property("checkoutId", th.StringType),
    th.Property("cancel", th.IntegerType, description="On a sale: the checkoutId of the refund that cancels it"),
).to_dict()

SCALAR_TYPES = frozenset({"string", "integer", "number", "boolean"})
SUPPORTED_TYPES = SCALAR_TYPES | {"array", "object"}

_DATE_TIME = re.compile(
    r"(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::(\d{2})(?:\.\d{1,9})?)?(Z|[+-](\d{2}):?(\d{2}))?",
    re.ASCII,
)


def is_date_time(value: t.Any) -> bool:
    """ISO 8601 date and time, `T` or space separated, with or without a UTC offset.

    Partner exports often carry local times without an offset. The value is only
    checked, never rewritten. Built in, because jsonschema skips date-time checks
    unless an optional package is installed.
    """
    if not isinstance(value, str):
        return True  # formats apply to strings; the type check reports anything else
    match = _DATE_TIME.fullmatch(value)
    if match is None:
        return False
    year, month, day, hour, minute, second = (int(part or 0) for part in match.groups()[:6])
    try:
        datetime.datetime(year, month, day, hour, minute, second)
    except ValueError:
        return False
    return match[8] is None or (int(match[8]) <= 23 and int(match[9]) <= 59)  # noqa: PLR2004


FORMATS = FormatChecker()
FORMATS.checks("date-time")(is_date_time)


def _finite_number(_checker: t.Any, value: t.Any) -> bool:
    """JSON numbers must be finite; checking huge integers must not coerce them to float."""
    return Draft7Validator.TYPE_CHECKER.is_type(value, "number") and (
        not isinstance(value, float) or math.isfinite(value)
    )


RecordValidator = validators.extend(
    Draft7Validator, type_checker=Draft7Validator.TYPE_CHECKER.redefine("number", _finite_number),
)


def types(node: t.Mapping) -> set[str]:
    """The non-null JSON types a schema node allows."""
    declared = node.get("type", [])
    declared = [declared] if isinstance(declared, str) else list(declared)
    return {kind for kind in declared if kind != "null"}


def kind(node: t.Mapping) -> str | None:
    """The one non-null type of a validated schema node."""
    return next(iter(types(node)), None)


def load_schema(value: t.Any) -> dict:
    """A configured record schema, given as an object or as JSON text."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            msg = "schema is not valid JSON"
            raise ValueError(msg) from None
    if not isinstance(value, dict):
        msg = "schema must be a JSON object"
        raise ValueError(msg)
    try:
        Draft7Validator.check_schema(value)
    except SchemaError as exc:
        msg = f"schema is not a valid JSON Schema: {exc.message}"
        raise ValueError(msg) from None
    if (
        value.get("type") not in ("object", ["object"])
        or not isinstance(value.get("properties"), dict) or not value["properties"]
    ):
        msg = "schema must be an object schema with at least one property"
        raise ValueError(msg)
    _check_properties(value["properties"], "")
    return value


def _check_properties(properties: t.Mapping, prefix: str) -> None:
    for name, node in properties.items():
        _check_node(node, f"{prefix}{name}")


def _check_node(node: t.Any, path: str) -> None:
    found = types(node) if isinstance(node, dict) else set()
    if len(found) != 1 or not found <= SUPPORTED_TYPES:
        msg = (
            f"schema field {path} must have exactly one type of "
            f"{', '.join(sorted(SUPPORTED_TYPES))} (null may be added)"
        )
        raise ValueError(msg)
    if found == {"object"} and isinstance(node.get("properties"), dict):
        _check_properties(node["properties"], f"{path}.")
    if found == {"array"}:
        if not isinstance(node.get("items"), dict):
            raise ValueError(f"schema field {path} must declare a single items schema")
        _check_node(node["items"], f"{path}[]")


def strip_unknown(record: t.Any, schema: t.Mapping) -> tuple[t.Any, set[str]]:
    """Remove fields the schema does not declare, at any depth; return their paths."""
    unknown: set[str] = set()
    return _strip(record, schema, "", unknown), unknown


def _strip(value: t.Any, node: t.Mapping, path: str, unknown: set[str]) -> t.Any:
    properties = node.get("properties", {})
    if isinstance(value, dict) and kind(node) == "object":
        kept = {}
        for name, item in value.items():
            child = f"{path}.{name}" if path else name
            if name in properties:
                kept[name] = _strip(item, properties[name], child, unknown)
            else:
                unknown.add(child)
        return kept
    items = node.get("items")
    if isinstance(value, list) and isinstance(items, dict):
        return [_strip(item, items, f"{path}[]", unknown) for item in value]
    return value


def _drop_required(node: t.Any) -> None:
    if isinstance(node, dict):
        node.pop("required", None)
        for child in (node.get("properties") or {}).values():
            _drop_required(child)
        _drop_required(node.get("items"))


class RecordChecker:
    """Checks records against the schema. Key fields are always required and non-empty.

    With require_all=False, only the key fields are required: a history written before
    a field became required in the configuration must still be readable.
    """

    def __init__(self, schema: t.Mapping, keys: t.Sequence[str], *, require_all: bool = True) -> None:
        """Build the validator."""
        contract = copy.deepcopy(dict(schema))
        if not require_all:
            _drop_required(contract)
        required = list(contract.get("required", []))
        contract["required"] = required + [key for key in keys if key not in required]
        for key in keys:
            node = contract["properties"][key]
            if "string" in types(node):
                node["minLength"] = max(1, node.get("minLength", 0))
        self._validator = RecordValidator(contract, format_checker=FORMATS)

    def problems(self, record: t.Any) -> list[str]:
        """What breaks the contract in one record, as 'path: message'. Empty when it fits."""
        found = []
        for error in sorted(self._validator.iter_errors(record), key=lambda e: e.json_path):
            path = error.json_path[2:]  # "$.payments[0].amount" -> "payments[0].amount"
            found.append(f"{path}: {error.message}" if path else error.message)
        return found
