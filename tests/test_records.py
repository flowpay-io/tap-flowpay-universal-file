"""Reading delivered files, checking them against the configured contract, and merging on the key."""

import json
import re

import pytest

from tap_flowpay_universal_file.contract import contract_from_config
from tap_flowpay_universal_file.records import InvalidDelivery
from tests.conftest import CSV_HEADER, CSV_PARTNER, SAMPLE_LINE, jsonl, payment, txn

GOKASA = contract_from_config({"partner_code": "GoKasa"})
PARTNER = contract_from_config(CSV_PARTNER)


def parse(*records):
    return GOKASA.read_delivery(jsonl(*records).encode(), "inbound/x.jsonl").records


def read_csv(rows, header=CSV_HEADER):
    return PARTNER.read_delivery((header + "".join(rows)).encode(), "inbound/x.csv")


# GoKasa (built-in preset, JSONL)

def test_a_sample_line_passes_through_unchanged():
    assert GOKASA.read_delivery((SAMPLE_LINE + "\n").encode(), "f.jsonl").records == [json.loads(SAMPLE_LINE)]


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
    records = GOKASA.read_delivery(content.encode(), "f.jsonl").records
    assert [r["transactionId"] for r in records] == ["t1", "t2"]


@pytest.mark.parametrize("content", [b"", b"\xef\xbb\xbf\n\r\n  "])
def test_empty_delivery_cannot_succeed_without_producing_history(content):
    with pytest.raises(InvalidDelivery, match="no records"):
        GOKASA.read_delivery(content, "empty.jsonl")


def test_fields_that_are_not_configured_are_ignored_and_counted():
    records = [
        {**txn("t1"), "status": "DONE"},
        {**txn("t2"), "status": "OPEN", "payments": [{**payment(100), "tip": 10}]},
        txn("t3"),
    ]
    delivery = GOKASA.read_delivery(jsonl(*records).encode(), "inbound/x.jsonl")
    assert delivery.records == [txn("t1"), txn("t2"), txn("t3")]
    assert delivery.ignored == {"status": 2, "payments[].tip": 1}


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
    ],
)
def test_configured_field_that_breaks_the_contract_fails(change, error):
    record = txn("t1")
    change(record)
    with pytest.raises(InvalidDelivery, match=re.escape(f"inbound/x.jsonl line 1: {error}")):
        parse(record)


def test_unreadable_lines_fail_with_file_and_line():
    content = (json.dumps(txn("t1")) + "\n{not json\n[1, 2]\n").encode()
    with pytest.raises(InvalidDelivery) as failure:
        GOKASA.read_delivery(content, "inbound/x.jsonl")
    message = str(failure.value)
    assert "inbound/x.jsonl: 2 problems" in message
    assert "inbound/x.jsonl line 2: not valid JSON" in message
    assert "inbound/x.jsonl line 3: [1, 2] is not of type 'object'" in message


def test_the_report_lists_the_first_20_problems():
    content = jsonl(*({**txn(f"t{i}"), "date": "bad"} for i in range(25))).encode()
    with pytest.raises(InvalidDelivery) as failure:
        GOKASA.read_delivery(content, "f.jsonl")
    message = str(failure.value)
    assert "f.jsonl: 25 problems" in message
    assert "f.jsonl line 20:" in message
    assert "f.jsonl line 21:" not in message
    assert "... and 5 more" in message


def test_merge_counts_a_repeated_transaction_once():
    records, changed = GOKASA.merge([txn("a"), txn("b")], [txn("b"), txn("c")])
    assert [r["transactionId"] for r in records] == ["a", "b", "c"]
    assert changed == []


def test_merge_later_batch_wins_and_reports_the_change():
    # A sale can be resent with `cancel` once it is refunded.
    sale = txn("a", 5000, checkout_id="1000101")
    cancelled = {**sale, "cancel": 1000102}
    records, changed = GOKASA.merge([sale], [cancelled])
    assert records == [cancelled]
    assert changed == ["a"]


def test_merge_treats_a_null_field_as_missing():
    records, changed = GOKASA.merge([{**txn("a"), "cancel": None}], [txn("a")])
    assert records == [txn("a")]
    assert changed == []


def test_merge_sorts_by_date_then_id():
    records, _ = GOKASA.merge([txn("b", date="2026-09-02"), txn("z", date="2026-09-01"), txn("a", date="2026-09-02")])
    assert [r["transactionId"] for r in records] == ["z", "a", "b"]


# A partner configured only through settings (CSV)

def test_csv_rows_are_converted_by_the_configured_types():
    delivery = read_csv([
        "R1;1;2026-09-01 10:00:00;123,50;false;first;Alice\n",
        "R1;2;2026-09-01T10:00:00+02:00;-5;TRUE;;Alice\n",
        "R2;1;2026-09-02T08:30;0,5;;;\n",
    ])
    assert delivery.records == [
        {"receiptId": "R1", "line": 1, "soldAt": "2026-09-01 10:00:00", "amount": 123.5, "refund": False,
         "note": "first"},
        {"receiptId": "R1", "line": 2, "soldAt": "2026-09-01T10:00:00+02:00", "amount": -5, "refund": True},
        {"receiptId": "R2", "line": 1, "soldAt": "2026-09-02T08:30", "amount": 0.5},
    ]
    assert delivery.ignored == {"cashier": 2}


@pytest.mark.parametrize(("row", "error"), [
    ("R1;1.5;2026-09-01 10:00;1;;;\n", "line: '1.5' is not an integer"),
    ("R1;1;2026-09-01 10:00;1.234,5;;;\n", "amount: '1.234,5' is not a number"),
    ("R1;1;2026-09-01 10:00;1;yes;;\n", "refund: 'yes' is not a boolean"),
    ("R1;1;yesterday;1;;;\n", "soldAt: 'yesterday' is not a 'date-time'"),
    ("R1;1;2026-09-01 10:00;;;;\n", "'amount' is a required property"),
    (";1;2026-09-01 10:00;1;;;\n", "'receiptId' is a required property"),
    ("R1;1;2026-09-01 10:00;1;;;;extra\n", "more columns than the header"),
])
def test_csv_value_that_breaks_the_contract_fails_with_its_line(row, error):
    with pytest.raises(InvalidDelivery, match=re.escape(f"inbound/x.csv line 2: {error}")):
        read_csv([row])


def test_csv_with_duplicate_column_names_fails():
    with pytest.raises(InvalidDelivery, match="duplicate"):
        read_csv(["R1;1;2026-09-01 10:00;1;1\n"], header="receiptId;line;soldAt;amount;amount\n")


def test_csv_with_only_a_header_has_no_records():
    with pytest.raises(InvalidDelivery, match="no records"):
        read_csv([])


@pytest.mark.parametrize("value", [
    "2026-09-01T10:00:00", "2026-09-01 10:00:00", "2026-09-01T10:00:00Z", "2026-09-01T10:00:00.123+02:00",
    "2026-09-01T10:00", "2026-09-01T10:00:00.123456789-0530",
])
def test_timestamps_with_or_without_offset_pass_unchanged(value):
    assert read_csv([f"R1;1;{value};1;;;\n"]).records[0]["soldAt"] == value


@pytest.mark.parametrize("value", [
    "2026-09-01", "01.09.2026 10:00", "2026-13-01T10:00:00", "2026-09-01T25:00:00", "2026-09-01T10:00+25:00",
])
def test_timestamps_that_are_not_iso_fail(value):
    with pytest.raises(InvalidDelivery, match="is not a 'date-time'"):
        read_csv([f"R1;1;{value};1;;;\n"])


def test_merge_on_a_composite_key_keeps_each_line_and_sorts_by_the_configured_order():
    first = read_csv(["R2;1;2026-09-02 09:00;1;;;\n", "R1;2;2026-09-01 10:00;2;;;\n", "R1;1;2026-09-01 10:00;3;;;\n"])
    resent = read_csv(["R1;2;2026-09-01 10:00;20;;;\n"])
    records, changed = PARTNER.merge(first.records, resent.records)
    assert [(r["receiptId"], r["line"], r["amount"]) for r in records] == [("R1", 1, 3), ("R1", 2, 20), ("R2", 1, 1)]
    assert changed == [("R1", 2)]
