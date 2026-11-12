"""Read delivered files and merge records on their key. Configured values pass through unchanged."""

from __future__ import annotations

import csv
import io
import json
import re
import typing as t
from collections import Counter

from tap_flowpay_universal_file.schema import RecordChecker, kind, strip_unknown

MAX_REPORTED = 20
_INTEGER = re.compile(r"[+-]?\d+", re.ASCII)
_NUMBER = re.compile(r"[+-]?(\d+(\.\d+)?|\.\d+)([eE][+-]?\d+)?", re.ASCII)


class InvalidDelivery(ValueError):
    """A delivered file that breaks the contract. The job fails rather than load it."""


class Delivery(t.NamedTuple):
    """The records of one file, and how many records had each field that is not configured."""

    records: list[dict]
    ignored: dict[str, int]


def lines(content: bytes) -> t.Iterator[tuple[int, str]]:
    """Non-blank lines with their numbers. Accepts a BOM and CRLF line ends."""
    for line_no, line in enumerate(content.decode("utf-8-sig").split("\n"), start=1):
        line = line.rstrip("\r")  # noqa: PLW2901
        if line.strip():
            yield line_no, line


def csv_rows(
    content: bytes, source: str, delimiter: str, error: type[ValueError],
) -> t.Iterator[tuple[int, dict[str, str | None]]]:
    """Rows of a CSV file with a header, as dicts with their line numbers."""
    reader = csv.DictReader(io.StringIO(content.decode("utf-8-sig"), newline=""), delimiter=delimiter)
    header = reader.fieldnames or []
    if len(set(header)) != len(header):
        msg = f"{source}: duplicate column names in the header"
        raise error(msg)
    for row in reader:
        if None in row:
            msg = f"{source} line {reader.line_num}: more columns than the header"
            raise error(msg)
        yield reader.line_num, row


def report(source: str, errors: list[str]) -> str:
    """One message for a failed file: the count and the first problems with line numbers."""
    shown = "\n".join(f"{source} {error}" for error in errors[:MAX_REPORTED])
    hidden = len(errors) - MAX_REPORTED
    more = f"\n... and {hidden} more" if hidden > 0 else ""
    return f"{source}: {len(errors)} problems\n{shown}{more}"


def read_jsonl(content: bytes, source: str, schema: t.Mapping, checker: RecordChecker) -> Delivery:
    """Every record of a JSONL file, checked before anything is returned."""
    def parsed() -> t.Iterator[tuple[int, t.Any, list[str]]]:
        for line_no, line in lines(content):
            try:
                yield line_no, json.loads(line), []
            except json.JSONDecodeError as exc:
                yield line_no, None, [f"not valid JSON ({exc.msg})"]

    return _collect(parsed(), source, schema, checker)


def read_csv(
    content: bytes, source: str, schema: t.Mapping, checker: RecordChecker, delimiter: str, decimal: str,
) -> Delivery:
    """Every row of a CSV file, converted by the configured types and checked."""
    properties = schema["properties"]

    def parsed() -> t.Iterator[tuple[int, t.Any, list[str]]]:
        for line_no, row in csv_rows(content, source, delimiter, InvalidDelivery):
            record, errors = {}, []
            for column, text in row.items():
                if text is None or text == "":
                    continue  # an empty cell is a missing value
                if column not in properties:
                    record[column] = text  # counted and removed with the other unknown fields
                    continue
                try:
                    record[column] = _convert(text, kind(properties[column]), decimal)
                except ValueError as exc:
                    errors.append(f"{column}: {text!r} is not {exc}")
            yield line_no, record, errors

    return _collect(parsed(), source, schema, checker)


def _convert(text: str, json_type: str | None, decimal: str) -> t.Any:
    if json_type == "integer":
        if not _INTEGER.fullmatch(text):
            raise ValueError("an integer")
        return int(text)
    if json_type == "number":
        # With a decimal comma, a dot could be a thousands separator: reject it rather than guess.
        normalized = text if decimal == "." else ("" if "." in text else text.replace(decimal, "."))
        if not _NUMBER.fullmatch(normalized):
            raise ValueError("a number")
        return int(normalized) if _INTEGER.fullmatch(normalized) else float(normalized)
    if json_type == "boolean":
        if text.lower() in ("true", "1"):
            return True
        if text.lower() in ("false", "0"):
            return False
        raise ValueError("a boolean")
    return text


def _collect(
    parsed: t.Iterable[tuple[int, t.Any, list[str]]], source: str, schema: t.Mapping, checker: RecordChecker,
) -> Delivery:
    records: list[dict] = []
    errors: list[str] = []
    ignored: Counter[str] = Counter()
    for line_no, raw, problems in parsed:
        if not problems:
            record, unknown = strip_unknown(raw, schema)
            ignored.update(unknown)
            problems = checker.problems(record)
        errors.extend(f"line {line_no}: {problem}" for problem in problems)
        if not problems:
            records.append(record)
    if errors:
        raise InvalidDelivery(report(source, errors))
    if not records:
        msg = f"{source}: no records; an empty delivery cannot produce a complete history"
        raise InvalidDelivery(msg)
    return Delivery(records, dict(sorted(ignored.items())))


def merge(
    *batches: t.Iterable[dict], key: t.Sequence[str], order: t.Sequence[str],
) -> tuple[list[dict], list[t.Any]]:
    """Merge batches of records on their key. A later batch wins.

    A null field counts as missing. Returns the merged records sorted by `order`, and
    the keys whose content changed between batches (e.g. a sale resent with `cancel`).
    A single-field key is reported as its value, a composite key as a tuple.
    """
    merged: dict[tuple, dict] = {}
    changed: list[t.Any] = []
    for batch in batches:
        for record in batch:
            current = {field: value for field, value in record.items() if value is not None}
            record_key = tuple(current[field] for field in key)
            previous = merged.get(record_key)
            if previous is not None and previous != current:
                changed.append(record_key[0] if len(key) == 1 else record_key)
            merged[record_key] = current

    def sort_key(record: dict) -> tuple:
        return tuple((record.get(field) is None, record.get(field)) for field in order)

    return sorted(merged.values(), key=sort_key), changed
