"""Partner file contracts, separate from storage and job orchestration."""

from __future__ import annotations

import re
import typing as t

from tap_gokasa.history import HISTORY_FILES, parse_history
from tap_gokasa.schema import SCHEMA
from tap_gokasa.transactions import merge, parse_delivery


class FileAdapter(t.Protocol):
    """The format-specific operations needed by a full-history file stream."""

    stream_name: str
    primary_keys: tuple[str, ...]
    schema: dict
    input_suffix: str
    history_files: tuple[str, ...]

    def validate_input_name(self, name: str, merchant_id: str) -> None:
        """Reject a filename that does not identify the configured merchant."""

    def parse_delivery(self, content: bytes, source: str) -> list[dict]:
        """Validate and read a delivery."""

    def parse_history(self, content: bytes, source: str) -> list[dict]:
        """Restore and validate a previously exported history."""

    def merge(self, *batches: t.Iterable[dict]) -> tuple[list[dict], list[str]]:
        """Return complete history and the identifiers of changed records."""


class GoKasaAdapter:
    """GoKasa JSONL, preserving its original amounts and cancellation fields."""

    stream_name = "transactions"
    primary_keys = ("transactionId",)
    schema = SCHEMA
    input_suffix = ".jsonl"
    history_files = HISTORY_FILES
    parse_delivery = staticmethod(parse_delivery)
    parse_history = staticmethod(parse_history)
    merge = staticmethod(merge)

    def validate_input_name(self, name: str, merchant_id: str) -> None:
        """Use the last underscore as the merchant/delivery boundary.

        Directory names are not identity. Delivery IDs must be nonempty and must
        not contain underscores or dots; merchant IDs may contain underscores.
        No numeric timestamp or country-specific merchant format is assumed.
        """
        filename = name.rsplit("/", 1)[-1]
        match = re.fullmatch(r"transactions_(.+)_([^_/.]+)\.jsonl", filename)
        if match is None or match[1] != merchant_id:
            msg = (
                "input_blob must name transactions_<merchant_id>_<delivery_id>.jsonl "
                "for the configured merchant; delivery_id cannot contain underscores or dots"
            )
            raise ValueError(msg)


def get_adapter(partner_code: str) -> FileAdapter:
    """Select only an explicitly supported format; never guess from file content."""
    if partner_code == "GoKasa":
        return GoKasaAdapter()
    raise ValueError("Unsupported partner_code; the only supported value is GoKasa")
