"""A partner's file contract: format, filenames, key, sort order and schema, all from configuration.

Adding a partner whose files fit these settings needs configuration only. GoKasa is a
built-in preset so existing configurations keep working without these settings.
"""

from __future__ import annotations

import json
import re
import typing as t
from dataclasses import dataclass, field
from functools import cached_property

from tap_flowpay_universal_file.history import read_history
from tap_flowpay_universal_file.records import Delivery, merge, read_csv, read_jsonl
from tap_flowpay_universal_file.schema import GOKASA_SCHEMA, SCALAR_TYPES, RecordChecker, load_schema, types

FORMATS = ("jsonl", "csv")
CONTRACT_SETTINGS = (
    "file_format", "file_prefix", "stream_name", "key_properties", "sort_properties", "schema",
    "csv_delimiter", "csv_decimal_separator",
)

# GoKasa's complete contract; the README documents exactly these settings.
GOKASA_SETTINGS = {
    "partner_code": "GoKasa",
    "file_format": "jsonl",
    "file_prefix": "transactions",
    "stream_name": "transactions",
    "key_properties": ["transactionId"],
    "sort_properties": ["date", "transactionId"],
    "schema": GOKASA_SCHEMA,
}


@dataclass(frozen=True)
class FileContract:
    """Everything that differs between partners whose files arrive in blob storage."""

    partner_code: str
    file_format: str
    file_prefix: str
    stream_name: str
    key_properties: tuple[str, ...]
    sort_properties: tuple[str, ...]
    schema: dict = field(hash=False)
    csv_delimiter: str = ","
    csv_decimal_separator: str = "."

    @property
    def input_suffix(self) -> str:
        """Extension of delivered files."""
        return f".{self.file_format}"

    @property
    def history_files(self) -> tuple[str, ...]:
        """Names the pipeline gives a complete history; JSONL is preferred, CSV is read as well."""
        return (f"{self.stream_name}.jsonl", f"{self.stream_name}.csv")

    @cached_property
    def _delivery_checker(self) -> RecordChecker:
        return RecordChecker(self.schema, self.key_properties)

    @cached_property
    def _history_checker(self) -> RecordChecker:
        return RecordChecker(self.schema, self.key_properties, require_all=False)

    def validate_input_name(self, name: str, merchant_id: str) -> None:
        """Use the last underscore as the merchant/delivery boundary.

        Directory names are not identity. Delivery IDs must be nonempty and must
        not contain underscores or dots; merchant IDs may contain underscores.
        """
        filename = name.rsplit("/", 1)[-1]
        pattern = rf"{re.escape(self.file_prefix)}_(.+)_([^_/.]+){re.escape(self.input_suffix)}"
        match = re.fullmatch(pattern, filename)
        if match is None or match[1] != merchant_id:
            msg = (
                f"input_blob must name {self.file_prefix}_<merchant_id>_<delivery_id>{self.input_suffix} "
                "for the configured merchant; delivery_id cannot contain underscores or dots"
            )
            raise ValueError(msg)

    def read_delivery(self, content: bytes, source: str) -> Delivery:
        """Validate and read one delivered file."""
        if self.file_format == "csv":
            return read_csv(
                content, source, self.schema, self._delivery_checker,
                self.csv_delimiter, self.csv_decimal_separator,
            )
        return read_jsonl(content, source, self.schema, self._delivery_checker)

    def read_history(self, content: bytes, source: str) -> list[dict]:
        """Restore and validate a previously exported complete history."""
        return read_history(content, source, self.schema, self._history_checker)

    def merge(self, *batches: t.Iterable[dict]) -> tuple[list[dict], list[t.Any]]:
        """Complete history, and the keys of records that changed."""
        order = self.sort_properties + tuple(key for key in self.key_properties if key not in self.sort_properties)
        return merge(*batches, key=self.key_properties, order=order)


def contract_from_config(config: t.Mapping) -> FileContract:
    """Build and validate the contract, with secret-free errors, before storage is used."""
    partner = config.get("partner_code", "GoKasa")
    if not isinstance(partner, str) or not partner.strip():
        msg = "partner_code must be a non-empty label"
        raise ValueError(msg)
    if config.get("schema") is None:
        if partner != "GoKasa":
            msg = "schema is required: only partner_code GoKasa has a built-in preset"
            raise ValueError(msg)
        given = [setting for setting in CONTRACT_SETTINGS if config.get(setting) is not None]
        if given:
            msg = f"{given[0]} requires schema; configure the complete file contract or none of it"
            raise ValueError(msg)
        config = GOKASA_SETTINGS

    schema = load_schema(config["schema"])
    file_format = config.get("file_format", "jsonl")
    if file_format not in FORMATS:
        msg = f"file_format must be one of: {', '.join(FORMATS)}"
        raise ValueError(msg)
    keys = _field_list(config.get("key_properties"), "key_properties", schema)
    if not keys:
        msg = "key_properties must name at least one field"
        raise ValueError(msg)
    sort = _field_list(config.get("sort_properties"), "sort_properties", schema) or keys
    delimiter = config.get("csv_delimiter", ",")
    if not isinstance(delimiter, str) or len(delimiter) != 1 or delimiter in "\"\r\n":
        msg = "csv_delimiter must be one character other than a quote or a line break"
        raise ValueError(msg)
    decimal = config.get("csv_decimal_separator", ".")
    if decimal not in (".", ","):
        msg = "csv_decimal_separator must be . or ,"
        raise ValueError(msg)
    if file_format == "csv":
        nested = [name for name, node in schema["properties"].items() if not types(node) <= SCALAR_TYPES]
        if nested:
            msg = f"csv files cannot hold array or object fields: {', '.join(nested)}"
            raise ValueError(msg)

    return FileContract(
        partner_code=partner,
        file_format=file_format,
        file_prefix=_name(config.get("file_prefix", "transactions"), "file_prefix", r"[A-Za-z0-9][A-Za-z0-9_-]*"),
        stream_name=_name(config.get("stream_name", "transactions"), "stream_name", r"[A-Za-z_][A-Za-z0-9_]*"),
        key_properties=keys,
        sort_properties=sort,
        schema=schema,
        csv_delimiter=delimiter,
        csv_decimal_separator=decimal,
    )


def _name(value: t.Any, setting: str, pattern: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(pattern, value):
        msg = f"{setting} must match {pattern}"
        raise ValueError(msg)
    return value


def _field_list(value: t.Any, setting: str, schema: t.Mapping) -> tuple[str, ...]:
    """Top-level single-value fields, given as a list or as JSON text."""
    if value is None:
        return ()
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            value = None
    if not isinstance(value, list) or not all(isinstance(name, str) for name in value):
        msg = f"{setting} must be a list of field names"
        raise ValueError(msg)
    if len(set(value)) != len(value):
        msg = f"{setting} names a field twice"
        raise ValueError(msg)
    properties = schema["properties"]
    for name in value:
        if name not in properties or not types(properties[name]) <= SCALAR_TYPES:
            msg = f"{setting} must name top-level single-value fields of the schema; {name!r} is not one"
            raise ValueError(msg)
    return tuple(value)
