"""Test helpers: GoKasa transactions in the shape of the real export, and an in-memory blob store."""

from __future__ import annotations

import datetime
import json

import pytest

from tap_gokasa.storage import BlobInfo

# A made-up line in the shape of GoKasa's export.
SAMPLE_LINE = (
    '{"transactionId":"0123456789abcdef01234567","payments":[{"paymentMethod":"CREDIT_CARD",'
    '"amount":12345,"currency":"CZK","acceptedAmount":12345,"acceptedCurrency":"CZK"}],'
    '"date":"2026-01-15","checkoutId":"1000001"}'
)


def payment(amount: int, method: str = "CASH") -> dict:
    """One payment as GoKasa sends it. In the real export acceptedAmount always equals amount."""
    return {
        "paymentMethod": method,
        "amount": amount,
        "currency": "CZK",
        "acceptedAmount": amount,
        "acceptedCurrency": "CZK",
    }


def txn(
    txn_id: str,
    *amounts: int,
    date: str = "2026-09-01",
    method: str = "CASH",
    checkout_id: str = "1000001",
    cancel: int | None = None,
) -> dict:
    """One GoKasa transaction with one payment per amount (default: one payment of 100)."""
    record = {
        "transactionId": txn_id,
        "payments": [payment(amount, method) for amount in (amounts or (100,))],
        "date": date,
        "checkoutId": checkout_id,
    }
    if cancel is not None:
        record["cancel"] = cancel
    return record


def jsonl(*records: dict) -> str:
    """Records as a JSONL file, compact like GoKasa's export."""
    return "".join(json.dumps(r, separators=(",", ":")) + "\n" for r in records)


class FakeBlobStore:
    """BlobStore in memory. Each put is one minute after the previous one."""

    def __init__(self) -> None:
        self.blobs: dict[tuple[str, str], tuple[bytes, datetime.datetime]] = {}
        self._clock = datetime.datetime(2026, 10, 1, tzinfo=datetime.timezone.utc)

    def put(self, container: str, name: str, data: bytes | str) -> None:
        self._clock += datetime.timedelta(minutes=1)
        if isinstance(data, str):
            data = data.encode()
        self.blobs[(container, name)] = (data, self._clock)

    def names(self, container: str) -> list[str]:
        return sorted(name for (c, name) in self.blobs if c == container)

    def list_blobs(self, container: str, prefix: str) -> list[BlobInfo]:
        return [
            BlobInfo(name, modified)
            for (c, name), (_, modified) in self.blobs.items()
            if c == container and name.startswith(prefix)
        ]

    def read(self, container: str, name: str) -> bytes:
        return self.blobs[(container, name)][0]

    def write(self, container: str, name: str, data: bytes) -> None:
        self.put(container, name, data)

    def delete(self, container: str, name: str) -> None:
        del self.blobs[(container, name)]


@pytest.fixture
def store() -> FakeBlobStore:
    return FakeBlobStore()
