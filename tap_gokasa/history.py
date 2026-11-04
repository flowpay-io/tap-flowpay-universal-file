"""Find and read back the previous complete history, as the pipeline wrote it from the tap's output."""

from __future__ import annotations

import csv
import io
import json
import re
import typing as t

from tap_gokasa.schema import FIELDS, problems
from tap_gokasa.transactions import lines, report

if t.TYPE_CHECKING:
    from tap_gokasa.storage import BlobInfo

# The GoKasa flow should export the stream as JSONL; a CSV export is read as well.
HISTORY_FILES = ("transactions.jsonl", "transactions.csv")
TEXT_FIELDS = ("transactionId", "checkoutId")
MIDNIGHT = re.compile(r"^(\d{4}-\d{2}-\d{2})T00:00:00(\.0+)?(Z|[+-]00:?00)?$")


class InvalidHistory(ValueError):
    """The previous history file cannot be read back. The job fails rather than lose transactions."""


def latest_history(blobs: t.Iterable[BlobInfo]) -> BlobInfo | None:
    """The newest history file. Job IDs are random, so order by lastModified."""
    candidates = [b for b in blobs if b.name.rsplit("/", 1)[-1] in HISTORY_FILES]
    return max(candidates, key=lambda b: (b.last_modified, b.name), default=None)


def parse_history(content: bytes, source: str) -> list[dict]:
    """The transactions in a previous history file, as the tap emitted them.

    The pipeline that runs the tap writes the file (with pandas, for example), so it
    can differ from what the tap emitted:
    payments as JSON text, null for a missing field, a whole number as a float,
    a numeric id as a number, a date as midnight, extra columns such as `tenant`.
    Those are undone; anything else that does not fit the schema fails the job.
    """
    rows = _csv_rows(content, source) if source.endswith(".csv") else _jsonl_rows(content, source)
    records: list[dict] = []
    errors: list[str] = []
    for line_no, row in rows:
        try:
            record = _restore(row)
        except (TypeError, ValueError) as exc:
            errors.append(f"line {line_no}: {exc}")
            continue
        found = problems(record)
        errors.extend(f"line {line_no}: {problem}" for problem in found)
        if not found:
            records.append(record)
    if errors:
        raise InvalidHistory(report(source, errors))
    return records


def _jsonl_rows(content: bytes, source: str) -> t.Iterator[tuple[int, t.Any]]:
    for line_no, line in lines(content):
        try:
            yield line_no, json.loads(line)
        except json.JSONDecodeError as exc:
            msg = f"{source} line {line_no}: not valid JSON ({exc.msg})"
            raise InvalidHistory(msg) from exc


def _csv_rows(content: bytes, source: str) -> t.Iterator[tuple[int, dict]]:
    reader = csv.DictReader(io.StringIO(content.decode("utf-8-sig"), newline=""))
    for row in reader:
        if None in row:
            msg = f"{source} line {reader.line_num}: more columns than the header"
            raise InvalidHistory(msg)
        yield reader.line_num, row


def _restore(row: t.Any) -> t.Any:
    if not isinstance(row, dict):
        return row  # the schema check reports it
    record = {}
    for field, value in row.items():
        if field not in FIELDS or value is None or value == "":
            continue
        if field == "payments" and isinstance(value, str):
            value = json.loads(value)  # noqa: PLW2901
        elif field == "cancel":
            value = _whole_number(value)  # noqa: PLW2901
        elif field in TEXT_FIELDS:
            value = _text(value)  # noqa: PLW2901
        elif field == "date" and isinstance(value, str) and MIDNIGHT.match(value):
            value = value[:10]  # noqa: PLW2901
        record[field] = value
    for payment in record.get("payments") or []:
        for field in ("amount", "acceptedAmount"):
            if isinstance(payment, dict) and payment.get(field) is not None:
                payment[field] = _whole_number(payment[field])
    return record


def _whole_number(value: t.Any) -> t.Any:
    """12345.0 or "12345" -> 12345. Anything else is left for the schema check."""
    if isinstance(value, str):
        try:
            value = float(value)
        except ValueError:
            return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
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
