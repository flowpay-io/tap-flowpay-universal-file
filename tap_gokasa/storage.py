"""Read-only blob storage access with SAS or service-principal authentication."""

from __future__ import annotations

import typing as t
from dataclasses import dataclass

from azure.identity import ClientSecretCredential
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


class AzureBlobStore:
    """Configured container clients; no write or delete operations are exposed."""

    def __init__(self, account_url: str, credentials: dict[str, t.Any]) -> None:
        """Connect to the containers. No request is made until the first call."""
        self._containers = {
            name: ContainerClient(account_url, name, credential=credential)
            for name, credential in credentials.items()
        }

    @classmethod
    def from_config(cls, config: t.Mapping) -> AzureBlobStore:
        """Create clients without authenticating until the first storage request."""
        inbound = config["inbound_container"]
        history = config["history_container"]
        if config["auth_method"] == "service_principal":
            credential = ClientSecretCredential(
                tenant_id=config["azure_tenant_id"],
                client_id=config["azure_client_id"],
                client_secret=config["azure_client_secret"],
            )
            credentials = {inbound: credential, history: credential}
        else:
            credentials = {
                inbound: config["inbound_sas_token"],
                history: config["history_sas_token"],
            }
        return cls(config["account_url"], credentials)

    def _container(self, name: str) -> ContainerClient:
        try:
            return self._containers[name]
        except KeyError:
            msg = "No storage client configured for this container"
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
