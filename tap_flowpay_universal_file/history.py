"""Find and read back the previous complete history, as the pipeline wrote it from the tap's output."""

from __future__ import annotations

import json
import re
import typing as t
from decimal import Decimal, InvalidOperation

from tap_flowpay_universal_file.records import csv_rows, lines, report
from tap_flowpay_universal_file.schema import RecordChecker, kind

if t.TYPE_CHECKING:
    from tap_flowpay_universal_file.storage import BlobInfo

MIDNIGHT = re.compile(r"^(\d{4}-\d{2}-\d{2})T00:00:00(\.0+)?(Z|[+-]00:?00)?$")
_INTEGER = re.compile(r"[+-]?\d+", re.ASCII)


class InvalidHistory(ValueError):
    """The previous history file cannot be read back. The job fails rather than lose records."""


def latest_history(blobs: t.Iterable[BlobInfo], *, filenames: tuple[str, ...]) -> BlobInfo | None:
    """Legacy lookup by modification time; this cannot establish job success."""
    candidates = [b for b in blobs if b.name.rsplit("/", 1)[-1] in filenames]
    return max(candidates, key=lambda b: (b.last_modified, b.name), default=None)


def read_history(content: bytes, source: str, schema: t.Mapping, checker: RecordChecker) -> list[dict]:
    """The records in a previous history file, as the tap emitted them.

    The pipeline that runs the tap writes the file (with pandas, for example), so it
    can differ from what the tap emitted: nested values as JSON text, null for a missing
    field, numbers and booleans as text, a whole number as a float, a numeric id as a
    number, a date as midnight, extra columns of its own. Those are undone by the
    configured types; anything else that does not fit fails the job.
    """
    rows = csv_rows(content, source, ",", InvalidHistory) if source.endswith(".csv") else _jsonl_rows(content, source)
    records: list[dict] = []
    errors: list[str] = []
    for line_no, row in rows:
        try:
            record = _restore_record(row, schema)
        except (TypeError, ValueError) as exc:
            errors.append(f"line {line_no}: {exc}")
            continue
        found = checker.problems(record)
        errors.extend(f"line {line_no}: {problem}" for problem in found)
        if not found:
            records.append(record)
    if errors:
        raise InvalidHistory(report(source, errors))
    if not records:
        msg = f"{source}: no records; refusing to reset history from an empty file"
        raise InvalidHistory(msg)
    return records


def _jsonl_rows(content: bytes, source: str) -> t.Iterator[tuple[int, t.Any]]:
    for line_no, line in lines(content):
        try:
            yield line_no, json.loads(line)
        except json.JSONDecodeError as exc:
            msg = f"{source} line {line_no}: not valid JSON ({exc.msg})"
            raise InvalidHistory(msg) from exc


def _restore_record(row: t.Any, schema: t.Mapping) -> t.Any:
    if not isinstance(row, dict):
        return row  # the schema check reports it
    properties = schema["properties"]
    return {
        field: _restore(value, properties[field])
        for field, value in row.items()
        if field in properties and value is not None and value != ""
    }


def _restore(value: t.Any, node: t.Mapping) -> t.Any:  # noqa: PLR0911
    if value is None:
        return None
    json_type = kind(node)
    if json_type in ("array", "object") and isinstance(value, str):
        value = json.loads(value)
    if json_type == "object" and isinstance(value, dict):
        properties = node.get("properties", {})
        return {name: _restore(item, properties[name]) for name, item in value.items() if name in properties}
    if json_type == "array" and isinstance(value, list) and isinstance(node.get("items"), dict):
        return [_restore(item, node["items"]) for item in value]
    if json_type == "integer":
        return _whole_number(value)
    if json_type == "number":
        return _number(value)
    if json_type == "boolean":
        return _boolean(value)
    if json_type == "string":
        if node.get("format") == "date" and isinstance(value, str) and MIDNIGHT.match(value):
            return value[:10]
        return _text(value)
    return value


def _number(value: t.Any) -> t.Any:
    """'12.5' -> 12.5, '12' -> 12. Anything else is left for the schema check."""
    if isinstance(value, str) and not isinstance(value, bool):
        try:
            return int(value) if _INTEGER.fullmatch(value) else float(value)
        except ValueError:
            return value
    return value


def _whole_number(value: t.Any) -> t.Any:
    """12345.0 or "12345" -> 12345. Anything else is left for the schema check."""
    if isinstance(value, str):
        try:
            number = Decimal(value)
        except InvalidOperation:
            return value
        # A float would round large IDs and could round a fractional value to an integer.
        if number.is_finite() and number == number.to_integral_value():
            return int(number)
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def _boolean(value: t.Any) -> t.Any:
    """'True', 'false', '1', 1.0 and similar -> bool. Anything else is left for the schema check."""
    if isinstance(value, str) and value.lower() in ("true", "1", "false", "0"):
        return value.lower() in ("true", "1")
    if not isinstance(value, bool) and isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    return value


def _text(value: t.Any) -> t.Any:
    """An id that pandas read as a number, back to text."""
    if isinstance(value, bool):
        return value
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, int):
        return str(value)
    return value
