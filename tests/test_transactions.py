"""Reading GoKasa files, checking them against the stream schema, and merging on transactionId."""

import json
import re

import pytest

from tap_gokasa.transactions import InvalidDelivery, merge, parse_delivery
from tests.conftest import SAMPLE_LINE, jsonl, payment, txn


def parse(*records):
    return parse_delivery(jsonl(*records).encode(), "inbound/x.jsonl")


def test_a_sample_line_passes_through_unchanged():
    assert parse_delivery((SAMPLE_LINE + "\n").encode(), "f.jsonl") == [json.loads(SAMPLE_LINE)]


def test_split_payment_refund_and_cancel_pass_through_unchanged():
    records = [
        {**txn("split"), "payments": [payment(1000), payment(2500, "CREDIT_CARD")]},
        txn("sale", 5000, checkout_id="1000101", cancel=1000102),
        txn("refund", -5000, checkout_id="1000102"),
        txn("bank", 7300, method="BANK_TRANSFER"),
    ]
    assert parse(*records) == records


def test_transaction_without_payments_is_kept():
    # The tap does not interpret transactions; downstream decides what to do with it.
    record = {**txn("t1"), "payments": []}
    assert parse(record) == [record]


def test_bom_blank_lines_crlf_and_no_final_newline_are_fine():
    content = "﻿" + json.dumps(txn("t1")) + "\r\n\r\n" + json.dumps(txn("t2"))
    assert [r["transactionId"] for r in parse_delivery(content.encode(), "f.jsonl")] == ["t1", "t2"]


@pytest.mark.parametrize(
    ("change", "error"),
    [
        (lambda r: r.pop("transactionId"), "'transactionId' is a required property"),
        (lambda r: r.update(transactionId=""), "transactionId: '' should be non-empty"),
        (lambda r: r.update(transactionId=123), "transactionId: 123 is not of type 'string'"),
        (lambda r: r.pop("date"), "'date' is a required property"),
        (lambda r: r.update(date="24.9.2026"), "date: '24.9.2026' is not a 'date'"),
        (lambda r: r.update(date="20260924"), "date: '20260924' is not a 'date'"),
        (lambda r: r.update(date="2026-02-30"), "date: '2026-02-30' is not a 'date'"),
        (lambda r: r.update(payments=None), "payments: None is not of type 'array'"),
        (lambda r: r["payments"][0].update(amount=1.5), "payments[0].amount: 1.5 is not of type 'integer'"),
        (lambda r: r["payments"][0].update(amount="100"), "payments[0].amount: '100' is not of type 'integer'"),
        (lambda r: r["payments"][0].pop("currency"), "payments[0]: 'currency' is a required property"),
        (lambda r: r.update(cancel="1000102"), "cancel: '1000102' is not of type 'integer', 'null'"),
        (lambda r: r.update(status="DONE"), "Additional properties are not allowed ('status' was unexpected)"),
        (lambda r: r["payments"][0].update(tip=10), "payments[0]: Additional properties are not allowed ('tip' was unexpected)"),
    ],
)
def test_line_that_breaks_the_contract_fails(change, error):
    record = txn("t1")
    change(record)
    with pytest.raises(InvalidDelivery, match=re.escape(f"inbound/x.jsonl line 1: {error}")):
        parse(record)


def test_unreadable_lines_fail_with_file_and_line():
    content = (json.dumps(txn("t1")) + "\n{not json\n[1, 2]\n").encode()
    with pytest.raises(InvalidDelivery) as failure:
        parse_delivery(content, "inbound/x.jsonl")
    message = str(failure.value)
    assert "inbound/x.jsonl: 2 problems" in message
    assert "inbound/x.jsonl line 2: not valid JSON" in message
    assert "inbound/x.jsonl line 3: [1, 2] is not of type 'object'" in message


def test_the_report_lists_the_first_20_problems():
    content = jsonl(*({**txn(f"t{i}"), "date": "bad"} for i in range(25))).encode()
    with pytest.raises(InvalidDelivery) as failure:
        parse_delivery(content, "f.jsonl")
    message = str(failure.value)
    assert "f.jsonl: 25 problems" in message
    assert "f.jsonl line 20:" in message
    assert "f.jsonl line 21:" not in message
    assert "... and 5 more" in message


def test_merge_counts_a_repeated_transaction_once():
    records, changed = merge([txn("a"), txn("b")], [txn("b"), txn("c")])
    assert [r["transactionId"] for r in records] == ["a", "b", "c"]
    assert changed == []


def test_merge_later_batch_wins_and_reports_the_change():
    # A sale can be resent with `cancel` once it is refunded.
    sale = txn("a", 5000, checkout_id="1000101")
    cancelled = {**sale, "cancel": 1000102}
    records, changed = merge([sale], [cancelled])
    assert records == [cancelled]
    assert changed == ["a"]


def test_merge_treats_a_null_field_as_missing():
    records, changed = merge([{**txn("a"), "cancel": None}], [txn("a")])
    assert records == [txn("a")]
    assert changed == []


def test_merge_sorts_by_date_then_id():
    records, _ = merge([txn("b", date="2026-09-02"), txn("z", date="2026-09-01"), txn("a", date="2026-09-02")])
    assert [r["transactionId"] for r in records] == ["z", "a", "b"]
