"""Exercise the real Azure download pipeline using an offline HTTP transport."""

import time
from unittest.mock import Mock

import pytest
from azure.core.credentials import AccessToken
from azure.core.exceptions import ClientAuthenticationError, ResourceNotFoundError
from azure.core.pipeline.transport import HttpResponse, HttpTransport
from azure.storage.blob import ContainerClient
from requests.structures import CaseInsensitiveDict

from tap_flowpay_universal_file.tap import TapFlowpayUniversalFile
from tests.test_authentication import SP_CONFIG
from tests.test_tap import HISTORY, INBOUND


class MemoryResponse(HttpResponse):
    def __init__(self, request, content, status=206):
        super().__init__(request, None)
        self.status_code = status
        self.reason = "Partial Content" if status == 206 else "Not Found"
        self._content = content
        self.headers = CaseInsensitiveDict({
            "content-length": str(len(content)),
            "content-range": f"bytes 0-{len(content) - 1}/{len(content)}",
            "content-type": "application/octet-stream",
            "etag": '"test-etag"',
            "x-ms-blob-type": "BlockBlob",
        })
        if status == 404:
            self.headers["x-ms-error-code"] = "BlobNotFound"

    def body(self):
        return self._content

    def read(self):
        return self._content

    def close(self):
        pass

    def stream_download(self, pipeline, **kwargs):
        return MemoryStream(self)


class MemoryStream:
    def __init__(self, response):
        self.response = response
        self.content = response.body()
        self.content_length = len(self.content)

    def __iter__(self):
        yield self.content


class MemoryTransport(HttpTransport):
    def __init__(self, status=206):
        self.requests = []
        self.status = status

    def open(self):
        pass

    def close(self):
        pass

    def __exit__(self, *args):
        self.close()

    def send(self, request, **kwargs):
        self.requests.append(request)
        return MemoryResponse(request, b'{"test":"unchanged"}\n', self.status)


class MemoryCredential:
    def __init__(self):
        self.scopes = []

    def get_token(self, *scopes, **kwargs):
        self.scopes.append(scopes)
        return AccessToken("synthetic-access-token", int(time.time()) + 3600)


def make_store(monkeypatch, transport, credential):
    monkeypatch.setattr("tap_flowpay_universal_file.storage.ClientSecretCredential", Mock(return_value=credential))
    monkeypatch.setattr(
        "tap_flowpay_universal_file.storage.ContainerClient",
        lambda *args, **kwargs: ContainerClient(*args, transport=transport, retry_total=0, **kwargs),
    )
    return TapFlowpayUniversalFile(config=SP_CONFIG).store


def test_real_sdk_downloads_both_files_with_bearer_authentication(monkeypatch):
    transport = MemoryTransport()
    credential = MemoryCredential()
    store = make_store(monkeypatch, transport, credential)

    assert store.read(INBOUND, "input.jsonl") == b'{"test":"unchanged"}\n'
    assert store.read(HISTORY, "transactions.jsonl") == b'{"test":"unchanged"}\n'

    assert len(transport.requests) == 2
    assert all(request.method == "GET" for request in transport.requests)
    assert all(request.headers["Authorization"] == "Bearer synthetic-access-token" for request in transport.requests)
    assert credential.scopes == [("https://storage.azure.com/.default",)] * 2


def test_not_found_is_a_failure_not_empty_history(monkeypatch):
    store = make_store(monkeypatch, MemoryTransport(status=404), MemoryCredential())
    with pytest.raises(ResourceNotFoundError):
        store.read(HISTORY, "missing.jsonl")


def test_authentication_failure_is_not_retried_with_other_credentials(monkeypatch):
    credential = Mock(spec=MemoryCredential)
    credential.get_token.side_effect = ClientAuthenticationError("Authentication failed")
    transport = MemoryTransport()
    store = make_store(monkeypatch, transport, credential)
    with pytest.raises(ClientAuthenticationError):
        store.read(INBOUND, "input.jsonl")
    assert transport.requests == []
