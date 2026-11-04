"""GoKasa tap class."""

from __future__ import annotations

import os
from functools import cached_property

from singer_sdk import Tap
from singer_sdk import typing as th  # JSON schema typing helpers

from tap_gokasa import streams
from tap_gokasa.storage import AzureBlobStore, BlobInfo, BlobStore

INBOUND_ROOT = "inbound/"
ARCHIVE_ROOT = "archive/"
# Placeholders in history_prefix and the job environment variables that replace them.
JOB_VARIABLES = {"{tenant}": "TENANT", "{tap}": "TAP"}


class TapGoKasa(Tap):
    """GoKasa tap class.

    GoKasa pushes JSONL files per merchant to blob storage. Each run merges the
    merchant's waiting files into the previous complete history and emits the new
    complete history as the `transactions` stream, in GoKasa's own format.
    """

    name = "tap-gokasa"

    config_jsonschema = th.PropertiesList(
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
            description="GoKasa merchant ID. Selects the merchant's files.",
        ),
        th.Property(
            "inbound_container",
            th.StringType,
            required=True,
            description="Container GoKasa uploads to",
        ),
        th.Property(
            "inbound_sas_token",
            th.StringType,
            required=True,
            secret=True,
            description=(
                "SAS token for the inbound container: read and list, "
                "plus write and delete when archive_processed is true"
            ),
        ),
        th.Property(
            "inbound_prefix",
            th.StringType,
            default="inbound/{merchant_id}/",
            description=(
                "Where the merchant's files wait; {merchant_id} is replaced. "
                "Use inbound/transactions_{merchant_id}_ if all merchants share one folder."
            ),
        ),
        th.Property(
            "archive_processed",
            th.BooleanType,
            default=True,
            description=(
                "Move processed files from inbound/ to archive/ after the sync. "
                "Set to false if something else moves them once the job has succeeded."
            ),
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
            required=True,
            secret=True,
            description="SAS token for the history container: read and list",
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

    @cached_property
    def store(self) -> BlobStore:
        """Blob storage client, one SAS token per container."""
        return AzureBlobStore(
            self.config["account_url"],
            {
                self.config["inbound_container"]: self.config["inbound_sas_token"],
                self.config["history_container"]: self.config["history_sas_token"],
            },
        )

    @property
    def inbound_container(self) -> str:
        """Container GoKasa uploads to."""
        return self.config["inbound_container"]

    @property
    def inbound_prefix(self) -> str:
        """Blob name prefix of this merchant's waiting files."""
        return self.config["inbound_prefix"].replace("{merchant_id}", self.config["merchant_id"])

    @property
    def history_prefix(self) -> str:
        """Folder with this connection's previous history files."""
        prefix = self.config["history_prefix"]
        for placeholder, variable in JOB_VARIABLES.items():
            if placeholder not in prefix:
                continue
            value = os.environ.get(variable)
            if not value:
                msg = f"history_prefix uses {placeholder}, but {variable} is not set in the job environment"
                raise ValueError(msg)
            prefix = prefix.replace(placeholder, value)
        return prefix

    def waiting_files(self) -> list[BlobInfo]:
        """The merchant's files in inbound/, oldest first, so a newer file wins."""
        blobs = self.store.list_blobs(self.inbound_container, self.inbound_prefix)
        return sorted(
            (blob for blob in blobs if blob.name.endswith(".jsonl")),
            key=lambda blob: (blob.last_modified, blob.name),
        )

    def discover_streams(self) -> list[streams.TransactionsStream]:
        """Return a list of discovered streams."""
        return [streams.TransactionsStream(self)]

    def sync_all(self) -> None:
        """Sync, then move the processed files to archive/."""
        super().sync_all()
        if not self.config["archive_processed"]:
            return
        for name in self.streams["transactions"].consumed_files:
            self._archive(name)

    def _archive(self, name: str) -> None:
        relative = name[len(INBOUND_ROOT):] if name.startswith(INBOUND_ROOT) else name
        target = ARCHIVE_ROOT + relative
        self.store.write(self.inbound_container, target, self.store.read(self.inbound_container, name))
        self.store.delete(self.inbound_container, name)
        self.logger.info("Moved %s to %s.", name, target)


if __name__ == "__main__":
    TapGoKasa.cli()
