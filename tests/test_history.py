"""Finding and reading back the previous complete history."""

import datetime
import json
import re

import pytest

from tap_gokasa.history import InvalidHistory, latest_history, parse_history
from tap_gokasa.storage import BlobInfo
from tests.conftest import jsonl, payment, txn


def at(day):
    return datetime.datetime(2026, 10, day, tzinfo=datetime.timezone.utc)


def test_latest_history_is_the_newest_transactions_file_by_last_modified():
    blobs = [
        BlobInfo("history/t/gokasa/202609/zzz/transactions.jsonl", at(1)),
        BlobInfo("history/t/gokasa/202610/aaa/transactions.jsonl", at(3)),
        BlobInfo("history/t/gokasa/202610/bbb/transactions.jsonl", at(2)),
        BlobInfo("history/t/gokasa/202610/ccc/orders.csv", at(9)),
        BlobInfo("history/t/gokasa/202610/ddd/transactions.parquet", at(9)),
    ]
    assert latest_history(blobs).name == "history/t/gokasa/202610/aaa/transactions.jsonl"


def test_a_csv_history_counts_too():
    blobs = [
        BlobInfo("history/t/gokasa/202610/aaa/transactions.jsonl", at(1)),
        BlobInfo("history/t/gokasa/202610/bbb/transactions.csv", at(2)),
    ]
    assert latest_history(blobs).name == "history/t/gokasa/202610/bbb/transactions.csv"


def test_no_history_yet():
    assert latest_history([BlobInfo("history/t/gokasa/202610/a/other.jsonl", at(1))]) is None


@pytest.mark.parametrize(("content", "name"), [
    (b"", "transactions.jsonl"),
    (b"\n\n", "transactions.jsonl"),
    (b"transactionId,payments,date,checkoutId,cancel\n", "transactions.csv"),
])
def test_empty_selected_history_cannot_silently_reset_existing_data(content, name):
    with pytest.raises(InvalidHistory, match="no transactions"):
        parse_history(content, name)


def test_jsonl_history_reads_back_what_the_tap_emitted():
    records = [
        {**txn("split"), "payments": [payment(1000), payment(2500, "CREDIT_CARD")]},
        txn("sale", 5000, cancel=1000102),
        txn("refund", -5000),
    ]
    assert parse_history(jsonl(*records).encode(), "h/transactions.jsonl") == records


def test_jsonl_history_as_pandas_may_write_it():
    # Depending on the pipeline, payments can arrive as JSON text, a missing field as null,
    # a whole number as a float, a numeric id as a number, a date as a timestamp at midnight,
    # and the pipeline may add columns of its own.
    rows = [
        {"transactionId": "a", "payments": json.dumps([payment(100)]), "date": "2026-09-01",
         "checkoutId": 1000001, "cancel": None, "tenant": "tenant1"},
        {"transactionId": "b", "payments": [payment(100)], "date": "2026-09-01T00:00:00.000",
         "checkoutId": "1000001", "cancel": 1000102.0, "tenant": "tenant1"},
    ]
    assert parse_history(jsonl(*rows).encode(), "h/transactions.jsonl") == [
        txn("a"),
        txn("b", cancel=1000102),
    ]


def test_csv_history_as_a_pipeline_writes_it():
    payments = json.dumps([payment(100)]).replace('"', '""')
    content = (
        "transactionId,payments,date,checkoutId,cancel,tenant\n"
        f'a,"{payments}",2026-09-01,1000001,,tenant1\n'
        f'b,"{payments}",2026-09-01,1000001,1000102.0,tenant1\n'
    )
    assert parse_history(content.encode(), "h/transactions.csv") == [
        txn("a"),
        txn("b", cancel=1000102),
    ]


def test_a_history_row_that_does_not_fit_fails_with_file_and_line():
    rows = [txn("a"), {**txn("b"), "date": "September"}]
    with pytest.raises(InvalidHistory, match=re.escape("h/transactions.jsonl line 2: date: 'September' is not a 'date'")):
        parse_history(jsonl(*rows).encode(), "h/transactions.jsonl")


def test_an_unreadable_history_line_fails():
    with pytest.raises(InvalidHistory, match=re.escape("h/transactions.jsonl line 2: not valid JSON")):
        parse_history((jsonl(txn("a")) + "{broken\n").encode(), "h/transactions.jsonl")
