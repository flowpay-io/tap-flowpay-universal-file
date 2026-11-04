"""Blob storage access. The tap only lists, reads, writes and deletes blobs."""

from __future__ import annotations

import typing as t
from dataclasses import dataclass

from azure.storage.blob import ContainerClient

if t.TYPE_CHECKING:
    import datetime


@dataclass(frozen=True)
class BlobInfo:
    """Name and lastModified of one blob."""

    name: str
    last_modified: datetime.datetime


class BlobStore(t.Protocol):
    """What the tap needs from blob storage. Tests use an in-memory version."""

    def list_blobs(self, container: str, prefix: str) -> list[BlobInfo]:
        """List blobs whose name starts with prefix."""

    def read(self, container: str, name: str) -> bytes:
        """Download one blob."""

    def write(self, container: str, name: str, data: bytes) -> None:
        """Upload one blob, replacing it if it exists."""

    def delete(self, container: str, name: str) -> None:
        """Delete one blob."""


class AzureBlobStore:
    """BlobStore backed by Azure containers, each with its own SAS token.

    A token per container keeps each one scoped to what the tap needs there.
    """

    def __init__(self, account_url: str, sas_tokens: dict[str, str]) -> None:
        """Connect to the containers. No request is made until the first call."""
        self._containers = {
            name: ContainerClient(account_url, name, credential=token)
            for name, token in sas_tokens.items()
        }

    def _container(self, name: str) -> ContainerClient:
        try:
            return self._containers[name]
        except KeyError:
            msg = f"No SAS token configured for container {name}"
            raise ValueError(msg) from None

    def list_blobs(self, container: str, prefix: str) -> list[BlobInfo]:
        """List blobs whose name starts with prefix."""
        return [
            BlobInfo(blob.name, blob.last_modified)
            for blob in self._container(container).list_blobs(name_starts_with=prefix)
        ]

    def read(self, container: str, name: str) -> bytes:
        """Download one blob."""
        return self._container(container).download_blob(name).readall()

    def write(self, container: str, name: str, data: bytes) -> None:
        """Upload one blob, replacing it if it exists."""
        self._container(container).upload_blob(name, data, overwrite=True)

    def delete(self, container: str, name: str) -> None:
        """Delete one blob."""
        self._container(container).delete_blob(name)
