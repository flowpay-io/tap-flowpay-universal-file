"""Tap class for partner files pushed to Azure Blob Storage."""

from __future__ import annotations

from functools import cached_property

from singer_sdk import Tap
from singer_sdk import typing as th  # JSON schema typing helpers

from tap_flowpay_universal_file import streams
from tap_flowpay_universal_file.configuration import (
    apply_job_environment,
    history_prefix,
    validate_blob_name,
    validate_settings,
)
from tap_flowpay_universal_file.contract import FileContract, contract_from_config
from tap_flowpay_universal_file.history import latest_history
from tap_flowpay_universal_file.storage import AzureBlobStore, BlobInfo, BlobStore


class TapFlowpayUniversalFile(Tap):
    """Merges a partner's delivered file into the merchant's previous complete history.

    The configured file contract owns the format; the tap owns storage and job selection.
    Prefix scanning remains available for existing manual configurations.
    """

    name = "tap-flowpay-universal-file"

    config_jsonschema = th.PropertiesList(
        th.Property(
            "partner_code",
            th.StringType,
            default="GoKasa",
            description="Partner label. Without schema, only GoKasa (built-in preset) is accepted.",
        ),
        th.Property(
            "file_format", th.StringType,
            description="jsonl or csv. Part of the file contract; requires schema.",
        ),
        th.Property(
            "file_prefix", th.StringType,
            description="Delivered files are named <file_prefix>_<merchant_id>_<delivery_id>.<format>. "
                        "Default transactions.",
        ),
        th.Property(
            "stream_name", th.StringType,
            description="Output stream; history files are <stream_name>.jsonl or .csv. Default transactions.",
        ),
        th.Property(
            "key_properties", th.ArrayType(th.StringType),
            description="Fields that identify a record, used for merging. Required with schema.",
        ),
        th.Property(
            "sort_properties", th.ArrayType(th.StringType),
            description="Output order. Defaults to key_properties.",
        ),
        th.Property(
            "schema", th.ObjectType(),
            description="JSON Schema of one record. Fields not in it are ignored.",
        ),
        th.Property("csv_delimiter", th.StringType, description="CSV only. Default ,"),
        th.Property("csv_decimal_separator", th.StringType, description="CSV only, . or , Default ."),
        th.Property(
            "auth_method",
            th.StringType,
            default="sas",
            description="service_principal (recommended) or sas (existing configurations)",
        ),
        th.Property(
            "azure_tenant_id", th.StringType,
            description="Azure directory ID, required for service_principal authentication",
        ),
        th.Property(
            "azure_client_id", th.StringType,
            description="Application ID, required for service_principal authentication",
        ),
        th.Property(
            "azure_client_secret", th.StringType, secret=True,
            description="Application secret, required for service_principal authentication",
        ),
        th.Property(
            "account_url",
            th.StringType,
            required=True,
            description="Blob endpoint, e.g. https://<account>.blob.core.windows.net",
        ),
        th.Property(
            "merchant_id",
            th.StringType,
            required=True,
            description="Partner merchant ID. Must match the selected delivery's filename.",
        ),
        th.Property(
            "inbound_container",
            th.StringType,
            required=True,
            description="Trusted input container for this partner",
        ),
        th.Property(
            "inbound_sas_token",
            th.StringType,
            secret=True,
            description="Required in sas mode: read; also list for prefix scanning",
        ),
        th.Property(
            "input_blob", th.StringType,
            description="Exact relative input blob name. When supplied, no input listing is performed.",
        ),
        th.Property(
            "require_input_blob", th.BooleanType, default=False,
            description="Set true in event-driven configurations to reject jobs missing input_blob.",
        ),
        th.Property(
            "inbound_prefix",
            th.StringType,
            default="inbound/{merchant_id}/",
            description=(
                "Legacy scan prefix, ignored with input_blob; {merchant_id} is replaced. "
                "Use inbound/transactions_{merchant_id}_ if all merchants share one folder."
            ),
        ),
        th.Property(
            "archive_processed",
            th.BooleanType,
            default=False,
            description="Deprecated. Must be false: the tap always leaves source files in place.",
        ),
        th.Property(
            "history_container",
            th.StringType,
            required=True,
            description="Container the complete history files are written to",
        ),
        th.Property(
            "history_sas_token",
            th.StringType,
            secret=True,
            description="Required in sas mode: read; also list for automatic history selection",
        ),
        th.Property(
            "history_blob", th.StringType,
            description=(
                "Exact previous successful history blob, or null for the first run. "
                "Required with input_blob. Omit both for legacy scanning."
            ),
        ),
        th.Property(
            "history_prefix",
            th.StringType,
            required=True,
            description=(
                "Folder with this connection's previous history files. {tenant} and {tap} "
                "are replaced with the TENANT and TAP environment variables of the job."
            ),
        ),
    ).to_dict()
    # Unlike omission (legacy lookup), explicit null means an intentional initial run.
    config_jsonschema["properties"]["history_blob"]["type"] = ["string", "null"]
    # Host UIs often store structured settings as JSON text.
    config_jsonschema["properties"]["schema"] = {
        **config_jsonschema["properties"]["schema"], "type": ["object", "string", "null"],
    }
    for _setting in ("key_properties", "sort_properties"):
        config_jsonschema["properties"][_setting]["type"] = ["array", "string", "null"]

    def _validate_config(
        self, *, raise_errors: bool = True, warnings_as_errors: bool = False,
    ) -> tuple[list[str], list[str]]:
        """Apply the job's environment, then validate credentials and references before storage is used."""
        try:
            self._config = apply_job_environment(self._config)
        except ValueError as exc:
            if raise_errors:
                raise
            return [], [str(exc)]
        warnings, errors = super()._validate_config(
            raise_errors=raise_errors, warnings_as_errors=warnings_as_errors,
        )
        if not errors:
            try:
                validate_settings(self.config)
            except ValueError as exc:
                if raise_errors:
                    raise
                errors.append(str(exc))
        return warnings, errors

    @cached_property
    def contract(self) -> FileContract:
        """The partner's file format, schema, key and sort order."""
        return contract_from_config(self.config)

    @cached_property
    def store(self) -> BlobStore:
        """Read-only clients using the configured authentication method."""
        return AzureBlobStore.from_config(self.config)

    @property
    def inbound_container(self) -> str:
        """The partner's configured input container."""
        return self.config["inbound_container"]

    @property
    def inbound_prefix(self) -> str:
        """Blob name prefix of this merchant's waiting files."""
        prefix = self.config["inbound_prefix"].replace("{merchant_id}", self.config["merchant_id"])
        validate_blob_name(prefix.rstrip("/"), "inbound_prefix")
        return prefix

    @property
    def history_prefix(self) -> str:
        """Folder with this connection's previous history files."""
        return history_prefix(self.config)

    def waiting_files(self) -> list[BlobInfo]:
        """Legacy scan: oldest first, so a more recently uploaded file wins."""
        blobs = self.store.list_blobs(self.inbound_container, self.inbound_prefix)
        return sorted(
            (blob for blob in blobs if blob.name.endswith(self.contract.input_suffix)),
            key=lambda blob: (blob.last_modified, blob.name),
        )

    def input_files(self) -> list[str]:
        """The one requested blob, or legacy waiting files; never fall back on failure."""
        if "input_blob" in self.config:
            return [self.config["input_blob"]]
        self.logger.warning("Legacy prefix scan enabled; use input_blob and history_blob for exact-file jobs.")
        names = [blob.name for blob in self.waiting_files()]
        for name in names:
            validate_blob_name(name, "input_blob")
            self.contract.validate_input_name(name, self.config["merchant_id"])
        return names

    def previous_history(self) -> str | None:
        """Use the caller's explicit history choice, or the legacy newest-file lookup."""
        if "history_blob" in self.config:
            return self.config["history_blob"]
        previous = latest_history(
            self.store.list_blobs(self.config["history_container"], self.history_prefix),
            filenames=self.contract.history_files,
        )
        return previous.name if previous else None

    def discover_streams(self) -> list[streams.FileHistoryStream]:
        """Return the stream described by the file contract."""
        return [streams.FileHistoryStream(self)]


if __name__ == "__main__":
    TapFlowpayUniversalFile.cli()
