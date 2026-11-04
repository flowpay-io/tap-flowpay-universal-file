"""Read GoKasa files and merge transactions on transactionId. Records pass through unchanged."""

from __future__ import annotations

import json
import typing as t

from tap_gokasa.schema import problems

MAX_REPORTED = 20


class InvalidDelivery(ValueError):
    """A GoKasa file that breaks the contract. The job fails rather than load it."""


def lines(content: bytes) -> t.Iterator[tuple[int, str]]:
    """Non-blank lines with their numbers. Accepts a BOM and CRLF line ends."""
    for line_no, line in enumerate(content.decode("utf-8-sig").split("\n"), start=1):
        line = line.rstrip("\r")  # noqa: PLW2901
        if line.strip():
            yield line_no, line


def report(source: str, errors: list[str]) -> str:
    """One message for a failed file: the count and the first problems with line numbers."""
    shown = "\n".join(f"{source} {error}" for error in errors[:MAX_REPORTED])
    hidden = len(errors) - MAX_REPORTED
    more = f"\n... and {hidden} more" if hidden > 0 else ""
    return f"{source}: {len(errors)} problems\n{shown}{more}"


def parse_delivery(content: bytes, source: str) -> list[dict]:
    """Every transaction in one GoKasa file, checked against the stream schema.

    The whole file is checked before anything is returned, so a bad file fails the
    job before the first record goes out.
    """
    records: list[dict] = []
    errors: list[str] = []
    for line_no, line in lines(content):
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            errors.append(f"line {line_no}: not valid JSON ({exc.msg})")
            continue
        found = problems(record)
        errors.extend(f"line {line_no}: {problem}" for problem in found)
        if not found:
            records.append(record)
    if errors:
        raise InvalidDelivery(report(source, errors))
    return records


def merge(*batches: t.Iterable[dict]) -> tuple[list[dict], list[str]]:
    """Merge batches of transactions on transactionId. A later batch wins.

    A null field counts as missing. Returns the merged transactions sorted by date
    and transactionId, and the ids whose content changed between batches (e.g. a
    sale that GoKasa resends with `cancel`).
    """
    merged: dict[str, dict] = {}
    changed: list[str] = []
    for batch in batches:
        for record in batch:
            current = {field: value for field, value in record.items() if value is not None}
            txn_id = current["transactionId"]
            previous = merged.get(txn_id)
            if previous is not None and previous != current:
                changed.append(txn_id)
            merged[txn_id] = current
    records = sorted(merged.values(), key=lambda r: (r["date"], r["transactionId"]))
    return records, changed
